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
        "import sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['machine', 'ls']:\n"
        "    print('ryzen-box  user@100.64.0.2  ready')\n"
        "    print('wsl-node   via wsl.exe      ready')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))

    from factory.lib.fleet import fleet_wake

    results = fleet_wake()
    assert len(results) == 2
    lines = log_file.read_text().splitlines()
    assert any(ln == "machine ls" for ln in lines)
    assert any("run --on ryzen-box --bg --name factory-worker-ryzen-box -- factory worker --all" in ln for ln in lines)
    assert any("run --on wsl-node --bg --name factory-worker-wsl-node -- factory worker --all" in ln for ln in lines)

