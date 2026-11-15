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
