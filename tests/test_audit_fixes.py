"""Targeted regression tests for the ponytail repository audit fixes."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _init_git_repo_with_origin(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    origin = tmp_path / f"{name}-origin.git"
    repo = tmp_path / name
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("/factory/jobs/*\n/.worktrees/\n", encoding="utf-8")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    (repo / "factory").mkdir(parents=True, exist_ok=True)
    checks = repo / "factory" / "checks.sh"
    checks.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    checks.chmod(checks.stat().st_mode | stat.S_IXUSR)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return origin, repo


# Finding 2: run_factory disables batch mode when staging_qa.enabled is True
def test_run_factory_disables_batch_when_staging_qa_enabled(tmp_path: Path, monkeypatch):
    from factory.lib import pipeline

    _, repo = _init_git_repo_with_origin(tmp_path, "sqa-no-batch")
    (repo / "factory" / "config.json").write_text(
        json.dumps({"staging_qa": {"enabled": True, "base_url": "https://staging.example.com"}}),
        encoding="utf-8",
    )

    fake_gh = MagicMock()
    fake_gh.list_queue_issues.return_value = [
        {"number": 1, "title": "One", "body": "", "url": "https://x/1"},
        {"number": 2, "title": "Two", "body": "", "url": "https://x/2"},
    ]
    monkeypatch.setattr(pipeline, "GitHubClient", lambda r: fake_gh)
    monkeypatch.setattr(pipeline.ProjectBoardSync, "discover", lambda r: None)

    batch_called = []
    solo_called = []
    monkeypatch.setattr(pipeline, "run_batch_factory", lambda *a, **kw: batch_called.append(True) or [])
    monkeypatch.setattr(
        pipeline,
        "process_job",
        lambda r, n, **kw: solo_called.append(n) or {"id": f"job-{n}", "issue": n, "stage": "merged"},
    )

    res = pipeline.run_factory(str(repo))
    assert len(res) == 2
    assert batch_called == []
    assert sorted(solo_called) == [1, 2]


# Finding 2: execute_batch respects halt signal and skips already-leased issues
def test_execute_batch_respects_halt_and_remote_leases(tmp_path: Path, monkeypatch):
    from factory.lib.batch import BatchSpec, TriageItem, execute_batch
    from factory.lib.config import load_config
    from factory.lib.git_ops import GitHubClient
    from factory.lib.git_state import GitStateStore
    from factory.lib.pipeline import JobHaltedError
    from factory.lib.state import enqueue_job

    _, repo = _init_git_repo_with_origin(tmp_path, "batch-lease")
    cfg = load_config(str(repo / "factory"))
    jobs_dir = str(repo / "factory" / "jobs")
    enqueue_job(jobs_dir, {"number": 1, "title": "One", "body": "", "url": ""}, max_rounds=3)

    # Another worker already holds the lease for job-1
    other_store = GitStateStore(str(repo), "other-box")
    other_store.claim_job_lease("job-1", {"id": "job-1", "issue": 1, "stage": "build"})

    my_store = GitStateStore(str(repo), "my-box")
    item = TriageItem(
        issue=1, title="One", body="", url="", decision="ACCEPT", risk="low",
        surfaces=[], complexity_weight=1, predicted_files=["a.py"], depends_on=[],
        topic_group="", summary="one", questions=[], decision_reason="",
    )
    batch = BatchSpec(id="batch-1", issues=[item], solo_only=False, total_weight=1, predicted_files=["a.py"])
    gh = MagicMock(spec=GitHubClient)
    agent_calls = []

    outcome = execute_batch(
        str(repo), batch, config=cfg, gh=gh, state_store=my_store,
        agent_runner=lambda *a, **kw: agent_calls.append(True),
    )
    assert outcome.merged_issues == []
    assert outcome.failed_issues == []
    assert agent_calls == []

    # Halt signal aborts batch before invoking build agent
    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path))
    (tmp_path / "halt.signal").write_text("stop", encoding="utf-8")
    with pytest.raises(JobHaltedError):
        execute_batch(str(repo), batch, config=cfg, gh=gh, agent_runner=lambda *a, **kw: agent_calls.append(True))


# Finding 3 & 10: same-machine staging lock isolation + zero-parent orphan commits
def test_staging_lock_same_machine_isolation_and_orphan_commits(tmp_path: Path):
    from factory.lib.git_state import GitStateStore

    _, repo = _init_git_repo_with_origin(tmp_path, "lock-orphan")
    s1 = GitStateStore(str(repo), "same-box")
    s2 = GitStateStore(str(repo), "same-box")

    assert s1.acquire_staging_lock("job-1", ttl_sec=600) is True
    # Second instance on the same machine cannot steal or delete s1's active lock
    assert s2.acquire_staging_lock("job-2", ttl_sec=600) is False
    s2.release_staging_lock()
    assert s1.acquire_staging_lock("job-1", ttl_sec=600) is True  # still owned by s1
    s1.release_staging_lock()
    assert s2.acquire_staging_lock("job-2", ttl_sec=600) is True
    s2.release_staging_lock()

    # Heartbeat commits are zero-parent orphans (no unbounded git history growth)
    s1.write_worker_heartbeat(0, 2, False)
    s1.write_worker_heartbeat(1, 2, False)
    sha = s1._fetch_ref("refs/factory/workers/same-box")
    assert sha
    rev = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--parents", "-n", "1", sha],
        check=True, capture_output=True, text=True,
    ).stdout.strip().split()
    assert len(rev) == 1  # commit SHA only, 0 parents


# Finding 4: fleet SSH quoting and remote $HOME preservation
def test_fleet_ssh_quoting_and_remote_home_path(tmp_path: Path, monkeypatch):
    from factory.lib.fleet import _factory_shell_cmd, _run_on_machine

    cmd = _factory_shell_cmd("worker", "--all", config={"path_prepend": ["~/.local/bin"]}, remote=True)
    assert cmd[:2] == ["sh", "-lc"]
    assert "$HOME/.local/bin" in cmd[2]
    assert os.path.expanduser("~") not in cmd[2]

    captured: list[list[str]] = []

    def fake_run(argv, **kw):
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr("factory.lib.fleet.subprocess.run", fake_run)
    rc, out = _run_on_machine({"name": "peer", "via": "ssh", "target": "user@peer"}, cmd)
    assert rc == 0 and out == "ok"
    assert captured[0][:3] == ["ssh", "-o", "BatchMode=yes"]
    # The sh -lc payload must be shell-quoted as a single remote argument
    remote_str = captured[0][4]
    assert remote_str.startswith("sh -lc '")


# Finding 5: reconcile_ready_jobs claims remote lease and supports async spawn_fn
def test_reconcile_ready_jobs_claims_lease_and_uses_spawn_fn(tmp_path: Path):
    from factory.lib.config import load_config
    from factory.lib.git_state import GitStateStore
    from factory.lib.pipeline import reconcile_ready_jobs
    from factory.lib.state import enqueue_job, update_job

    _, repo = _init_git_repo_with_origin(tmp_path, "reconcile-lease")
    cfg = load_config(str(repo / "factory"))
    jobs_dir = str(repo / "factory" / "jobs")
    enqueue_job(jobs_dir, {"number": 10, "title": "Free issue", "body": "", "url": ""}, max_rounds=3)
    update_job(jobs_dir, "job-10", stage="ready", pr_number=100)
    enqueue_job(jobs_dir, {"number": 11, "title": "Leased elsewhere", "body": "", "url": ""}, max_rounds=3)
    update_job(jobs_dir, "job-11", stage="ready", pr_number=101)

    store = GitStateStore(str(repo), "worker-a")
    other = GitStateStore(str(repo), "worker-b")
    other.claim_job_lease("job-11", {"id": "job-11", "issue": 11, "stage": "ready", "pr_number": 101})

    gh = MagicMock()
    gh.fetch_pr_review_state.return_value = {
        "merged": False,
        "review_decision": "APPROVED",
        "latest_review_id": "rev-1",
        "labels": [],
    }

    spawned: list[tuple[str, int]] = []
    processed = reconcile_ready_jobs(
        str(repo), cfg, gh, state_store=store,
        spawn_fn=lambda r, inum: spawned.append((r, inum)),
    )
    assert [j["issue"] for j in processed] == [10]
    assert spawned == [(str(repo), 10)]


# Finding 7: learn_from_jobs and factory logs read schema-valid "issue" review findings
def test_review_issue_findings_in_learn_and_logs(tmp_path: Path):
    from factory import cli
    from factory.lib.onboarding import learn_from_jobs
    from factory.lib.state import enqueue_job, update_job

    _, repo = _init_git_repo_with_origin(tmp_path, "learn-schema")
    jobs_dir = str(repo / "factory" / "jobs")
    enqueue_job(jobs_dir, {"number": 5, "title": "Fix bug", "body": "", "url": ""}, max_rounds=3)
    update_job(jobs_dir, "job-5", stage="merged", round=2)
    rdir = repo / "factory" / "jobs" / "job-5" / "round-1"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "review-code.json").write_text(
        json.dumps({
            "verdict": "FAIL",
            "summary": ["bad sql"],
            "findings": [
                {"severity": "high", "file": "app/db.py", "line": 42, "issue": "Use parameterized queries", "fix": "bind"},
            ],
            "checked": ["app/db.py"],
        }),
        encoding="utf-8",
    )

    added = learn_from_jobs(str(repo))
    assert any("app/db.py:42: Use parameterized queries" in b for b in added)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["logs", "--repo", str(repo), "job-5"])
    assert rc == 0
    assert "app/db.py:42: Use parameterized queries" in buf.getvalue()


# Finding 8: deploy_to_staging honors staging_qa.deploy_command
def test_deploy_to_staging_honors_deploy_command(tmp_path: Path, monkeypatch):
    from factory.lib.staging_qa import deploy_to_staging

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen_cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="deployed ok\n", stderr="")

    monkeypatch.setattr("factory.lib.staging_qa.subprocess.run", fake_run)
    ok, log = deploy_to_staging(
        str(tmp_path), {}, 30,
        staging_cfg={"deploy_command": "echo custom-deploy"},
    )
    assert ok is True
    assert "deployed ok" in log
    assert seen_cmds == [["bash", "-lc", "echo custom-deploy"]]


# Finding 9 & 18: factory clean preserves merged state; status <job_id> fetches single remote ref
def test_cli_clean_preserves_merged_and_status_uses_get_remote_job(tmp_path: Path):
    from factory import cli
    from factory.lib.state import enqueue_job, load_job, update_job

    _, repo = _init_git_repo_with_origin(tmp_path, "clean-merged")
    jobs_dir = str(repo / "factory" / "jobs")
    enqueue_job(jobs_dir, {"number": 7, "title": "Merged elsewhere", "body": "", "url": ""}, max_rounds=3)
    update_job(jobs_dir, "job-7", stage="build", pr_number=70)

    fake_gh = MagicMock()
    fake_gh.fetch_issue.return_value = ({"number": 7, "state": "CLOSED"}, "")
    fake_gh.get_pr_merge_commit.return_value = "deadbeef"
    fake_store = MagicMock()
    fake_store.get_remote_job.return_value = {"id": "job-7", "issue": 7, "stage": "merged", "round": 1}

    buf = io.StringIO()
    with patch("factory.cli.GitHubClient", return_value=fake_gh), \
         patch("factory.cli.GitStateStore", return_value=fake_store), \
         patch("factory.cli.remove_worktree"):
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["clean", "--repo", str(repo)])
    assert rc == 0
    assert load_job(jobs_dir, "job-7")["stage"] == "merged"

    # factory status <job_id> uses get_remote_job instead of list_remote_jobs
    fake_store.reset_mock()
    fake_store.get_remote_job.return_value = {"id": "job-99", "issue": 99, "stage": "build", "round": 1}
    buf2 = io.StringIO()
    with patch("factory.cli.GitStateStore", return_value=fake_store):
        with contextlib.redirect_stdout(buf2):
            rc2 = cli.main(["status", "--repo", str(repo), "job-99"])
    assert rc2 == 0
    fake_store.get_remote_job.assert_called_once_with("job-99")
    fake_store.list_remote_jobs.assert_not_called()
    assert '"id": "job-99"' in buf2.getvalue()


# Finding 11: doctor passes on opencode-only config without agy
def test_doctor_does_not_require_agy_for_opencode_only_config(tmp_path: Path, monkeypatch):
    from factory.lib.config import MODEL_ROLES
    from factory.lib.doctor import run_doctor

    monkeypatch.setenv("FACTORY_HOST_DIR", str(tmp_path / "host-cfg"))
    _, repo = _init_git_repo_with_origin(tmp_path, "oc-only")
    fake_oc = tmp_path / "opencode"
    fake_oc.write_text("#!/usr/bin/env bash\necho 'opencode 1.0.0'\n", encoding="utf-8")
    fake_oc.chmod(0o755)
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(fake_oc))
    (repo / "factory" / "config.json").write_text(
        json.dumps({
            "providers": {"opencode": {"bin": str(fake_oc)}},
            "models": {role: "opencode/deepseek-v4.1-flash-free" for role in MODEL_ROLES},
        }),
        encoding="utf-8",
    )

    real_which = __import__("shutil").which
    real_run = subprocess.run

    def fake_which(cmd, path=None):
        if os.path.basename(str(cmd)) == "agy":
            return None
        if cmd == "gh":
            return "/usr/bin/gh"
        return real_which(cmd, path=path)

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and os.path.basename(cmd[0]) == "gh":
            return subprocess.CompletedProcess(cmd, 0, stdout="Logged in\n", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)

    ok, messages = run_doctor(str(repo))
    assert ok is True, messages
    assert not any("`agy`" in m for m in messages)


# Finding 13, 14, 15: layered config, single-model cooldown skip, opencode failure logs & agy log preservation
def test_agy_runner_layered_config_single_model_cooldown_and_logs(tmp_path: Path, monkeypatch):
    from factory.lib.agy_runner import (
        QuotaExhaustedError,
        _resolve_idle_bg_grace_sec,
        run_agent,
        run_agent_chain,
    )
    from factory.lib.opencode_runner import run_opencode_turn
    from factory.lib.schemas import SCHEMAS

    # Finding 13: layered runtime config overrides raw factory/config.json
    fdir = tmp_path / "factory"
    fdir.mkdir()
    (fdir / "config.json").write_text(json.dumps({"idle_bg_grace_sec": 99}), encoding="utf-8")
    assert _resolve_idle_bg_grace_sec(str(fdir), None, config={"idle_bg_grace_sec": 7}) == 7.0

    # Finding 14: single-model chain skips active cooldown instead of burning 429
    class _Store:
        def get_quota_state(self):
            return {"models": {"gemini-4-argon": {"cooldown_until_ts": time.time() + 600}}}

    called = []
    with pytest.raises(QuotaExhaustedError):
        run_agent_chain(
            str(fdir), str(tmp_path), str(tmp_path), "intake", "intake",
            "gemini-4-argon:medium", "prompt", SCHEMAS["intake"], 30,
            state_store=_Store(),
            runner_fn=lambda *a, **kw: called.append(True),
        )
    assert called == []

    # Finding 15: opencode writes raw failure log when subprocess fails
    bad_oc = tmp_path / "bad-opencode"
    bad_oc.write_text("#!/usr/bin/env bash\necho 'boom stderr' >&2\nexit 2\n", encoding="utf-8")
    bad_oc.chmod(0o755)
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(bad_oc))
    log_path = tmp_path / "logs" / "oc-fail.json"
    with pytest.raises(RuntimeError):
        run_opencode_turn(
            str(tmp_path), "intake", "opencode/deepseek-v4.1-flash-free", "hi", SCHEMAS["intake"], 15,
            config={"providers": {"opencode": {"bin": str(bad_oc)}}},
            log_path=str(log_path),
        )
    assert log_path.is_file()
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert saved["returncode"] == 2
    assert "boom stderr" in saved["stderr"]

    # Finding 15: agy run_agent preserves raw stderr/returncode alongside envelope
    _, wt = _init_git_repo_with_origin(tmp_path, "agy-log-wt")
    (wt / "factory" / "agents").mkdir(parents=True, exist_ok=True)
    (wt / "factory" / "agents" / "planner.md").write_text("Planner agent", encoding="utf-8")
    fake_agy = tmp_path / "fake-agy"
    fake_agy.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'diag stderr line' >&2\n"
        "cat <<'JSON'\n"
        '{"status":"SUCCESS","structured_output":{"edges":[],"predicted_surfaces":[],"reasoning":"ok"},"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}\n'
        "JSON\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    jdir = tmp_path / "job-1"
    jdir.mkdir()
    out, _, _ = run_agent(
        str(wt / "factory"), str(jdir), str(wt), "plan", "planner", "gemini-4-argon:medium",
        "prompt", SCHEMAS["plan"], 30,
        log_name="planner",
        config={"providers": {"agy": {"bin": str(fake_agy)}}},
    )
    assert out["reasoning"] == "ok"
    agy_log = json.loads((jdir / "logs" / "planner.json").read_text(encoding="utf-8"))
    assert agy_log["returncode"] == 0
    assert "diag stderr line" in agy_log["stderr"]
    assert agy_log["status"] == "SUCCESS"


# Finding 19 & 20: fleet wake exit code on failure; patrol dataclass hook & configured queue label
def test_fleet_wake_nonzero_exit_and_patrol_dataclass_and_queue_label(tmp_path: Path):
    from factory import cli
    from factory.lib.patrol import run_patrol

    buf = io.StringIO()
    with patch("factory.lib.fleet.fleet_wake", return_value=[("box-a", 1, "ssh refused")]):
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["fleet", "wake"])
    assert rc == 1

    _, repo = _init_git_repo_with_origin(tmp_path, "patrol-test")
    (repo / "factory" / "config.json").write_text(
        json.dumps({"labels": {"queue": "custom-queue"}}),
        encoding="utf-8",
    )
    (repo / "factory" / "patrol_hooks.py").write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n\n"
        "@dataclass\n"
        "class Item:\n"
        "    title: str\n\n"
        "def collect_patrol_findings(repo_root: str):\n"
        "    it = Item('Drift detected')\n"
        "    return [{'fingerprint': 'fp-1', 'title': it.title, 'body': 'details'}]\n",
        encoding="utf-8",
    )
    gh = MagicMock()
    gh.list_open_issues.return_value = []
    gh.create_issue.return_value = {"url": "https://x/issues/1"}

    actions = run_patrol(str(repo), gh=gh)
    assert len(actions) == 1
    gh.create_issue.assert_called_once()
    assert gh.create_issue.call_args.kwargs["labels"] == ("custom-queue",)
