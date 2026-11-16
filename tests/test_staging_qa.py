"""Tests for generic staging_qa train, hook delegation, config, state, and schemas."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from factory.lib.config import load_config
from factory.lib.schemas import render_qa_failure_md, render_qa_md, validate_stage_output
from factory.lib.staging_qa import (
    StagingTrainCoordinator,
    TrainOutcome,
    TrainTicket,
    load_staging_secrets,
    plan_staging_deploy,
    redact_secrets,
    rollback_staging,
)
from factory.lib.state import advance_stage, create_or_resume_job


def test_qa_schema_validation_and_rendering():
    ok = validate_stage_output(
        "qa",
        {
            "verdict": "PASS",
            "summary": ["All checks green"],
            "scenarios_tested": ["Login and dashboard load"],
            "personas_used": ["pro"],
            "failures": [],
            "reproduction_steps": [],
        },
    )
    assert ok["verdict"] == "PASS"
    assert "VERDICT: PASS" in render_qa_md(ok)

    with pytest.raises(ValueError, match="reproduction_steps"):
        validate_stage_output(
            "qa",
            {
                "verdict": "FAIL",
                "summary": ["Broken"],
                "scenarios_tested": ["Login"],
                "personas_used": ["pro"],
                "failures": [
                    {
                        "scenario": "Login",
                        "expected": "200",
                        "actual": "500",
                        "url": "https://staging.example.com/login",
                        "evidence": "shot.png",
                    }
                ],
                "reproduction_steps": [],
            },
        )

    fail_md = render_qa_failure_md(1, 3, "HTTP 500", None)
    assert "Staging QA cycle 1/3 failed" in fail_md


def test_config_staging_qa_validation(tmp_path):
    fdir = tmp_path / "factory"
    fdir.mkdir()
    (fdir / "config.json").write_text(
        json.dumps(
            {
                "staging_qa": {
                    "enabled": True,
                    "base_url": "https://app.example.com",
                    "forbidden_prod_hosts": ["app.example.com"],
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Refusing production URL"):
        load_config(str(fdir))

    (fdir / "config.json").write_text(
        json.dumps(
            {
                "staging_qa": {
                    "enabled": True,
                    "base_url": "https://staging.example.com",
                    "forbidden_prod_hosts": ["app.example.com"],
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(str(fdir))
    assert cfg["staging_qa"]["enabled"] is True


def test_state_staging_qa_transitions_and_resume(tmp_path):
    jobs_dir = str(tmp_path / "jobs")
    issue = {"number": 10, "title": "QA test", "url": "https://github.com/o/r/issues/10"}
    job, action = create_or_resume_job(jobs_dir, issue, max_rounds=3, max_qa_rounds=2)
    assert action == "new"
    assert job["qa_round_limit"] == 2

    for stg in ("spec", "build", "gate", "review", "approve", "merging", "staging_qa"):
        advance_stage(jobs_dir, "job-10", stg)
    advance_stage(jobs_dir, "job-10", "needs-human", last_error="QA failed")

    resumed, r_action = create_or_resume_job(jobs_dir, issue, max_rounds=3, max_qa_rounds=2)
    assert r_action == "resume"
    assert resumed["stage"] == "build"


def test_load_staging_secrets_and_redaction(tmp_path):
    env_file = tmp_path / "staging.env"
    env_file.write_text('export API_TOKEN="super-secret-value-123"\n', encoding="utf-8")
    secrets = load_staging_secrets([str(env_file)])
    assert secrets["API_TOKEN"] == "super-secret-value-123"
    assert "[REDACTED]" in redact_secrets("leaked super-secret-value-123 here", secrets)

    bad_env = tmp_path / "bad.env"
    bad_env.write_text("STRIPE_SECRET_KEY=sk_live_1234567890abcdefghij\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing live Stripe key"):
        load_staging_secrets([str(bad_env)])


def test_staging_train_coordinator_coalesces_batch(tmp_path):
    coord = StagingTrainCoordinator(max_batch_size=3, coalesce_window_sec=0.1)
    batches: list[list[str]] = []

    def _exec(batch: list[TrainTicket]) -> None:
        batches.append([t.job_id for t in batch])
        for t in batch:
            t.outcome = TrainOutcome(action="return", job={"id": t.job_id, "stage": "merged"})
            t.done_event.set()

    tickets = [
        TrainTicket(
            job_id=f"job-{i}",
            issue_number=i,
            jdir=str(tmp_path),
            worktree=str(tmp_path),
            branch=f"factory/job-{i}",
            issue_md="x",
            initial_stage="merging",
        )
        for i in (1, 2)
    ]

    threads = [
        threading.Thread(target=lambda tk=tk: coord.run_ticket(tk, _exec))
        for tk in tickets
    ]
    for th in threads:
        th.start()
        time.sleep(0.01)
    for th in threads:
        th.join(timeout=5)

    assert len(batches) == 1
    assert set(batches[0]) == {"job-1", "job-2"}


def test_repo_staging_hooks_delegation(tmp_path):
    fdir = tmp_path / "factory"
    fdir.mkdir()
    (fdir / "staging_hooks.py").write_text(
        """
from dataclasses import dataclass

@dataclass
class _Plan:
    skip_non_runtime: bool
    extra_args: list[str]
    reason: str

def plan_staging_deploy(changed_files):
    return _Plan(skip_non_runtime=False, extra_args=["--web-only"], reason="hooked")

def rollback_staging(deploy_wt, deploy_env, snapshot):
    return True, f"rolled back {snapshot.get('web')}"
""",
        encoding="utf-8",
    )
    plan = plan_staging_deploy(["apps/web/main.py"], repo_root=str(tmp_path))
    assert plan.extra_args == ["--web-only"]
    ok, log = rollback_staging(str(tmp_path), {}, {"web": "v1"}, repo_root=str(tmp_path))
    assert ok is True
    assert "rolled back v1" in log


def test_distributed_staging_train_coalesces_across_two_workers(tmp_path, monkeypatch):
    import subprocess
    from factory.lib.git_ops import ensure_worktree
    from factory.lib.git_state import GitStateStore
    from factory.lib.pipeline import process_job
    from factory.lib.state import load_job, update_job

    bare = str(tmp_path / "origin.git")
    c0 = str(tmp_path / "w0")
    c1 = str(tmp_path / "w1")
    subprocess.run(["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True)
    for c in (c0, c1):
        subprocess.run(["git", "clone", bare, c], check=True, capture_output=True)
        subprocess.run(["git", "-C", c, "config", "user.email", "t@e.com"], check=True)
        subprocess.run(["git", "-C", c, "config", "user.name", "T"], check=True)

    f0 = os.path.join(c0, "factory")
    os.makedirs(f0, exist_ok=True)
    with open(os.path.join(f0, "config.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "auto_merge": "all",
                "staging_qa": {
                    "enabled": True,
                    "max_batch_size": 4,
                    "coalesce_window_sec": 1,
                    "base_url": "https://staging.example.com",
                },
            },
            f,
        )
    with open(os.path.join(f0, "checks.sh"), "w", encoding="utf-8") as f:
        f.write("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(os.path.join(f0, "checks.sh"), 0o755)
    subprocess.run(["git", "-C", c0, "add", "."], check=True)
    subprocess.run(["git", "-C", c0, "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", c0, "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", c1, "pull", "--rebase", "origin", "main"], check=True, capture_output=True)

    s0 = GitStateStore(c0, "m0")
    s1 = GitStateStore(c1, "m1")

    for c, store, num in [(c0, s0, 11), (c1, s1, 12)]:
        jid = f"job-{num}"
        jdir = os.path.join(c, "factory", "jobs")
        create_or_resume_job(jdir, {"number": num, "title": f"T{num}", "url": f"https://x/{num}"}, 3)
        wt, br = ensure_worktree(c, jid, "main")
        with open(os.path.join(wt, f"f{num}.py"), "w", encoding="utf-8") as f:
            f.write(f"X = {num}\n")
        subprocess.run(["git", "-C", wt, "add", f"f{num}.py"], check=True)
        subprocess.run(["git", "-C", wt, "commit", "-m", f"feat {num}"], check=True, capture_output=True)
        subprocess.run(["git", "-C", wt, "push", "-u", "origin", br], check=True, capture_output=True)
        update_job(jdir, jid, pr_number=100 + num, pr_url=f"https://x/pr/{100 + num}", round=1)
        advance_stage(jdir, jid, "spec")
        advance_stage(jdir, jid, "build")
        advance_stage(jdir, jid, "gate")
        advance_stage(jdir, jid, "review")
        advance_stage(jdir, jid, "approve")
        advance_stage(jdir, jid, "merging")
        store.checkpoint_job(jid, os.path.join(jdir, jid), load_job(jdir, jid), release_lease=False)

    deploy_calls = []

    class _FakeGH:
        def __init__(self):
            self.merged_prs = set()
        def fetch_issue(self, num):
            return {"number": num, "title": f"T{num}", "url": f"https://x/{num}", "body": "b", "comments": [], "labels": []}, f"# Issue #{num}\nb\n"
        def get_pr_merge_commit(self, pr_num):
            return f"sha-{pr_num}" if pr_num in self.merged_prs else None
        def merge_pr(self, pr_num, *_a, **_kw):
            self.merged_prs.add(pr_num)
            return f"sha-{pr_num}"
        def mark_pr_ready(self, pr_num):
            pass
        def comment_issue(self, *a, **kw):
            pass
        def edit_labels(self, *a, **kw):
            pass

    gh = _FakeGH()
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)
    monkeypatch.setattr("factory.lib.pipeline.run_gate", lambda *a, **kw: (True, "VERDICT: PASS\n"))
    monkeypatch.setattr(
        "factory.lib.pipeline.plan_staging_deploy",
        lambda changed_files, repo_root=None: type("P", (), {"skip_non_runtime": False, "extra_args": [], "reason": "runtime"})(),
    )
    def fake_deploy(wt, env, timeout_sec=900):
        deploy_calls.append(wt)
        return True, "deployed"

    monkeypatch.setattr("factory.lib.pipeline.deploy_to_staging", fake_deploy)
    monkeypatch.setattr(
        "factory.lib.pipeline.run_agent",
        lambda *_a, **_kw: ({"verdict": "PASS", "summary": ["ok"], "scenarios_tested": ["s"], "personas_used": ["p"], "failures": [], "reproduction_steps": []}, {"total_tokens": 10}, 0.01),
    )

    # Pre-publish job-12's train ticket to simulate near-simultaneous arrival across separate workers
    s1.put_train_ticket("job-12", {"job_id": "job-12", "issue_number": 12, "branch": "factory/job-12", "solo": False, "initial_stage": "merging"})

    res_holder = {}
    t1 = threading.Thread(target=lambda: res_holder.setdefault("11", process_job(c0, 11, gh=gh, state_store=s0)))
    t2 = threading.Thread(target=lambda: res_holder.setdefault("12", process_job(c1, 12, gh=gh, state_store=s1)))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert len(deploy_calls) == 1
    assert res_holder["11"]["stage"] == "merged"
    assert res_holder["12"]["stage"] == "merged"
    assert s0.list_train_tickets() == []
