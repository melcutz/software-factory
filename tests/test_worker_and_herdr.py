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


def test_select_next_candidate_tiers_and_deterministic_spread():
    from factory.lib.worker import select_next_candidate

    now = 1_700_000_000.0
    workers = {
        "m1": {"worker_id": "m1", "expires_at_ts": now + 300},
        "dead": {"worker_id": "dead", "expires_at_ts": now - 500},
    }
    remote_jobs = [
        {"id": "job-10", "issue": 10, "stage": "build", "lease": None, "last_worker_id": "m1"},
        {"id": "job-11", "issue": 11, "stage": "build", "lease": {"worker_id": "dead", "expires_at_ts": now - 500}},
        {"id": "job-12", "issue": 12, "stage": "build", "lease": {"worker_id": "m1", "expires_at_ts": now + 300}},
    ]
    ready_issues = [{"number": 20}, {"number": 21}, {"number": 22}]

    # Tier 1: own interrupted job wins over Tier 2 and Tier 3
    cand = select_next_candidate("m1", remote_jobs, ready_issues, workers, now_ts=now)
    assert cand == ("resume", 10, remote_jobs[0])

    # Active local lease (job-12) must NOT be re-claimed as Tier 1 when job-10 is absent
    cand_active = select_next_candidate("m1", [remote_jobs[1], remote_jobs[2]], ready_issues, workers, now_ts=now)
    assert cand_active == ("resume", 11, remote_jobs[1])

    # Expired lease owned by m1 IS placed into Tier 1 even when m1 has a live worker heartbeat
    expired_own = {"id": "job-13", "issue": 13, "stage": "build", "lease": {"worker_id": "m1", "expires_at_ts": now - 500}}
    cand_expired_own = select_next_candidate("m1", [remote_jobs[1], expired_own], ready_issues, workers, now_ts=now)
    assert cand_expired_own == ("resume", 13, expired_own)

    # Tier 2: peer orphaned job wins over Tier 3 when m2 has no Tier 1 job
    cand2 = select_next_candidate("m2", remote_jobs, ready_issues, workers, now_ts=now)
    assert cand2 == ("resume", 11, remote_jobs[1])

    # Tier 3: different machines pick differently via sha256(machine_id:issue)
    picks = {
        select_next_candidate(f"machine-{i}", [], ready_issues, workers, now_ts=now)[1]
        for i in range(6)
    }
    assert len(picks) > 1


def test_spread_first_pacing_enforces_ramp_up_delay():
    from factory.lib.worker import ClaimPacer

    pacer = ClaimPacer(ramp_up_delay_sec=20.0)
    assert pacer.can_claim(now=100.0) is True
    pacer.record_claim(now=100.0)
    assert pacer.can_claim(now=110.0) is False
    assert pacer.can_claim(now=120.5) is True


def test_pause_and_unpause_toggles_host_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))
    from factory import cli
    from factory.lib.worker import is_host_paused

    assert is_host_paused() is False
    assert cli.main(["pause"]) == 0
    assert is_host_paused() is True
    assert cli.main(["unpause"]) == 0
    assert is_host_paused() is False


def test_fleet_wake_uses_hc_machine_ls_and_hc_run_on(tmp_path, monkeypatch):
    log_file = tmp_path / "hc_fleet.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['machine', 'ls']:\n"
        "    print(json.dumps({'machines': [\n"
        "        {'name': 'ryzen-box', 'host': 'ryzen', 'state': 'connected', 'version': '0.1.16'},\n"
        "        {'name': 'wsl-node', 'host': 'wsl', 'state': 'connected', 'version': '0.1.16'},\n"
        "    ]}))\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))

    from factory.lib.fleet import fleet_wake

    results = fleet_wake()
    assert len(results) >= 2
    lines = log_file.read_text().splitlines()
    assert any(ln == "machine ls" for ln in lines)
    assert any("run --bg --name factory-worker-" in ln and "exec factory worker --all" in ln for ln in lines)
    assert any("run --on ryzen-box --bg --name factory-worker-ryzen-box -- sh -lc" in ln and "exec factory worker --all" in ln for ln in lines)
    assert any("run --on wsl-node --bg --name factory-worker-wsl-node -- sh -lc" in ln and "exec factory worker --all" in ln for ln in lines)


def test_fleet_update_runs_git_pull_on_all_machines(tmp_path, monkeypatch):
    log_file = tmp_path / "hc_update.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['machine', 'ls']:\n"
        "    print(json.dumps({'machines': [{'name': 'lenovo', 'state': 'connected'}]}))\n"
        "else:\n"
        "    print('Already up to date.')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))

    from factory import cli
    assert cli.main(["fleet", "update"]) == 0
    lines = log_file.read_text().splitlines()
    assert any("run --on lenovo -- sh -lc" in ln and "pull --ff-only origin" in ln for ln in lines)


def test_fleet_wake_is_idempotent_on_name_taken(tmp_path, monkeypatch):
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:3] == ['machine', 'ls']:\n"
        "    print(json.dumps({'machines': [{'name': 'base-ubuntu', 'state': 'connected'}]}))\n"
        "else:\n"
        "    print(json.dumps({'error': {'kind': 'name_taken', 'message': 'already named'}}))\n"
        "    sys.exit(1)\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))

    from factory.lib.fleet import fleet_wake
    res = fleet_wake()
    assert ("base-ubuntu", 0, "already running") in res


def test_sanitize_hc_name_and_human_readable_fleet_output():
    from factory.cli import _format_fleet_result
    from factory.lib.herdr_bridge import sanitize_hc_name

    assert sanitize_hc_name("factory-worker-Rocket") == "factory-worker-rocket"
    assert sanitize_hc_name("My Repo#42!") == "my-repo-42"

    started = _format_fleet_result("lenovo", 0, '{"address":"lenovo/t11","state":"live"}')
    assert started == "lenovo: started (lenovo/t11, live)"

    failed = _format_fleet_result("Rocket", 6, '{"error":{"message":"bad name","hint":"lowercase only"}}')
    assert failed == "Rocket: FAILED - bad name (lowercase only)"


def test_fleet_remove_calls_hc_machine_rm(tmp_path, monkeypatch):
    log_file = tmp_path / "hc_rm.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['machine', 'ls']:\n"
        "    print(json.dumps({'machines': [{'name': 'lenovo', 'state': 'connected'}]}))\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))

    from factory import cli
    assert cli.main(["fleet", "remove", "lenovo"]) == 0
    lines = log_file.read_text().splitlines()
    assert "machine rm lenovo" in lines


def _init_repo_with_origin(tmp_path, name="repo", concurrency=4):
    import json
    import subprocess

    bare = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    seed = tmp_path / f"{name}-seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "T"], check=True)
    (seed / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:main"], check=True)

    repo = tmp_path / name
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "W"], check=True)
    (repo / "factory").mkdir()
    (repo / "factory" / "config.json").write_text(
        json.dumps({"base_branch": "main", "concurrency": concurrency}),
        encoding="utf-8",
    )
    return repo


def test_write_worker_heartbeat_persists_probed_resources(tmp_path, monkeypatch):
    from factory.lib.git_state import GitStateStore
    from factory.lib.governor import HostResources

    repo = _init_repo_with_origin(tmp_path, "hb-repo")
    monkeypatch.setattr(
        "factory.lib.governor.probe_host_resources",
        lambda: HostResources(free_ram_gb=11.25, cpu_load_pct=37.5),
    )
    store = GitStateStore(str(repo), "m1")
    store.write_worker_heartbeat(2, 4, False, now_ts=1_700_000_000.0)
    hb = store.list_worker_heartbeats()["m1"]
    assert hb["active_jobs"] == 2
    assert hb["max_workers"] == 4
    assert hb["cpu_load_pct"] == 37.5
    assert hb["free_ram_gb"] == 11.25


def test_run_worker_hc_slot_accounting_and_max_workers(tmp_path, monkeypatch):
    import json
    import time

    from factory.lib.git_state import GitStateStore
    from factory.lib.governor import HostResources
    from factory.lib.worker import run_worker

    repo = _init_repo_with_origin(tmp_path, "slot-repo", concurrency=4)
    host_dir = tmp_path / "host_cfg"
    host_dir.mkdir()
    (host_dir / "host.json").write_text(
        json.dumps({"machine_id": "m1", "max_workers": 2, "ramp_up_delay_sec": 0.0}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FACTORY_HOST_DIR", str(host_dir))

    hc_log = tmp_path / "hc_spawn.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(hc_log)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t42')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))

    monkeypatch.setattr("factory.lib.worker.run_doctor", lambda _r: (True, ["ok"]))
    monkeypatch.setattr(
        "factory.lib.governor.probe_host_resources",
        lambda: HostResources(free_ram_gb=8.5, cpu_load_pct=22.0),
    )
    monkeypatch.setattr("factory.lib.worker._plan_queue_batch", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(
        "factory.lib.worker.GitHubClient.list_queue_issues",
        lambda self, cfg: [{"number": 30, "title": "New issue", "body": ""}],
    )

    store = GitStateStore(str(repo), "m1")
    now = time.time()
    # 2 active leases owned by m1 + 1 expired lease + 1 terminal (merged) + 1 paused (needs-human)
    jdir = str(tmp_path / "empty_jdir")
    store.checkpoint_job(
        "job-10",
        jdir,
        {"id": "job-10", "issue": 10, "stage": "build", "lease": {"worker_id": "m1", "fencing_token": 1, "expires_at_ts": now + 300}},
    )
    store.checkpoint_job(
        "job-11",
        jdir,
        {"id": "job-11", "issue": 11, "stage": "spec", "lease": {"worker_id": "m1", "fencing_token": 1, "expires_at_ts": now + 300}},
    )
    store.checkpoint_job(
        "job-12",
        jdir,
        {"id": "job-12", "issue": 12, "stage": "build", "lease": {"worker_id": "m1", "fencing_token": 1, "expires_at_ts": now - 500}},
    )
    store.checkpoint_job(
        "job-13",
        jdir,
        {"id": "job-13", "issue": 13, "stage": "merged", "lease": {"worker_id": "m1", "fencing_token": 1, "expires_at_ts": now + 300}},
    )
    store.checkpoint_job(
        "job-14",
        jdir,
        {"id": "job-14", "issue": 14, "stage": "needs-human", "lease": {"worker_id": "m1", "fencing_token": 1, "expires_at_ts": now + 300}},
    )

    # At max_workers=2 with 2 active hc leases (job-10, job-11), worker must not claim or spawn
    assert run_worker([str(repo)], once=True) == 0
    assert not hc_log.exists()
    hb = store.list_worker_heartbeats()["m1"]
    assert hb["active_jobs"] == 2
    assert hb["cpu_load_pct"] == 22.0
    assert hb["free_ram_gb"] == 8.5

    # Mark job-11 as merged -> now only 1 active local lease (job-10); even across 2 repos, max_workers=2 allows only 1 claim
    repo2 = _init_repo_with_origin(tmp_path, "slot-repo-2", concurrency=4)
    store.checkpoint_job(
        "job-11",
        jdir,
        {"id": "job-11", "issue": 11, "stage": "merged", "lease": None},
    )
    assert run_worker([str(repo), str(repo2)], once=True) == 0
    spawned = hc_log.read_text(encoding="utf-8").splitlines()
    assert len(spawned) == 1
    assert "-- _exec-job" in spawned[0] or "_exec-job" in spawned[0]
    hb2 = store.list_worker_heartbeats()["m1"]
    assert hb2["active_jobs"] == 2


def test_run_worker_enforces_repo_concurrency_cap(tmp_path, monkeypatch):
    import json
    import time

    from factory.lib.git_state import GitStateStore
    from factory.lib.governor import HostResources
    from factory.lib.worker import run_worker

    repo = _init_repo_with_origin(tmp_path, "conc-repo", concurrency=2)
    host_dir = tmp_path / "host_cfg_conc"
    host_dir.mkdir()
    (host_dir / "host.json").write_text(
        json.dumps({"machine_id": "m1", "max_workers": 4, "ramp_up_delay_sec": 0.0}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FACTORY_HOST_DIR", str(host_dir))

    hc_log = tmp_path / "hc_conc.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(hc_log)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t55')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))

    monkeypatch.setattr("factory.lib.worker.run_doctor", lambda _r: (True, ["ok"]))
    monkeypatch.setattr(
        "factory.lib.governor.probe_host_resources",
        lambda: HostResources(free_ram_gb=8.0, cpu_load_pct=15.0),
    )
    monkeypatch.setattr("factory.lib.worker._plan_queue_batch", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(
        "factory.lib.worker.GitHubClient.list_queue_issues",
        lambda self, cfg: [{"number": 40, "title": "Issue 40", "body": ""}],
    )

    now = time.time()
    peer_a = GitStateStore(str(repo), "peer-a")
    peer_b = GitStateStore(str(repo), "peer-b")
    peer_a.write_worker_heartbeat(1, 2, False, now_ts=now)
    peer_b.write_worker_heartbeat(1, 2, False, now_ts=now)
    peer_a.claim_job_lease("job-20", {"id": "job-20", "issue": 20, "stage": "build"}, now_ts=now)
    peer_b.claim_job_lease("job-21", {"id": "job-21", "issue": 21, "stage": "build"}, now_ts=now)

    # Repo has 2 active non-expired leases == concurrency (2) -> m1 must not claim issue 40
    assert run_worker([str(repo)], once=True) == 0
    assert not hc_log.exists()

    # Expire job-21 and mark job-20 as terminal merged -> active leases drop to 0 < 2 -> m1 claims
    jdir = str(tmp_path / "empty_conc_jdir")
    peer_a.checkpoint_job("job-20", jdir, {"id": "job-20", "issue": 20, "stage": "merged"}, release_lease=True)
    peer_b.checkpoint_job(
        "job-21",
        jdir,
        {"id": "job-21", "issue": 21, "stage": "build", "lease": {"worker_id": "peer-b", "fencing_token": 1, "expires_at_ts": now - 500}},
    )
    assert run_worker([str(repo)], once=True) == 0
    assert hc_log.exists()
    spawned = hc_log.read_text(encoding="utf-8").splitlines()
    assert len(spawned) == 1


def test_herdr_bridge_labels_wait_and_live(tmp_path, monkeypatch):
    from factory.lib.herdr_bridge import HerdrBridge

    hc_log = tmp_path / "hc_calls.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(hc_log)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t99')\n"
        "elif sys.argv[1:3] == ['ls', '--json']:\n"
        "    print(json.dumps({'terminals': [{'id': 't99', 'name': 'job-1', 'state': 'live'}]}))\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))

    bridge = HerdrBridge.from_env()
    tid = bridge.spawn_job_terminal("job-1", str(tmp_path), ["echo", "hi"], labels={"issue": "1", "stage": "queued"})
    assert tid == "t99"
    assert bridge.set_labels({"stage": "build", "round": "1"}) is True
    assert bridge.is_terminal_live("t99") is True
    assert bridge.wait_terminal("t99", timeout_sec=5.0) == 0

    lines = hc_log.read_text(encoding="utf-8").splitlines()
    assert any("--label issue=1 --label stage=queued" in ln for ln in lines)
    assert "label t1 stage=build round=1" in lines
    assert "wait t99" in lines


def test_run_factory_spawns_hc_terminals_and_process_job_emits_progress(tmp_path, monkeypatch):
    import io
    import json
    from factory.lib.herdr_bridge import HerdrBridge
    from factory.lib.pipeline import process_job, run_factory
    from factory.lib.state import create_or_resume_job

    repo = _init_repo_with_origin(tmp_path, "herdr-pipe")
    (repo / "factory" / "checks.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (repo / "factory" / "checks.sh").chmod(0o755)

    hc_log = tmp_path / "hc_pipe.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(hc_log)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t77')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t10")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))

    # 1) process_job emits OSC 7501 progress + hc label updates
    osc_buf = io.StringIO()
    orig_from_env = HerdrBridge.from_env
    monkeypatch.setattr(
        "factory.lib.pipeline.HerdrBridge.from_env",
        classmethod(lambda cls: orig_from_env(stream=osc_buf)),
    )
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)
    monkeypatch.setattr(
        "factory.lib.pipeline.GitHubClient.fetch_issue",
        lambda self, num: ({"number": num, "title": f"Issue {num}", "url": f"https://x/{num}", "body": "b", "comments": [], "labels": []}, f"# Issue #{num}\nb\n"),
    )
    monkeypatch.setattr("factory.lib.pipeline.GitHubClient.comment_issue", lambda *a, **kw: None)
    monkeypatch.setattr("factory.lib.pipeline.GitHubClient.edit_labels", lambda *a, **kw: None)
    monkeypatch.setattr(
        "factory.lib.pipeline.run_agent",
        lambda *_a, **_kw: (
            {"decision": "REJECT", "risk": "low", "surfaces": [], "summary": ["no"], "scope_in": "", "scope_out": "", "questions": [], "reasoning": "no"},
            {"total_tokens": 10},
            0.01,
        ),
    )

    res = process_job(str(repo), 70)
    assert res["stage"] == "needs-human"
    osc_out = osc_buf.getvalue()
    assert "\033]7501;state=working:progress=10\033\\" in osc_out
    assert "\033]7501;state=blocked:kind=question\033\\" in osc_out
    lines = hc_log.read_text(encoding="utf-8").splitlines()
    assert any(ln.startswith("label t10 ") and "stage=intake" in ln for ln in lines)


def test_run_factory_spawns_hc_terminals(tmp_path, monkeypatch):
    from factory.lib.pipeline import run_factory
    from factory.lib.state import create_or_resume_job

    repo = _init_repo_with_origin(tmp_path, "herdr-run-factory")
    (repo / "factory" / "checks.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (repo / "factory" / "checks.sh").chmod(0o755)

    hc_log = tmp_path / "hc_pipe.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(hc_log)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t77')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t10")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)

    jobs_dir = str(repo / "factory" / "jobs")
    create_or_resume_job(jobs_dir, {"number": 71, "title": "Queued", "url": "https://x/71"}, 3)
    monkeypatch.setattr(
        "factory.lib.pipeline.GitHubClient.list_queue_issues",
        lambda self, cfg: [{"number": 71, "title": "Queued", "url": "https://x/71", "body": ""}],
    )
    monkeypatch.setattr("factory.lib.pipeline._plan_queue_batch", lambda *a, **kw: ({71: []}, {}))
    out_jobs = run_factory(str(repo), batch_mode=False)
    assert len(out_jobs) == 1
    spawn_lines = hc_log.read_text(encoding="utf-8").splitlines()
    assert any("run --bg" in ln and "_exec-job" in ln and "71" in ln for ln in spawn_lines)
    assert any("wait t77" in ln for ln in spawn_lines)


def test_exec_job_needs_human_does_not_hang_in_tty_shell(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "factory").mkdir(parents=True)
    wt = repo / ".worktrees" / "job-88"
    wt.mkdir(parents=True)

    monkeypatch.setattr("factory.lib.worker.process_job", lambda *a, **kw: {"id": "job-88", "stage": "needs-human", "last_error": "boom"})
    monkeypatch.setattr("factory.lib.worker.sys.stdin.isatty", lambda: True)
    shell_called = []
    monkeypatch.setattr("factory.lib.worker.subprocess.run", lambda *a, **kw: shell_called.append(a))

    from factory.lib.worker import exec_job_command
    rc = exec_job_command(str(repo), 88)
    assert rc == 2
    assert shell_called == []


def test_fleet_restart_stops_and_wakes(tmp_path, monkeypatch):
    log_file = tmp_path / "hc_restart.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['machine', 'ls']:\n"
        "    print(json.dumps({'machines': [{'name': 'lenovo', 'state': 'connected'}]}))\n"
        "else:\n"
        "    print('ok')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))

    from factory import cli
    assert cli.main(["fleet", "restart"]) == 0
    lines = log_file.read_text().splitlines()
    assert any("kill lenovo/factory-worker-lenovo" in ln for ln in lines)
    assert any("run --on lenovo --bg --name factory-worker-lenovo" in ln for ln in lines)


def test_qa_external_blocker_escalates_immediately(tmp_path, monkeypatch):
    import os
    from unittest.mock import MagicMock
    from factory.lib.pipeline import _handle_qa_failure_for_ticket
    from factory.lib.staging_qa import TrainTicket
    from factory.lib.state import create_or_resume_job

    jobs_dir = str(tmp_path / "jobs")
    qa_dir = str(tmp_path / "jobs" / "job-640" / "qa-1")
    os.makedirs(qa_dir, exist_ok=True)
    create_or_resume_job(jobs_dir, {"number": 640, "title": "503", "url": "https://x/640"}, 3)

    gh = MagicMock()
    cfg = {"labels": {"queue": "factory", "needs_info": "factory:needs-info", "needs_human": "factory:needs-human"}}
    tk = TrainTicket(
        job_id="job-640",
        issue_number=640,
        jdir=os.path.join(jobs_dir, "job-640"),
        worktree=str(tmp_path),
        branch="factory/job-640",
        issue_md="",
        initial_stage="staging_qa",
        solo=True,
        merged_sha="abc12345",
    )
    qa_out = {
        "verdict": "FAIL",
        "summary": ["CF Speed Brain"],
        "scenarios_tested": ["tab click"],
        "personas_used": ["pro"],
        "failures": [],
        "reproduction_steps": ["click tab"],
        "external_blocker": "Disable Cloudflare Speed Brain on keepfor.me zone",
    }
    _handle_qa_failure_for_ticket(
        str(tmp_path), jobs_dir, "main", cfg, gh, {}, tk, 1, 3, qa_dir, "QA failed", qa_out, "def67890", True, "ok"
    )
    assert tk.outcome is not None
    assert tk.outcome.action == "return"
    assert tk.outcome.job["stage"] == "needs-human"
    gh.comment_issue.assert_called_once()
    assert "Disable Cloudflare Speed Brain" in gh.comment_issue.call_args[0][1]


def test_run_worker_survives_transient_github_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "factory").mkdir(parents=True)
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))
    monkeypatch.setattr("factory.lib.worker.run_doctor", lambda r: (True, ["OK"]))
    monkeypatch.setattr("factory.lib.worker.GitStateStore.write_worker_heartbeat", lambda *a, **kw: "m1")
    monkeypatch.setattr("factory.lib.worker.GitStateStore.prune_stale_refs", lambda *a, **kw: 0)
    monkeypatch.setattr("factory.lib.worker.GitStateStore.get_control_state", lambda *a, **kw: {"halted": False})
    monkeypatch.setattr("factory.lib.worker.GitStateStore.get_quota_state", lambda *a, **kw: {})
    monkeypatch.setattr("factory.lib.worker.GitStateStore.list_remote_jobs", lambda *a, **kw: [])
    monkeypatch.setattr("factory.lib.worker.GitStateStore.list_worker_heartbeats", lambda *a, **kw: {})
    monkeypatch.setattr(
        "factory.lib.worker.GitHubClient.list_queue_issues",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("error connecting to api.github.com")),
    )

    from factory.lib.worker import run_worker
    assert run_worker([str(repo)], once=True) == 0


def test_cross_process_full_gate_slot_prunes_dead_pids(tmp_path):
    import os
    from factory.lib.gate_runner import _full_gate_slot

    sdir = tmp_path / "slots"
    sdir.mkdir()
    # Dead PID file should be pruned; live os.getpid() file from another slot id should count
    (sdir / "99999999-111").write_text("99999999\n", encoding="utf-8")
    (sdir / f"{os.getpid()}-999999").write_text(f"{os.getpid()}\n", encoding="utf-8")

    with _full_gate_slot(str(sdir)) as active:
        assert active >= 2
        assert not (sdir / "99999999-111").exists()


def test_renew_job_lease_extends_expiry_and_preserves_checkpoint_files(tmp_path):
    from factory.lib.git_state import GitStateStore

    repo = _init_repo_with_origin(tmp_path, "renew-repo")
    store = GitStateStore(str(repo), "m1")
    ok, claimed = store.claim_job_lease("job-77", {"id": "job-77", "issue": 77, "stage": "build"}, now_ts=1000.0)
    assert ok and claimed is not None

    jdir = repo / "factory" / "jobs" / "job-77"
    jdir.mkdir(parents=True)
    (jdir / "spec.md").write_text("# Spec 77\n", encoding="utf-8")
    assert store.checkpoint_job("job-77", str(jdir), claimed)

    assert store.renew_job_lease("job-77", now_ts=1500.0) is True
    out_dir = tmp_path / "hydrated"
    hydrated = store.hydrate_job("job-77", str(out_dir))
    assert hydrated is not None
    assert hydrated["lease"]["expires_at_ts"] > claimed["lease"]["expires_at_ts"]
    assert (out_dir / "spec.md").read_text(encoding="utf-8") == "# Spec 77\n"


def test_cross_process_repo_lock_blocks_other_process(tmp_path):
    import subprocess
    from factory.lib.git_ops import repo_lock

    target = tmp_path / "locked-repo"
    target.mkdir()
    lk = repo_lock(str(target))
    with lk:
        # Re-entrant in same thread
        with lk:
            res = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"from factory.lib.git_ops import repo_lock; "
                    f"print(repo_lock({str(target)!r}).acquire(blocking=False))",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert res.stdout.strip() == "False"


def test_status_reports_remote_leased_jobs_as_live(tmp_path):
    from factory.lib.pipeline import factory_runner_status, format_status_table

    jobs = [
        {
            "id": "job-88",
            "stage": "merging",
            "activity": "running:pre-merge-gate:full",
            "pid": 99999999,
            "lease": {"worker_id": "Base", "hostname": "Base"},
            "_lease_active": True,
        }
    ]
    banner = factory_runner_status(str(tmp_path), jobs)
    table = format_status_table(jobs)
    assert "RUNNING (1 live worker(s))" in banner
    assert "running:pre-merge-gate:full" in table
    assert "stale" not in table






