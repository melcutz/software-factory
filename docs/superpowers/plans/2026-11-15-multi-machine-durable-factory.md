# Multi-Machine Durable Software Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `software-factory-agy` into a durable, multi-machine autonomous factory using Git-native `refs/factory/*` coordination, `herdr-core` (`hc`) + `herdr-app` live visibility/intervention (OSC 7501 + `hc run --bg`), a global kill switch (`factory halt`/`resume`), and a polite host/LLM quota governor.

**Architecture:** Peer machines run `factory worker --all` inside `herdr-core` (`hc`) without any central coordinator server (falling back to local threads/subprocesses in plain terminals). All leases, checkpoints, locks, DAG cache, halt control, and 429 quota cooldowns live in orphan Git commits under `refs/factory/*` updated via `--force-with-lease`.

**Tech Stack:** Python 3.12 (`stdlib` only: `subprocess`, `json`, `hashlib`, `threading`, `socket`, `dataclasses`, `argparse`), Git plumbing, `gh` CLI, `herdr-core` (`hc`) + OSC 7501, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-15-multi-machine-durable-factory-design.md`

## Global Constraints

- Zero new third-party Python dependencies (`pyproject.toml` runtime dependencies remain stdlib-only).
- POSIX runtime everywhere (Linux, macOS, Windows via WSL2).
- Never push mid-build slice checkpoints to `refs/heads/factory/job-<N>` (use `refs/factory/checkpoints/job-<N>`) so private-repo GitHub Actions minutes are not consumed.
- Worker heartbeats use one ref per machine (`refs/factory/workers/<machine_id>`, 120s interval, 360s TTL, +60s skew buffer for takeover = 420s).
- `factory halt` must never increment `round`, `qa_round`, or escalate an issue to `needs-human`.
- When `$HC_TERMINAL_ID` is unset (plain terminal, CI, `pytest`), `HerdrBridge` must fall back cleanly without invoking `hc` or failing.

## Review Focus

- **Clock skew / stale lease edge case:** A peer machine with slightly ahead clock must not steal a lease before `expires_at + 60s` (`420s` total since last heartbeat); test explicitly that a 370s-old heartbeat is NOT stolen while a 430s-old heartbeat IS stolen.
- **Empty / initial repo without `refs/factory/*`:** `claim_job_lease` when `refs/factory/jobs/job-<N>` does not yet exist on `origin` must pass `0000000000000000000000000000000000000000` (40 zeros) as expected old SHA to `--force-with-lease`.
- **Halt during `deploy_to_staging`:** Without `--force`, a worker inside `deploy_to_staging` must finish the deploy/rollback atomically before honoring `halt` and checkpointing, whereas `--force` interrupts immediately.
- **Quota backoff reset:** A `429` sets `consecutive_429s=1` (`120s`), escalating `300s -> 900s -> 3600s` max, and the first successful `agy` call for that base model clears its cooldown entry.
- **Non-Herdr execution:** Running `factory worker --all` or `factory run` without `$HC_TERMINAL_ID` must execute jobs in local worker threads/subprocesses without raising or invoking `hc`.

---

### Task 1: Git-Native Distributed State, Leases, Checkpoints & Locks (`factory/lib/git_state.py`)

**Files:**
- Create: `factory/lib/git_state.py`
- Test: `tests/test_git_state.py`

**Interfaces:**
- Consumes: `factory.lib.git_ops._run_git`
- Produces:
  - `LeaseLostError(RuntimeError)`
  - `GitStateStore(repo_root: str, machine_id: str, remote: str = "origin")`
  - `GitStateStore.write_worker_heartbeat(active_jobs: int, max_workers: int, paused: bool) -> str`
  - `GitStateStore.delete_worker_heartbeat() -> None`
  - `GitStateStore.list_worker_heartbeats() -> dict[str, dict]`
  - `GitStateStore.claim_job_lease(job_id: str, initial_job: dict | None = None, *, now_ts: float | None = None) -> tuple[bool, dict | None]`
  - `GitStateStore.checkpoint_job(job_id: str, jdir: str, job_data: dict, *, release_lease: bool = False) -> bool`
  - `GitStateStore.hydrate_job(job_id: str, jdir: str) -> dict | None`
  - `GitStateStore.list_remote_jobs() -> list[dict]`
  - `GitStateStore.push_slice_checkpoint(job_id: str, worktree: str) -> str`
  - `GitStateStore.fetch_slice_checkpoint(job_id: str, worktree: str) -> str | None`
  - `GitStateStore.delete_slice_checkpoint(job_id: str) -> None`
  - `GitStateStore.acquire_staging_lock(job_ids: list[str], *, ttl_sec: int = 1800, now_ts: float | None = None) -> bool`
  - `GitStateStore.release_staging_lock() -> None`
  - `GitStateStore.get_control_state() -> dict`
  - `GitStateStore.set_halted(halted: bool, reason: str = "") -> dict`
  - `GitStateStore.get_quota_state() -> dict`
  - `GitStateStore.record_quota_cooldown(base_model: str, reason: str, retry_after_sec: int | None = None) -> dict`
  - `GitStateStore.clear_quota_cooldown(base_model: str) -> None`
  - `GitStateStore.get_cached_dag(issue_set_hash: str) -> dict | None`
  - `GitStateStore.put_cached_dag(issue_set_hash: str, order_deps: dict, hard_deps: dict) -> None`
  - `GitStateStore.prune_stale_refs(*, max_age_days: int = 30, now_ts: float | None = None) -> int`

- [ ] **Step 1: Write the failing tests in `tests/test_git_state.py`**

```python
import json
import subprocess
import threading
import time

from factory.lib.git_state import GitStateStore


def _init_bare_and_clones(tmp_path, n=2):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:main"], check=True)
    clones = []
    for i in range(n):
        c = tmp_path / f"worker-{i}"
        subprocess.run(["git", "clone", str(bare), str(c)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(c), "config", "user.email", "t@e.com"], check=True)
        subprocess.run(["git", "-C", str(c), "config", "user.name", f"W{i}"], check=True)
        clones.append(str(c))
    return str(bare), clones


def test_concurrent_cas_claim_only_one_winner(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=3)
    stores = [GitStateStore(clones[i], f"machine-{i}") for i in range(3)]
    for s in stores:
        s.write_worker_heartbeat(0, 2, False)

    results = []
    barrier = threading.Barrier(3)

    def _try(store):
        barrier.wait()
        ok, job = store.claim_job_lease("job-42", {"id": "job-42", "issue": 42, "stage": "intake"})
        results.append((ok, job))

    threads = [threading.Thread(target=_try, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1]["lease"]["fencing_token"] == 1


def test_skew_margin_prevents_early_takeover_and_allows_expired(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    t0 = 1_700_000_000.0
    s0.write_worker_heartbeat(1, 2, False, now_ts=t0)  # expires_at = t0 + 360
    ok, _ = s0.claim_job_lease("job-7", {"id": "job-7", "issue": 7, "stage": "build"}, now_ts=t0)
    assert ok

    # At t0 + 370 (past 360s TTL, inside 60s skew margin) -> must NOT steal
    ok_early, _ = s1.claim_job_lease("job-7", now_ts=t0 + 370)
    assert ok_early is False

    # At t0 + 430 (past 360 + 60s skew margin) -> MUST steal with incremented fencing_token
    ok_late, job = s1.claim_job_lease("job-7", now_ts=t0 + 430)
    assert ok_late is True
    assert job["lease"]["worker_id"] == "m1"
    assert job["lease"]["fencing_token"] == 2


def test_checkpoint_hydration_and_slice_checkpoint(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    s0.write_worker_heartbeat(1, 2, False)
    jdir0 = tmp_path / "j0"
    jdir0.mkdir()
    (jdir0 / "spec.md").write_text("# Spec\n")
    (jdir0 / "plan.json").write_text(json.dumps({"slices": [{"id": 1}, {"id": 2}]}))
    ok, job = s0.claim_job_lease("job-9", {"id": "job-9", "issue": 9, "stage": "build", "completed_slices": 1})
    assert ok
    s0.checkpoint_job("job-9", str(jdir0), job, release_lease=True)

    # Make a slice commit in clone 0 and push slice checkpoint
    (tmp_path / "worker-0" / "slice1.txt").write_text("done\n")
    subprocess.run(["git", "-C", clones[0], "add", "slice1.txt"], check=True)
    subprocess.run(["git", "-C", clones[0], "commit", "-m", "slice 1"], check=True)
    sha = s0.push_slice_checkpoint("job-9", clones[0])

    s1.write_worker_heartbeat(0, 2, False)
    ok2, _ = s1.claim_job_lease("job-9")
    assert ok2
    jdir1 = tmp_path / "j1"
    hydrated = s1.hydrate_job("job-9", str(jdir1))
    assert hydrated["completed_slices"] == 1
    assert (jdir1 / "spec.md").read_text() == "# Spec\n"
    fetched = s1.fetch_slice_checkpoint("job-9", clones[1])
    assert fetched == sha


def test_staging_lock_control_quota_and_prune(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    s0.write_worker_heartbeat(1, 2, False)
    s1.write_worker_heartbeat(0, 2, False)

    assert s0.acquire_staging_lock(["job-1"]) is True
    assert s1.acquire_staging_lock(["job-2"]) is False
    s0.release_staging_lock()
    assert s1.acquire_staging_lock(["job-2"]) is True

    s0.set_halted(True, reason="maintenance")
    assert s1.get_control_state()["halted"] is True
    s0.set_halted(False)
    assert s1.get_control_state()["halted"] is False

    q = s0.record_quota_cooldown("gemini-4-argon", "429 RESOURCE_EXHAUSTED")
    assert q["models"]["gemini-4-argon"]["consecutive_429s"] == 1
    assert s1.get_quota_state()["models"]["gemini-4-argon"]["consecutive_429s"] == 1
    s1.clear_quota_cooldown("gemini-4-argon")
    assert "gemini-4-argon" not in s0.get_quota_state()["models"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_git_state.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'factory.lib.git_state'`

- [ ] **Step 3: Implement `factory/lib/git_state.py`**

Implement `LeaseLostError` and `GitStateStore` using Git plumbing (`hash-object -w`, `mktree`, `commit-tree`, `cat-file`, `ls-remote`, `fetch`, `push --force-with-lease=<ref>:<old_sha>`).
- Checkpoint files included in `refs/factory/jobs/<job_id>`: `job.json`, `intake.json`, `intake.md`, `spec.json`, `spec.md`, `plan.json`, `qa-failure.md`, and `round-*/slice-*-build.json` / `round-*/review-*.json` (explicitly excluding `logs/`).
- Quota backoff schedule: `[120, 300, 900, 3600]`, honoring `max(scheduled, retry_after_sec or 0)`.
- `ZERO_SHA = "0" * 40` used for `--force-with-lease=<ref>:0000...` when ref does not yet exist on `origin`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_git_state.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add factory/lib/git_state.py tests/test_git_state.py
git commit -m "feat(git-state): add Git-native refs/factory/* leases, checkpoints, locks, control, and quota"
```

---

### Task 2: Integrate Leases, Slice Checkpoints, Kill Switch (`halt`/`resume`) & Fleet `status` into Pipeline & CLI

**Files:**
- Modify: `factory/lib/pipeline.py`
- Modify: `factory/lib/staging_qa.py`
- Modify: `factory/cli.py`
- Test: `tests/test_git_state.py`

**Interfaces:**
- Consumes: `GitStateStore`, `LeaseLostError` from Task 1
- Produces:
  - `check_halt_or_lease(repo_root: str, job_id: str, store: GitStateStore | None) -> None`
  - `run_halt(repos: list[str], reason: str = "", force: bool = False) -> int`
  - `run_resume(repos: list[str]) -> int`
  - CLI subcommands: `factory halt [--reason TEXT] [--force]`, `factory resume`, updated `factory status [--all]` (showing fleet banner, active machines, and `MACHINE` column).

- [ ] **Step 1: Write the failing test for `halt`/`resume` preserving state without `needs-human`**

Append to `tests/test_git_state.py`:

```python
def test_halt_and_resume_preserves_checkpoint_without_needs_human(tmp_path, monkeypatch):
    _, clones = _init_bare_and_clones(tmp_path, n=1)
    s0 = GitStateStore(clones[0], "m0")
    s0.write_worker_heartbeat(1, 2, False)
    ok, job = s0.claim_job_lease("job-55", {"id": "job-55", "issue": 55, "stage": "build", "round": 1, "completed_slices": 2})
    assert ok
    jdir = tmp_path / "j55"
    jdir.mkdir()
    s0.checkpoint_job("job-55", str(jdir), job, release_lease=False)

    from factory import cli
    rc = cli.main(["halt", "--repo", clones[0], "--reason", "lunch break"])
    assert rc == 0
    assert s0.get_control_state()["halted"] is True

    rc2 = cli.main(["resume", "--repo", clones[0]])
    assert rc2 == 0
    assert s0.get_control_state()["halted"] is False
    hydrated = s0.hydrate_job("job-55", str(tmp_path / "j55_after"))
    assert hydrated["stage"] == "build"
    assert hydrated["completed_slices"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_git_state.py::test_halt_and_resume_preserves_checkpoint_without_needs_human -v`
Expected: FAIL (unrecognized arguments / missing subcommands `halt`, `resume`)

- [ ] **Step 3: Wire `GitStateStore` into `pipeline.py`, `staging_qa.py`, and `cli.py`**

- In `factory/lib/pipeline.py`:
  - `process_job`: accept optional `state_store: GitStateStore | None = None`; when present (or auto-created for `run_factory`), hydrate from `refs/factory/jobs/<job_id>` if remote checkpoint is newer, push slice checkpoint + `checkpoint_job` after each completed slice and stage transition, and check `get_control_state()["halted"]` + local `~/.config/factory/halt.signal` between slices/stages. On halt (without incrementing `round` or calling `_escalate_to_human`), reset uncommitted worktree edits, checkpoint with `lease=None, activity="halted"`, and return cleanly.
  - `_execute_staging_train_batch`: acquire `state_store.acquire_staging_lock(...)` around `staging_lock(jobs_dir)` when `state_store` is active, and honor `halt` (finishing in-flight `deploy_to_staging` atomically unless `--force`).
  - `_plan_queue_batch`: check `state_store.get_cached_dag(issue_set_hash)` before calling the Planner LLM and save with `put_cached_dag`.
  - `format_status_table`: add `MACHINE` column populated from `job.get("lease", {}).get("hostname") or "-"`.
- In `factory/cli.py`:
  - Add `halt` (`--reason`, `--force`) and `resume` subcommands, plus `--repo`/`--all` support.
  - Update `status` to fetch `refs/factory/control`, `refs/factory/workers/*`, `refs/factory/quota`, and `refs/factory/jobs/*` (merged with local `factory/jobs/`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_git_state.py tests/test_pipeline_e2e.py tests/test_multi_repo_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add factory/lib/pipeline.py factory/lib/staging_qa.py factory/cli.py tests/test_git_state.py
git commit -m "feat(pipeline): wire distributed leases, slice checkpoints, halt/resume, and fleet status"
```

---

### Task 3: `herdr-core` (`hc`) Bridge & OSC 7501 Status Protocol (`factory/lib/herdr_bridge.py`)

**Files:**
- Create: `factory/lib/herdr_bridge.py`
- Test: `tests/test_worker_and_herdr.py`

**Interfaces:**
- Consumes: `HC_TERMINAL_ID` env var, `hc` binary (`shutil.which("hc")` or `HC_BIN_PATH` override for tests)
- Produces:
  - `HerdrBridge.from_env() -> HerdrBridge`
  - `HerdrBridge.enabled -> bool`
  - `HerdrBridge.emit_status(state: str, *, kind: str = "", progress: int | None = None) -> None` (emits OSC 7501 `\033]7501;state=...[:kind=...][:progress=...]\033\\` to `sys.stdout` when `enabled`)
  - `HerdrBridge.spawn_job_terminal(name: str, cwd: str, cmd: list[str]) -> str | None` (`hc run --bg --name <name> --cwd <cwd> -- <cmd...>`)
  - `HerdrBridge.list_terminals() -> list[str]` (`hc ls`)

- [ ] **Step 1: Write the failing tests in `tests/test_worker_and_herdr.py`**

```python
import io
import sys

from factory.lib.herdr_bridge import HerdrBridge


def test_herdr_bridge_noop_outside_hc(monkeypatch):
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    buf = io.StringIO()
    bridge = HerdrBridge.from_env(stream=buf)
    assert bridge.enabled is False
    bridge.emit_status("working", progress=50)
    assert buf.getvalue() == ""
    assert bridge.spawn_job_terminal("repo-1", "/tmp", ["echo", "hi"]) is None


def test_herdr_bridge_osc7501_and_spawn_job_terminal(tmp_path, monkeypatch):
    log_file = tmp_path / "hc_calls.log"
    fake_bin = tmp_path / "hc"
    fake_bin.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t9')\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_bin))

    buf = io.StringIO()
    bridge = HerdrBridge.from_env(stream=buf)
    assert bridge.enabled is True
    bridge.emit_status("working", progress=40)
    bridge.emit_status("blocked", kind="question")
    tid = bridge.spawn_job_terminal("repo-5", str(tmp_path), ["factory", "_exec-job", "5"])
    assert tid == "t9"

    out = buf.getvalue()
    assert "\033]7501;state=working:progress=40\033\\" in out
    assert "\033]7501;state=blocked:kind=question\033\\" in out
    lines = log_file.read_text().splitlines()
    assert any(ln.startswith("run --bg --name repo-5") and "-- factory _exec-job 5" in ln for ln in lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker_and_herdr.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'factory.lib.herdr_bridge'`

- [ ] **Step 3: Implement `factory/lib/herdr_bridge.py`**

Implement `HerdrBridge` with `threading.Lock`-protected OSC 7501 writes (`emit_status`), `timeout=5.0` `hc run --bg` spawning (swallowing errors cleanly so `hc` hiccups never crash jobs), and no-op fallback when `HC_TERMINAL_ID` is unset.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker_and_herdr.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add factory/lib/herdr_bridge.py tests/test_worker_and_herdr.py
git commit -m "feat(herdr): add HerdrBridge for hc background terminals and OSC 7501 status"
```

---

### Task 4: Continuous Worker Loop, Spread-First Scheduler & `factory fleet` (`worker.py`, `fleet.py`)

**Files:**
- Create: `factory/lib/worker.py`
- Create: `factory/lib/fleet.py`
- Modify: `factory/cli.py`
- Test: `tests/test_worker_and_herdr.py`

**Interfaces:**
- Consumes: `GitStateStore`, `HerdrBridge`, `process_job`, `resolve_target_repos`
- Produces:
  - `HostConfig` (`load_host_config`)
  - `select_next_candidate(...)` (spread-first + warm affinity + `sha256(machine_id:issue)` tie-breaking)
  - `run_worker(repos: list[str], ...) -> int`
  - `exec_job_command(repo_root: str, issue_number: int) -> int`
  - `fleet_status()`, `fleet_wake()`, `fleet_pause()`, `fleet_unpause()`
  - CLI subcommands: `factory worker [--repo | --all]`, `factory _exec-job --repo <path> <issue>`, `factory pause [--drain]`, `factory unpause`, `factory fleet {status,wake,pause,unpause}`

- [ ] **Step 1: Write failing tests for spread-first selection, warm affinity, and `factory fleet` in `tests/test_worker_and_herdr.py`**

Add tests asserting:
1. `select_next_candidate` orders Tier 1 (own interrupted/halted jobs) before Tier 2 (peer orphaned jobs) before Tier 3 (fresh DAG-ready issues), and two different `machine_id`s pick different tie-broken issues for Tier 3.
2. Spread-first pacing enforces `ramp_up_delay_sec` between claims (`_next_claim_allowed_at`).
3. `factory pause` / `factory unpause` toggles `~/.config/factory/paused` (overridable via `FACTORY_HOST_DIR` for tests).
4. `fleet_wake` reads `hc machine ls` (plus optional `fleet.json` fallback) and invokes `hc run --on <machine> --bg --name ... -- factory worker --all`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker_and_herdr.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'factory.lib.worker'`

- [ ] **Step 3: Implement `factory/lib/worker.py`, `factory/lib/fleet.py`, and CLI subcommands in `factory/cli.py`**

- `worker.py`: startup `run_doctor` filtering per repo, background 120s worker heartbeat + 20s `refs/factory/control` watcher + 24h `prune_stale_refs`, spread-first poll loop claiming at most 1 job per `ramp_up_delay_sec`, spawning an `hc run --bg` child terminal (`_exec-job`) when `$HC_TERMINAL_ID` is set or a local thread outside `hc`, and releasing leases cleanly on `SIGTERM`/`SIGINT`/`pause --drain`.
- `exec_job_command`: runs `process_job` with `HerdrBridge`, emits OSC 7501 `state=done` on terminal/halted stages and exits cleanly, or emits OSC 7501 `state=blocked:kind=question` and leaves an interactive rescue shell in `.worktrees/job-<N>` on `needs-human`.
- `fleet.py`: wraps `hc machine ls` + `hc run --on <machine>` (with optional `fleet.json` / `ssh` fallback) for `wake`, `pause`, `unpause`, and `status`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker_and_herdr.py tests/test_multi_repo_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add factory/lib/worker.py factory/lib/fleet.py factory/cli.py tests/test_worker_and_herdr.py
git commit -m "feat(worker): add continuous spread-first worker loop and factory fleet CLI"
```

---

### Task 5: Homelab-Polite Host Resource & Shared LLM Quota Governor (`governor.py`, `agy_runner.py`)

**Files:**
- Create: `factory/lib/governor.py`
- Modify: `factory/lib/agy_runner.py`
- Modify: `factory/lib/pipeline.py`
- Modify: `factory/lib/worker.py`
- Test: `tests/test_governor_and_quota.py`

**Interfaces:**
- Consumes: `HostConfig`, `GitStateStore` (`get_quota_state`, `record_quota_cooldown`, `clear_quota_cooldown`)
- Produces:
  - `QuotaExhaustedError(RuntimeError)` with `model_spec: str` and `retry_after_sec: int | None`
  - `HostResources(free_ram_gb: float, cpu_load_pct: float)`
  - `probe_host_resources() -> HostResources`
  - `check_host_admission(host_cfg: HostConfig, *, resources: HostResources | None = None) -> tuple[bool, str]`

- [ ] **Step 1: Write failing tests in `tests/test_governor_and_quota.py`**

Test:
1. Linux `/proc/meminfo` + `os.getloadavg()` parsing and macOS `vm_stat` parsing fixtures.
2. `check_host_admission` returning `(False, "RAM ...")` when `free_ram_gb < min_free_ram_gb` and `(False, "CPU ...")` when `cpu_load_pct >= max_cpu_load_pct`.
3. `agy_runner.py` raising `QuotaExhaustedError` on `429` / `RESOURCE_EXHAUSTED` output without consuming retry attempt.
4. `pipeline.py` catching `QuotaExhaustedError`, recording backoff into `refs/factory/quota`, checkpointing job with `activity="cooldown: quota"`, and NOT incrementing `round` or escalating to `needs-human`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_governor_and_quota.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'factory.lib.governor'`

- [ ] **Step 3: Implement `governor.py`, `QuotaExhaustedError` in `agy_runner.py`, and wire into `pipeline.py` & `worker.py`**

- `governor.py`: cross-platform Linux/WSL2 (`/proc/meminfo`) and macOS (`vm_stat`) probe + `check_host_admission`.
- `agy_runner.py`: detect rate-limit / `429` / `RESOURCE_EXHAUSTED` / `quota exceeded` patterns in stderr/stdout/JSON envelope and raise `QuotaExhaustedError(model_spec, retry_after_sec)` immediately; on success, notify caller so `clear_quota_cooldown(base_model)` runs.
- `pipeline.py`: between build slices, check `check_host_admission`; if host stays busy > 60s, checkpoint and release lease (`activity="yielded: host busy"`). On `QuotaExhaustedError`, record shared cooldown in `refs/factory/quota` (mirrored to registered repos) and release lease cleanly.
- `worker.py`: gate new claims on `check_host_admission` and `refs/factory/quota` cooldowns, emitting OSC 7501 `state=idle` and logging when throttled or in quota cooldown.

- [ ] **Step 4: Run full `pytest` suite**

Run: `pytest`
Expected: All existing 82 tests + all new multi-machine tests PASS.

- [ ] **Step 5: Commit & push**

```bash
git add factory/lib/governor.py factory/lib/agy_runner.py factory/lib/pipeline.py factory/lib/worker.py tests/test_governor_and_quota.py
git commit -m "feat(governor): add host resource throttling and shared LLM 429 quota backoff"
git push
```
