"""Continuous competing-consumer worker loop, spread-first scheduler, and _exec-job runner."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from factory.lib.config import expand_path_prepend, load_config
from factory.lib.git_ops import GitHubClient
from factory.lib.git_state import SKEW_MARGIN_SEC, TERMINAL_STAGES, GitStateStore
import factory.lib.governor as _governor
from factory.lib.herdr_bridge import HerdrBridge
from factory.lib.pipeline import (
    _plan_queue_batch,
    is_local_halted,
    process_job,
    run_doctor,
)

probe_host_resources = _governor.probe_host_resources
_ORIG_PROBE_HOST_RESOURCES = _governor.probe_host_resources


def host_config_dir() -> str:
    return os.path.expanduser(os.environ.get("FACTORY_HOST_DIR") or os.path.join("~", ".config", "factory"))


@dataclass
class HostConfig:
    machine_id: str
    max_workers: int = 2
    min_free_ram_gb: float = 2.0
    max_cpu_load_pct: float = 85.0
    ramp_up_delay_sec: float = 20.0
    poll_interval_sec: float = 45.0


def load_host_config() -> HostConfig:
    path = os.path.join(host_config_dir(), "host.json")
    data: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except (OSError, ValueError):
            pass
    return HostConfig(
        machine_id=str(data.get("machine_id") or socket.gethostname()),
        max_workers=max(1, int(data.get("max_workers", 2))),
        min_free_ram_gb=float(data.get("min_free_ram_gb", 2.0)),
        max_cpu_load_pct=float(data.get("max_cpu_load_pct", 85.0)),
        ramp_up_delay_sec=float(data.get("ramp_up_delay_sec", 20.0)),
        poll_interval_sec=float(data.get("poll_interval_sec", 45.0)),
    )


def pause_file_path() -> str:
    return os.path.join(host_config_dir(), "paused")


def is_host_paused() -> bool:
    return os.path.isfile(pause_file_path())


def is_host_draining() -> bool:
    path = pause_file_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return isinstance(data, dict) and bool(data.get("paused")) and bool(data.get("drain"))
    except (OSError, ValueError):
        return False


def set_host_paused(paused: bool, *, drain: bool = False) -> None:
    path = pause_file_path()
    if paused:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"paused": True, "drain": bool(drain)}, f)
    elif os.path.isfile(path):
        os.remove(path)


class ClaimPacer:
    """Enforces spread-first ramp-up delay between consecutive job claims on one machine."""

    def __init__(self, ramp_up_delay_sec: float = 20.0) -> None:
        self.ramp_up_delay_sec = max(0.0, float(ramp_up_delay_sec))
        self._next_claim_allowed_at = 0.0

    def can_claim(self, *, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        return ts >= self._next_claim_allowed_at

    def record_claim(self, *, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else now
        self._next_claim_allowed_at = ts + self.ramp_up_delay_sec


def _is_lease_expired(lease: dict, workers: dict[str, dict], now_ts: float) -> bool:
    owner = lease.get("worker_id")
    if not owner:
        return True
    lease_exp = lease.get("expires_at_ts")
    if lease_exp is not None and now_ts > (float(lease_exp) + SKEW_MARGIN_SEC):
        return True
    w = workers.get(owner)
    exp = float((w or {}).get("expires_at_ts") or lease_exp or 0.0)
    return now_ts > (exp + SKEW_MARGIN_SEC)


def _is_active_leased_job(job: dict, workers: dict[str, dict], now_ts: float) -> bool:
    stage = str(job.get("stage") or "")
    if stage in TERMINAL_STAGES or stage in {"needs-human", "needs-info"}:
        return False
    lease = job.get("lease")
    if not isinstance(lease, dict) or not lease.get("worker_id"):
        return False
    return not _is_lease_expired(lease, workers, now_ts)


def _job_key(repo: str, job: dict) -> tuple[str, str]:
    if job.get("issue") is not None:
        return (repo, f"job-{int(job['issue'])}")
    return (repo, str(job.get("id") or ""))


def _collect_active_local_jobs(
    machine_id: str,
    stores: dict[str, GitStateStore],
    active_threads: list[tuple[str, int, threading.Thread]],
    *,
    now_ts: float | None = None,
    snapshots: dict[str, tuple[list[dict], dict[str, dict]]] | None = None,
) -> set[tuple[str, str]]:
    ts = time.time() if now_ts is None else now_ts
    active: set[tuple[str, str]] = {(r, f"job-{i}") for (r, i, t) in active_threads if t.is_alive()}
    for repo, store in stores.items():
        try:
            remote_jobs = store.list_remote_jobs()
            workers = store.list_worker_heartbeats()
        except Exception:
            continue
        if snapshots is not None:
            snapshots[repo] = (remote_jobs, workers)
        for j in remote_jobs:
            if _is_active_leased_job(j, workers, ts) and (j.get("lease") or {}).get("worker_id") == machine_id:
                active.add(_job_key(repo, j))
    return active


def select_next_candidate(
    machine_id: str,
    remote_jobs: list[dict],
    ready_issues: list[dict],
    workers: dict[str, dict],
    *,
    now_ts: float | None = None,
) -> tuple[str, int, dict] | None:
    """Select next job/issue using Tier 1 (own) > Tier 2 (orphaned) > Tier 3 (spread-first DAG)."""
    ts = time.time() if now_ts is None else now_ts
    tier1: list[dict] = []
    tier2: list[dict] = []
    known_issues: set[int] = set()

    ready_issue_nums = {int(iss["number"]) for iss in ready_issues if iss.get("number") is not None}

    for j in remote_jobs:
        issue_num = j.get("issue")
        if issue_num is not None:
            known_issues.add(int(issue_num))
        stage = str(j.get("stage") or "")
        # Re-applying the queue label on GitHub puts the issue back into ready_issues;
        # allow resuming needs-human/needs-info jobs when they are explicitly re-queued.
        if stage in TERMINAL_STAGES or (
            stage in {"needs-human", "needs-info"}
            and (issue_num is None or int(issue_num) not in ready_issue_nums)
        ):
            continue
        lease = j.get("lease")
        if lease is None:
            if j.get("last_worker_id") == machine_id:
                tier1.append(j)
            else:
                tier2.append(j)
        elif isinstance(lease, dict):
            if _is_lease_expired(lease, workers, ts):
                if lease.get("worker_id") == machine_id:
                    tier1.append(j)
                else:
                    tier2.append(j)

    if tier1:
        job = sorted(tier1, key=lambda x: int(x.get("issue") or 0))[0]
        return ("resume", int(job["issue"]), job)
    if tier2:
        def _tier2_key(j: dict) -> tuple[int, int]:
            prev_owner = j.get("last_worker_id") or (j.get("lease") or {}).get("worker_id")
            owner_w = workers.get(prev_owner) if prev_owner else None
            owner_online = 1 if (owner_w and ts <= float(owner_w.get("expires_at_ts") or 0.0)) else 0
            return (owner_online, int(j.get("issue") or 0))

        job = sorted(tier2, key=_tier2_key)[0]
        return ("resume", int(job["issue"]), job)

    fresh = [iss for iss in ready_issues if int(iss["number"]) not in known_issues]
    if not fresh:
        return None
    fresh.sort(
        key=lambda iss: hashlib.sha256(f"{machine_id}:{int(iss['number'])}".encode("utf-8")).hexdigest()
    )
    chosen = fresh[0]
    return ("new", int(chosen["number"]), chosen)


def exec_job_command(repo_root: str, issue_number: int) -> int:
    """Execute one factory job inside an hc child terminal or local worker thread."""
    repo_root = os.path.abspath(repo_root)
    try:
        repo_cfg = load_config(os.path.join(repo_root, "factory"))
    except Exception:
        repo_cfg = None
    path_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    prepend = [
        b
        for b in expand_path_prepend(repo_cfg)
        if os.path.isdir(b) and b not in path_parts
    ]
    if prepend:
        os.environ["PATH"] = os.pathsep.join([*prepend, *path_parts])
    host_cfg = load_host_config()
    bridge = HerdrBridge.from_env()
    store = GitStateStore(repo_root, host_cfg.machine_id)
    bridge.emit_status("working", progress=5)

    job_id = f"job-{issue_number}"
    lease_stop = threading.Event()

    def _lease_heartbeat() -> None:
        while not lease_stop.wait(120.0):
            try:
                store.renew_job_lease(job_id)
            except Exception:
                pass

    hb_thread = threading.Thread(target=_lease_heartbeat, daemon=True)
    hb_thread.start()
    try:
        job = process_job(repo_root, issue_number, state_store=store)
    finally:
        lease_stop.set()
        hb_thread.join(timeout=1.0)
    stage = str(job.get("stage") or "")
    if stage == "needs-human":
        bridge.emit_status("blocked", kind="question")
        wt = os.path.join(repo_root, ".worktrees", job.get("id", f"job-{issue_number}"))
        print(f"\n[needs-human] {job.get('id')} paused for human intervention.")
        print(f"Worktree: {wt}")
        if job.get("last_error"):
            print(f"Reason: {job['last_error']}")
        if not bridge.enabled and os.environ.get("FACTORY_INTERACTIVE_SHELL") == "1" and sys.stdin.isatty() and os.path.isdir(wt):
            shell = os.environ.get("SHELL") or "/bin/bash"
            subprocess.run([shell], cwd=wt, check=False)
        return 2

    bridge.emit_status("done")
    return 0


def run_worker(
    repos: list[str],
    *,
    once: bool = False,
) -> int:
    """Run continuous competing-consumer worker loop across target repositories."""
    host_cfg = load_host_config()
    bridge = HerdrBridge.from_env()
    pacer = ClaimPacer(host_cfg.ramp_up_delay_sec)
    stop_event = threading.Event()

    healthy_repos: list[str] = []
    for repo in repos:
        ok, msgs = run_doctor(repo)
        if ok:
            healthy_repos.append(os.path.abspath(repo))
        else:
            print(f"[worker] skipping unhealthy repo {repo}: {'; '.join(msgs)}", file=sys.stderr)

    if not healthy_repos:
        print("[worker] no healthy repositories to watch.", file=sys.stderr)
        return 1

    stores = {r: GitStateStore(r, host_cfg.machine_id) for r in healthy_repos}
    active_threads: list[tuple[str, int, threading.Thread]] = []
    active_hc_terminals: dict[tuple[str, int], str] = {}

    def _handle_sig(_signum, _frame):
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _handle_sig)
        signal.signal(signal.SIGTERM, _handle_sig)

    def _write_heartbeats(active_count: int) -> None:
        probe_fn = (
            probe_host_resources
            if probe_host_resources is not _ORIG_PROBE_HOST_RESOURCES
            else _governor.probe_host_resources
        )
        res = probe_fn()
        paused = is_host_paused()
        for store in stores.values():
            try:
                store.write_worker_heartbeat(
                    active_count,
                    host_cfg.max_workers,
                    paused,
                    cpu_load_pct=res.cpu_load_pct,
                    free_ram_gb=res.free_ram_gb,
                )
            except Exception:
                pass

    last_hb: float | None = None
    last_prune: float | None = None
    while not stop_event.is_set():
        active_threads = [(r, i, t) for (r, i, t) in active_threads if t.is_alive()]
        if bridge.enabled and active_hc_terminals:
            try:
                live_names = bridge.list_live_terminal_names()
                active_hc_terminals = {
                    k: name for k, name in active_hc_terminals.items() if name in live_names
                }
            except Exception:
                pass
        repo_snapshots: dict[str, tuple[list[dict], dict[str, dict]]] = {}
        active_local_jobs = _collect_active_local_jobs(
            host_cfg.machine_id,
            stores,
            active_threads,
            snapshots=repo_snapshots,
        )
        for repo_key, issue_key in active_hc_terminals:
            active_local_jobs.add((repo_key, f"job-{issue_key}"))
        now = time.monotonic()

        def _spawn_job(repo_path: str, issue_num: int) -> None:
            job_id = f"job-{issue_num}"
            active_local_jobs.add((repo_path, job_id))
            bridge.emit_status("working")
            name = f"{os.path.basename(repo_path)}-{issue_num}"
            if bridge.enabled:
                factory_bin = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                    "bin",
                    "factory",
                )
                exec_cmd = (
                    [factory_bin]
                    if os.path.isfile(factory_bin) and os.access(factory_bin, os.X_OK)
                    else [sys.executable, "-m", "factory.cli"]
                )
                tid = bridge.spawn_job_terminal(
                    name,
                    repo_path,
                    [*exec_cmd, "_exec-job", "--repo", repo_path, str(issue_num)],
                )
                if tid:
                    from factory.lib.herdr_bridge import sanitize_hc_name

                    active_hc_terminals[(repo_path, issue_num)] = sanitize_hc_name(name)
                    return

            t = threading.Thread(target=exec_job_command, args=(repo_path, issue_num), daemon=True)
            t.start()
            active_threads.append((repo_path, issue_num, t))

        if last_hb is None or now - last_hb >= 120.0:
            _write_heartbeats(len(active_local_jobs))
            last_hb = now

        if last_prune is None or now - last_prune >= 24 * 3600.0:
            for store in stores.values():
                try:
                    store.prune_stale_refs()
                except Exception:
                    pass
            last_prune = now

        if is_local_halted() or is_host_paused():
            bridge.emit_status("idle")
            if once:
                break
            stop_event.wait(min(host_cfg.poll_interval_sec, 20.0))
            continue

        # Check host admission and shared quota if governor is available
        try:
            from factory.lib.governor import check_host_admission
            admitted, _reason = check_host_admission(host_cfg)
        except ImportError:
            admitted = True

        if not admitted or len(active_local_jobs) >= host_cfg.max_workers or not pacer.can_claim():
            bridge.emit_status("working" if active_local_jobs else "idle")
            if once:
                break
            stop_event.wait(5.0)
            continue

        claimed_any = False
        for repo in healthy_repos:
            if len(active_local_jobs) >= host_cfg.max_workers or not pacer.can_claim():
                break
            try:
                store = stores[repo]
                if store.get_control_state().get("halted"):
                    continue
                try:
                    from factory.lib.pipeline import reconcile_ready_jobs

                    reconcile_ready_jobs(
                        repo,
                        state_store=store,
                        spawn_fn=lambda _r, inum: _spawn_job(repo, inum),
                    )
                except Exception:
                    pass

                cfg = load_config(os.path.join(repo, "factory"))
                try:
                    from factory.lib.config import model_chain
                    from factory.lib.governor import is_model_in_cooldown

                    quota_state = store.get_quota_state()
                    in_cd = False
                    for role in ("intake", "spec_writer", "builder"):
                        chain = model_chain(cfg, role)
                        if not chain:
                            continue
                        if all(is_model_in_cooldown(quota_state, m, config=cfg)[0] for m in chain):
                            in_cd = True
                            break
                    if in_cd:
                        continue
                except Exception:
                    pass

                if repo in repo_snapshots:
                    remote_jobs, workers = repo_snapshots[repo]
                else:
                    remote_jobs = store.list_remote_jobs()
                    workers = store.list_worker_heartbeats()
                now_ts = time.time()
                for j in remote_jobs:
                    if _is_active_leased_job(j, workers, now_ts) and (j.get("lease") or {}).get("worker_id") == host_cfg.machine_id:
                        active_local_jobs.add(_job_key(repo, j))
                if len(active_local_jobs) >= host_cfg.max_workers:
                    break

                active_repo_leases = sum(
                    1 for j in remote_jobs if _is_active_leased_job(j, workers, now_ts)
                )
                if active_repo_leases >= int(cfg["concurrency"]):
                    continue

                gh = GitHubClient(repo)
                issues = gh.list_queue_issues(cfg)

                done_issues = {int(j["issue"]) for j in remote_jobs if j.get("stage") == "merged" and j.get("issue")}
                order_deps, hard_deps = _plan_queue_batch(
                    repo,
                    os.path.join(repo, "factory"),
                    os.path.join(repo, "factory", "jobs"),
                    cfg,
                    issues,
                    state_store=store,
                ) if issues else ({}, {})
                ready = [
                    iss
                    for iss in issues
                    if all(
                        dep in done_issues
                        for dep in (
                            order_deps.get(int(iss["number"]), set())
                            | hard_deps.get(int(iss["number"]), set())
                        )
                    )
                ]

                local_running_issues = {
                    i for (r, i, t) in active_threads if r == repo and t.is_alive()
                } | {
                    i for (r, i) in active_hc_terminals if r == repo
                }
                filtered_remote_jobs = [
                    j for j in remote_jobs if int(j.get("issue") or -1) not in local_running_issues
                ]
                filtered_ready = [
                    iss for iss in ready if int(iss["number"]) not in local_running_issues
                ]
                cand = select_next_candidate(
                    host_cfg.machine_id,
                    filtered_remote_jobs,
                    filtered_ready,
                    workers,
                    now_ts=now_ts,
                )
                if not cand:
                    continue

                kind, issue_num, payload = cand
                job_id = f"job-{issue_num}"
                initial = (
                    None
                    if kind == "resume"
                    else {"id": job_id, "issue": issue_num, "stage": "intake", "last_worker_id": host_cfg.machine_id}
                )
                ok, _ = store.claim_job_lease(job_id, initial)
                if not ok:
                    continue

                pacer.record_claim()
                claimed_any = True
                _spawn_job(repo, issue_num)
            except Exception as exc:
                print(f"[worker] warning: poll error for {repo}: {exc}", file=sys.stderr)
                continue

        if claimed_any:
            _write_heartbeats(len(active_local_jobs))
            last_hb = time.monotonic()
        else:
            bridge.emit_status("working" if active_local_jobs else "idle")
        if once:
            for _, _, t in active_threads:
                t.join()
            break
        wait_sec = host_cfg.ramp_up_delay_sec if claimed_any else host_cfg.poll_interval_sec
        stop_event.wait(wait_sec)

    if not once:
        for store in stores.values():
            try:
                store.delete_worker_heartbeat()
            except Exception:
                pass
    bridge.emit_status("idle")
    return 0
