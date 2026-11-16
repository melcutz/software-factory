import json
import subprocess
import threading

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


def test_halt_and_resume_preserves_checkpoint_without_needs_human(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path / "host_cfg"))
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


def _setup_factory_clone(clone_path: str) -> None:
    import os

    fdir = os.path.join(clone_path, "factory")
    os.makedirs(os.path.join(fdir, "jobs"), exist_ok=True)
    with open(os.path.join(fdir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "base_branch": "main",
                "auto_merge": "all",
                "auto_polish": False,
                "max_rounds": 3,
                "max_slice_retries": 1,
            },
            f,
        )
    checks_sh = os.path.join(fdir, "checks.sh")
    with open(checks_sh, "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(checks_sh, 0o755)


class _DummyGH:
    def __init__(self, issues: dict[int, dict] | None = None):
        self.issues = issues or {}
        self.prs: dict[int, dict] = {}

    def fetch_issue(self, number: int):
        iss = self.issues.get(
            int(number),
            {
                "number": int(number),
                "title": f"Issue {number}",
                "body": f"Body {number}",
                "url": f"https://example.test/issues/{number}",
            },
        )
        return iss, f"# Issue {number}\n\n{iss.get('body', '')}"

    def list_queue_issues(self, _cfg):
        return [iss for _, iss in sorted(self.issues.items())]

    def edit_labels(self, *_a, **_kw):
        pass

    def comment_issue(self, *_a, **_kw):
        pass

    def close_issue(self, *_a, **_kw):
        pass

    def ensure_draft_pr(self, _wt, branch, _base, title, body, pr_number=None):
        num = pr_number or (len(self.prs) + 100)
        url = f"https://example.test/pr/{num}"
        self.prs[num] = {"number": num, "url": url, "branch": branch, "title": title, "body": body, "merged": False}
        return num, url

    def update_pr(self, *_a, **_kw):
        pass

    def mark_pr_ready(self, *_a, **_kw):
        pass

    def merge_pr(self, pr_num: int, _method: str):
        if pr_num in self.prs:
            self.prs[pr_num]["merged"] = True

    def get_pr_merge_commit(self, pr_num: int):
        if self.prs.get(pr_num, {}).get("merged"):
            return "deadbeef1234"
        return None


def test_peer_takeover_hydrates_before_init_across_needs_human_review_mid_slice_and_lease_stub(tmp_path, monkeypatch):
    import os
    from factory.lib.git_ops import ensure_worktree
    from factory.lib.pipeline import process_job

    _, clones = _init_bare_and_clones(tmp_path, n=2)
    for c in clones:
        _setup_factory_clone(c)

    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    s0.write_worker_heartbeat(1, 2, False)
    s1.write_worker_heartbeat(1, 2, False)
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)

    steps_called: list[tuple[str, str, str]] = []

    def fake_run_agent(_fdir, _jdir, worktree, step, role, _model, prompt, *_rest, **kwargs):
        assert callable(kwargs.get("abort_check"))
        m_id = "unknown"
        for line in prompt.splitlines():
            if line.startswith("JOB_ID:"):
                m_id = line.split(":", 1)[1].strip()
                break
        steps_called.append((m_id, step, role))
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if step == "intake":
            return (
                {
                    "decision": "ACCEPT",
                    "risk": "low",
                    "surfaces": [],
                    "summary": ["ok"],
                    "scope_in": "in",
                    "scope_out": "out",
                    "questions": [],
                    "reasoning": "ok",
                },
                usage,
                0.1,
            )
        if step == "spec":
            return (
                {
                    "summary": ["spec"],
                    "problem": "p",
                    "solution": "s",
                    "user_stories": ["u"],
                    "implementation_decisions": ["d"],
                    "testing_decisions": ["t"],
                    "glossary_updates": [],
                    "out_of_scope": [],
                    "slices": [
                        {
                            "id": "s1",
                            "title": "Slice 1",
                            "goal": "g1",
                            "files": ["f1.py"],
                            "test_seams": ["t1"],
                            "acceptance_criteria": ["ac1"],
                        }
                    ],
                },
                usage,
                0.1,
            )
        if step == "build":
            fname = f"built_{m_id}_{len(steps_called)}.txt"
            with open(os.path.join(worktree, fname), "w", encoding="utf-8") as f:
                f.write("done\n")
            subprocess.run(["git", "-C", worktree, "add", fname], check=True)
            subprocess.run(["git", "-C", worktree, "commit", "-m", f"build {fname}"], check=True)
            return (
                {
                    "verdict": "DONE",
                    "summary": [f"Built {fname}"],
                    "changes": [fname],
                    "tests_added": ["t"],
                    "blocked_reason": "",
                    "notes": "",
                },
                usage,
                0.1,
            )
        if step == "review":
            return ({"verdict": "PASS", "summary": ["ok"], "findings": [], "checked": ["all"]}, usage, 0.1)
        if step == "approve":
            return (
                {
                    "decision": "APPROVE",
                    "summary": ["ok"],
                    "reasoning": "ok",
                    "merge_danger": "none",
                    "question": "",
                },
                usage,
                0.1,
            )
        raise AssertionError(f"unexpected step {step}")

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)
    gh = _DummyGH()

    # 1) Fresh 4-field worker lease stub hydrated on Machine B without KeyError: 'round'
    ok_stub, _ = s1.claim_job_lease(
        "job-101",
        {"id": "job-101", "issue": 101, "stage": "intake", "last_worker_id": "m1"},
    )
    assert ok_stub
    res_stub = process_job(clones[1], 101, gh=gh, state_store=s1)
    assert res_stub["stage"] == "merged"
    assert res_stub.get("lease") is None

    # 2) Mid-slice build takeover: Machine A checkpoints slice 1/2, Machine B takes over slice 2/2
    jdir_102 = os.path.join(clones[0], "factory", "jobs", "job-102")
    os.makedirs(os.path.join(jdir_102, "round-1"), exist_ok=True)
    with open(os.path.join(jdir_102, "intake.json"), "w", encoding="utf-8") as f:
        json.dump({"decision": "ACCEPT", "risk": "low", "surfaces": [], "summary": ["ok"]}, f)
    with open(os.path.join(jdir_102, "spec.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": ["spec"], "problem": "p", "solution": "s"}, f)
    with open(os.path.join(jdir_102, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# Spec 102\n")
    with open(os.path.join(jdir_102, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "slices": [
                    {"id": "s1", "title": "S1", "goal": "g1", "files": ["s1.py"], "test_seams": ["t1"], "acceptance_criteria": ["a1"]},
                    {"id": "s2", "title": "S2", "goal": "g2", "files": ["s2.py"], "test_seams": ["t2"], "acceptance_criteria": ["a2"]},
                ]
            },
            f,
        )
    with open(os.path.join(jdir_102, "round-1", "slice-1-build.json"), "w", encoding="utf-8") as f:
        json.dump({"verdict": "DONE", "summary": ["Slice 1 done"], "changes": ["s1.py"], "tests_added": ["t1"], "blocked_reason": "", "notes": ""}, f)
    with open(os.path.join(jdir_102, "round-1", "slice-1-build.md"), "w", encoding="utf-8") as f:
        f.write("VERDICT: DONE\n\nSlice 1 done\n")

    wt0_102, _ = ensure_worktree(clones[0], "job-102", "main", branch="factory/job-102")
    with open(os.path.join(wt0_102, "s1.py"), "w", encoding="utf-8") as f:
        f.write("S1 = 1\n")
    subprocess.run(["git", "-C", wt0_102, "add", "s1.py"], check=True)
    subprocess.run(["git", "-C", wt0_102, "commit", "-m", "slice 1 commit"], check=True)
    s0.push_slice_checkpoint("job-102", wt0_102)
    ok_102, job_102 = s0.claim_job_lease(
        "job-102",
        {
            "id": "job-102",
            "issue": 102,
            "branch": "factory/job-102",
            "stage": "build",
            "round": 1,
            "round_limit": 3,
            "risk": "low",
            "surfaces": [],
            "total_slices": 2,
            "completed_slices": 1,
        },
    )
    assert ok_102
    s0.checkpoint_job("job-102", jdir_102, job_102, release_lease=True)

    assert s1.claim_job_lease("job-102")[0]
    steps_before = len(steps_called)
    res_102 = process_job(clones[1], 102, gh=gh, state_store=s1)
    assert res_102["stage"] == "merged"
    assert res_102["round"] == 1
    assert res_102["completed_slices"] == 2
    assert res_102.get("lease") is None
    job102_steps = [s for (jid, s, _) in steps_called[steps_before:] if jid == "job-102"]
    assert job102_steps == ["build", "review"]
    wt1_102 = os.path.join(clones[1], ".worktrees", "job-102")
    assert not os.path.exists(wt1_102)
    assert subprocess.run(["git", "-C", clones[1], "cat-file", "-e", "factory/job-102:s1.py"]).returncode == 0

    # 3) Review stage takeover with custom branch name and remote branch fallback
    jdir_103 = os.path.join(clones[0], "factory", "jobs", "job-103")
    os.makedirs(os.path.join(jdir_103, "round-1"), exist_ok=True)
    for fn, data in [
        ("intake.json", {"decision": "ACCEPT", "risk": "low", "surfaces": [], "summary": ["ok"]}),
        ("spec.json", {"summary": ["spec"], "problem": "p", "solution": "s"}),
        ("plan.json", {"slices": [{"id": "s1", "title": "S1"}]}),
        ("round-1/build.json", {"verdict": "DONE", "summary": ["done"], "changes": ["r.py"], "tests_added": ["t"], "blocked_reason": "", "notes": ""}),
    ]:
        with open(os.path.join(jdir_103, fn), "w", encoding="utf-8") as f:
            json.dump(data, f)
    with open(os.path.join(jdir_103, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# Spec 103\n")
    with open(os.path.join(jdir_103, "round-1", "build.md"), "w", encoding="utf-8") as f:
        f.write("VERDICT: DONE\n")
    with open(os.path.join(jdir_103, "round-1", "checks.md"), "w", encoding="utf-8") as f:
        f.write("VERDICT: PASS\n")
    wt0_103, _ = ensure_worktree(clones[0], "job-103", "main", branch="factory/job-103-custom")
    with open(os.path.join(wt0_103, "r.py"), "w", encoding="utf-8") as f:
        f.write("R = 1\n")
    subprocess.run(["git", "-C", wt0_103, "add", "r.py"], check=True)
    subprocess.run(["git", "-C", wt0_103, "commit", "-m", "review branch commit"], check=True)
    subprocess.run(["git", "-C", wt0_103, "push", "origin", "HEAD:refs/heads/factory/job-103-custom"], check=True)
    gh.ensure_draft_pr(wt0_103, "factory/job-103-custom", "main", "Issue 103", "body", pr_number=103)
    ok_103, job_103 = s0.claim_job_lease(
        "job-103",
        {
            "id": "job-103",
            "issue": 103,
            "branch": "factory/job-103-custom",
            "stage": "review",
            "round": 1,
            "round_limit": 3,
            "risk": "low",
            "surfaces": [],
            "total_slices": 1,
            "completed_slices": 1,
            "pr_number": 103,
            "pr_url": "https://example.test/pr/103",
        },
    )
    assert ok_103
    s0.checkpoint_job("job-103", jdir_103, job_103, release_lease=True)

    assert s1.claim_job_lease("job-103")[0]
    steps_before = len(steps_called)
    res_103 = process_job(clones[1], 103, gh=gh, state_store=s1)
    assert res_103["stage"] == "merged"
    assert res_103["branch"] == "factory/job-103-custom"
    assert res_103["round"] == 1
    job103_steps = [s for (jid, s, _) in steps_called[steps_before:] if jid == "job-103"]
    assert job103_steps == ["review"]

    # 4) needs-human takeover on Machine B preserves round and escalated context
    jdir_104 = os.path.join(clones[0], "factory", "jobs", "job-104")
    os.makedirs(os.path.join(jdir_104, "round-1"), exist_ok=True)
    for fn, data in [
        ("intake.json", {"decision": "ACCEPT", "risk": "low", "surfaces": [], "summary": ["ok"]}),
        ("spec.json", {"summary": ["spec"], "problem": "p", "solution": "s"}),
        ("plan.json", {"slices": [{"id": "s1", "title": "S1"}]}),
    ]:
        with open(os.path.join(jdir_104, fn), "w", encoding="utf-8") as f:
            json.dump(data, f)
    with open(os.path.join(jdir_104, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# Spec 104\n")
    with open(os.path.join(jdir_104, "round-1", "approve.md"), "w", encoding="utf-8") as f:
        f.write("DECISION: ESCALATE\n\nNeed human sign-off\n")
    wt0_104, _ = ensure_worktree(clones[0], "job-104", "main", branch="factory/job-104")
    with open(os.path.join(wt0_104, "nh.py"), "w", encoding="utf-8") as f:
        f.write("NH = 1\n")
    subprocess.run(["git", "-C", wt0_104, "add", "nh.py"], check=True)
    subprocess.run(["git", "-C", wt0_104, "commit", "-m", "round 1 commit"], check=True)
    s0.push_slice_checkpoint("job-104", wt0_104)
    ok_104, job_104 = s0.claim_job_lease(
        "job-104",
        {
            "id": "job-104",
            "issue": 104,
            "branch": "factory/job-104",
            "stage": "needs-human",
            "escalated_from": "approve",
            "last_error": "Approver requested human review",
            "round": 1,
            "round_limit": 3,
            "risk": "low",
            "surfaces": [],
            "total_slices": 1,
            "completed_slices": 1,
        },
    )
    assert ok_104
    s0.checkpoint_job("job-104", jdir_104, job_104, release_lease=True)

    assert s1.claim_job_lease("job-104")[0]
    steps_before = len(steps_called)
    res_104 = process_job(clones[1], 104, gh=gh, state_store=s1)
    assert res_104["stage"] == "merged"
    assert res_104["round"] == 2
    job104_steps = [s for (jid, s, _) in steps_called[steps_before:] if jid == "job-104"]
    assert job104_steps == ["build", "review", "approve"]


def test_mid_slice_yield_on_host_overload_and_pause_drain(tmp_path, monkeypatch):
    import os
    from factory.lib.governor import HostResources
    from factory.lib.pipeline import process_job
    from factory.lib.worker import is_host_draining, set_host_paused

    host_dir = tmp_path / "host_cfg"
    host_dir.mkdir()
    monkeypatch.setenv("FACTORY_HOST_DIR", str(host_dir))
    assert is_host_draining() is False
    set_host_paused(True, drain=False)
    assert is_host_draining() is False
    set_host_paused(True, drain=True)
    assert is_host_draining() is True
    set_host_paused(False)
    assert is_host_draining() is False

    _, clones = _init_bare_and_clones(tmp_path, n=2)
    for c in clones:
        _setup_factory_clone(c)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    s0.write_worker_heartbeat(1, 2, False)
    s1.write_worker_heartbeat(1, 2, False)
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)

    overload_active = {"busy": False, "drain": False}

    def fake_probe():
        if overload_active["busy"]:
            return HostResources(free_ram_gb=0.5, cpu_load_pct=95.0)
        return HostResources(free_ram_gb=16.0, cpu_load_pct=10.0)

    monkeypatch.setattr("factory.lib.governor.probe_host_resources", fake_probe)

    def fake_run_agent(_fdir, _jdir, worktree, step, _role, _model, prompt, *_rest, **_kw):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if step == "intake":
            return (
                {
                    "decision": "ACCEPT",
                    "risk": "low",
                    "surfaces": [],
                    "summary": ["ok"],
                    "scope_in": "in",
                    "scope_out": "out",
                    "questions": [],
                    "reasoning": "ok",
                },
                usage,
                0.1,
            )
        if step == "spec":
            return (
                {
                    "summary": ["spec"],
                    "problem": "p",
                    "solution": "s",
                    "user_stories": ["u"],
                    "implementation_decisions": ["d"],
                    "testing_decisions": ["t"],
                    "glossary_updates": [],
                    "out_of_scope": [],
                    "slices": [
                        {"id": "s1", "title": "S1", "goal": "g1", "files": ["a.py"], "test_seams": ["t1"], "acceptance_criteria": ["ac1"]},
                        {"id": "s2", "title": "S2", "goal": "g2", "files": ["b.py"], "test_seams": ["t2"], "acceptance_criteria": ["ac2"]},
                    ],
                },
                usage,
                0.1,
            )
        if step == "build":
            if "SLICE_INDEX: 1/2" in prompt:
                if overload_active.get("mode") == "busy":
                    overload_active["busy"] = True
                elif overload_active.get("mode") == "drain":
                    set_host_paused(True, drain=True)
                fname = "slice1.py"
            else:
                fname = "slice2.py"
            with open(os.path.join(worktree, fname), "w", encoding="utf-8") as f:
                f.write("x = 1\n")
            subprocess.run(["git", "-C", worktree, "add", fname], check=True)
            subprocess.run(["git", "-C", worktree, "commit", "-m", f"add {fname}"], check=True)
            return (
                {
                    "verdict": "DONE",
                    "summary": [f"Added {fname}"],
                    "changes": [fname],
                    "tests_added": ["t"],
                    "blocked_reason": "",
                    "notes": "",
                },
                usage,
                0.1,
            )
        if step == "review":
            return ({"verdict": "PASS", "summary": ["ok"], "findings": [], "checked": ["all"]}, usage, 0.1)
        raise AssertionError(f"unexpected step {step}")

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)
    gh = _DummyGH()

    # Case 1: Host becomes overloaded during Slice 1/2 -> yields after Slice 1
    overload_active["mode"] = "busy"
    s0.claim_job_lease("job-110", {"id": "job-110", "issue": 110, "stage": "intake", "last_worker_id": "m0"})
    yielded = process_job(clones[0], 110, gh=gh, state_store=s0)
    assert yielded["stage"] == "build"
    assert yielded["round"] == 1
    assert yielded["completed_slices"] == 1
    assert yielded["activity"] == "yielded: host busy"
    assert yielded.get("lease") is None
    remote_110 = s1.hydrate_job("job-110", str(tmp_path / "check_110"))
    assert remote_110["activity"] == "yielded: host busy"
    assert remote_110["lease"] is None
    assert remote_110["completed_slices"] == 1

    # Peer m1 claims yielded job-110 and finishes Slice 2/2
    overload_active["busy"] = False
    overload_active["mode"] = ""
    assert s1.claim_job_lease("job-110")[0]
    done_110 = process_job(clones[1], 110, gh=gh, state_store=s1)
    assert done_110["stage"] == "merged"
    assert done_110["round"] == 1
    assert done_110["completed_slices"] == 2

    # Case 2: Host enters pause --drain during Slice 1/2 -> pauses after Slice 1
    overload_active["mode"] = "drain"
    s0.claim_job_lease("job-111", {"id": "job-111", "issue": 111, "stage": "intake", "last_worker_id": "m0"})
    drained = process_job(clones[0], 111, gh=gh, state_store=s0)
    assert drained["stage"] == "build"
    assert drained["round"] == 1
    assert drained["completed_slices"] == 1
    assert drained["activity"] == "paused: draining"
    assert drained.get("lease") is None
    set_host_paused(False)


def test_run_factory_coordinates_leases_with_origin_and_skips_active_peer_lease(tmp_path, monkeypatch):
    import os
    from factory.lib.pipeline import run_factory

    _, clones = _init_bare_and_clones(tmp_path, n=2)
    for c in clones:
        _setup_factory_clone(c)

    peer_store = GitStateStore(clones[0], "peer-a")
    peer_store.write_worker_heartbeat(1, 2, False)
    # Peer-a holds an active lease on issue 201
    ok_peer, _ = peer_store.claim_job_lease(
        "job-201",
        {"id": "job-201", "issue": 201, "stage": "build", "last_worker_id": "peer-a"},
    )
    assert ok_peer

    host_dir = tmp_path / "host_m1"
    host_dir.mkdir()
    (host_dir / "host.json").write_text(json.dumps({"machine_id": "m1"}), encoding="utf-8")
    monkeypatch.setenv("FACTORY_HOST_DIR", str(host_dir))
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)

    gh = _DummyGH(
        issues={
            201: {"number": 201, "title": "Leased by peer", "body": "b1", "url": "https://example.test/issues/201"},
            202: {"number": 202, "title": "Unleased issue", "body": "b2", "url": "https://example.test/issues/202"},
        }
    )
    monkeypatch.setattr("factory.lib.pipeline.GitHubClient", lambda _repo: gh)

    def fake_run_agent(_fdir, _jdir, worktree, step, _role, _model, _prompt, *_rest, **_kw):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if step == "intake":
            return (
                {
                    "decision": "ACCEPT",
                    "risk": "low",
                    "surfaces": [],
                    "summary": ["ok"],
                    "scope_in": "in",
                    "scope_out": "out",
                    "questions": [],
                    "reasoning": "ok",
                },
                usage,
                0.1,
            )
        if step == "spec":
            return (
                {
                    "summary": ["spec"],
                    "problem": "p",
                    "solution": "s",
                    "user_stories": ["u"],
                    "implementation_decisions": ["d"],
                    "testing_decisions": ["t"],
                    "glossary_updates": [],
                    "out_of_scope": [],
                    "slices": [{"id": "s1", "title": "S1", "goal": "g1", "files": ["f.py"], "test_seams": ["t"], "acceptance_criteria": ["ac"]}],
                },
                usage,
                0.1,
            )
        if step == "build":
            with open(os.path.join(worktree, "f.py"), "w", encoding="utf-8") as f:
                f.write("F = 1\n")
            subprocess.run(["git", "-C", worktree, "add", "f.py"], check=True)
            subprocess.run(["git", "-C", worktree, "commit", "-m", "add f.py"], check=True)
            return (
                {"verdict": "DONE", "summary": ["ok"], "changes": ["f.py"], "tests_added": ["t"], "blocked_reason": "", "notes": ""},
                usage,
                0.1,
            )
        if step == "review":
            return ({"verdict": "PASS", "summary": ["ok"], "findings": [], "checked": ["all"]}, usage, 0.1)
        raise AssertionError(f"unexpected step {step}")

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)

    # Queue run on clone 1: skips 201 (leased by peer-a) and processes + releases 202
    results = run_factory(clones[1])
    assert [r["issue"] for r in results] == [202]
    assert results[0]["stage"] == "merged"
    assert results[0].get("lease") is None

    # Explicit single-issue run on 201 also skips when leased by live peer-a
    assert run_factory(clones[1], issue_number=201) == []

    # Verify remote job-202 lease was released while job-201 is still owned by peer-a
    check_store = GitStateStore(clones[1], "m1")
    remote_202 = check_store.hydrate_job("job-202", str(tmp_path / "h202"))
    assert remote_202["stage"] == "merged"
    assert remote_202["lease"] is None
    remote_201 = check_store.hydrate_job("job-201", str(tmp_path / "h201"))
    assert remote_201["lease"]["worker_id"] == "peer-a"


def test_train_ticket_refs_crud(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")

    assert s0.put_train_ticket("job-1", {"job_id": "job-1", "issue_number": 1, "branch": "factory/job-1", "solo": False})
    assert s1.put_train_ticket("job-2", {"job_id": "job-2", "issue_number": 2, "branch": "factory/job-2", "solo": False})

    tickets = s0.list_train_tickets()
    assert [t["job_id"] for t in tickets] == ["job-1", "job-2"]

    s0.delete_train_ticket("job-1")
    assert [t["job_id"] for t in s1.list_train_tickets()] == ["job-2"]
    s1.delete_train_ticket("job-2")
    assert s0.list_train_tickets() == []


