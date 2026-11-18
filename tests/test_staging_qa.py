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
    assert cfg["staging_qa"]["test_mode_secret_keys"] == ["STRIPE_SECRET_KEY"]

    (fdir / "config.json").write_text(
        json.dumps({"staging_qa": {"test_mode_secret_keys": "STRIPE_SECRET_KEY"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="staging_qa.test_mode_secret_keys"):
        load_config(str(fdir))


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

    custom_bad_env = tmp_path / "custom_bad.env"
    custom_bad_env.write_text("CUSTOM_PAYMENT_KEY=rk_live_1234567890abcdefghij\n", encoding="utf-8")
    # Default guarded key list ignores custom key name unless configured.
    assert load_staging_secrets([str(custom_bad_env)])["CUSTOM_PAYMENT_KEY"].startswith("rk_live_")
    with pytest.raises(ValueError, match="Refusing live Stripe key in CUSTOM_PAYMENT_KEY"):
        load_staging_secrets([str(custom_bad_env)], test_mode_secret_keys=["CUSTOM_PAYMENT_KEY"])


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


def test_staging_qa_retry_injects_qa_failure_md_in_fix_round_and_checkpoints_qa_branch(
    tmp_path, monkeypatch
):
    import subprocess
    from factory.lib.git_ops import ensure_worktree, head_sha
    from factory.lib.git_state import GitStateStore
    from factory.lib.pipeline import process_job
    from factory.lib.state import load_job, update_job

    bare = str(tmp_path / "origin.git")
    c0 = str(tmp_path / "w0")
    gh_clone = str(tmp_path / "gh_clone")
    subprocess.run(["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True)
    for c in (c0, gh_clone):
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
                    "max_qa_rounds": 2,
                    "max_batch_size": 1,
                    "coalesce_window_sec": 0,
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

    s0 = GitStateStore(c0, "m0")
    jid = "job-25"
    jdir_root = os.path.join(c0, "factory", "jobs")
    jdir = os.path.join(jdir_root, jid)
    create_or_resume_job(
        jdir_root,
        {"number": 25, "title": "Multi-slice ingest fix", "url": "https://x/25"},
        max_rounds=3,
        max_qa_rounds=2,
    )
    os.makedirs(os.path.join(jdir, "round-1"), exist_ok=True)
    with open(os.path.join(jdir, "intake.json"), "w", encoding="utf-8") as f:
        json.dump({"decision": "ACCEPT", "risk": "low", "surfaces": ["backend"], "reasoning": "ok"}, f)
    with open(os.path.join(jdir, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": "s",
                "acceptance_criteria": ["ac"],
                "slices": [
                    {"id": "s1", "title": "Slice 1", "files": ["f25.py"], "tests": []},
                    {"id": "s2", "title": "Slice 2", "files": ["f25.py"], "tests": []},
                ],
            },
            f,
        )
    with open(os.path.join(jdir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "slices": [
                    {"id": "s1", "title": "Slice 1", "files": ["f25.py"], "tests": []},
                    {"id": "s2", "title": "Slice 2", "files": ["f25.py"], "tests": []},
                ]
            },
            f,
        )
    with open(os.path.join(jdir, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# Spec\nSupport Bluesky profile URLs.\n")
    with open(os.path.join(jdir, "round-1", "checks.md"), "w", encoding="utf-8") as f:
        f.write("VERDICT: PASS\n")

    wt, br = ensure_worktree(c0, jid, "main")
    with open(os.path.join(wt, "f25.py"), "w", encoding="utf-8") as f:
        f.write("VERSION = 1\n")
    subprocess.run(["git", "-C", wt, "add", "f25.py"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-m", "feat 25"], check=True, capture_output=True)
    subprocess.run(["git", "-C", wt, "push", "-u", "origin", br], check=True, capture_output=True)
    round1_head = head_sha(wt)
    s0.push_slice_checkpoint(jid, wt)

    update_job(
        jdir_root,
        jid,
        pr_number=125,
        pr_url="https://x/pr/125",
        round=1,
        total_slices=2,
        completed_slices=2,
        risk="low",
        surfaces=["backend"],
    )
    for stg in ("spec", "build", "gate", "review", "approve", "merging"):
        advance_stage(jdir_root, jid, stg)
    s0.checkpoint_job(jid, jdir, load_job(jdir_root, jid), release_lease=False)

    class _RealMergeGH:
        def __init__(self):
            self.pr_heads = {125: "factory/job-25"}
            self.merged = {}
            self.next_pr = 126

        def fetch_issue(self, num):
            return {
                "number": num,
                "title": "Multi-slice ingest fix",
                "url": f"https://x/{num}",
                "body": "b",
                "comments": [],
                "labels": ["factory"],
            }, f"# Issue #{num}\nb\n"

        def get_pr_merge_commit(self, pr_num):
            return self.merged.get(pr_num)

        def merge_pr(self, pr_num, *_a, **_kw):
            head_br = self.pr_heads[pr_num]
            subprocess.run(["git", "-C", gh_clone, "fetch", "origin"], check=True, capture_output=True)
            subprocess.run(["git", "-C", gh_clone, "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", gh_clone, "reset", "--hard", "origin/main"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", gh_clone, "merge", "--no-ff", "-m", f"Merge PR #{pr_num}", f"origin/{head_br}"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", gh_clone, "push", "origin", "main"], check=True, capture_output=True)
            sha = head_sha(gh_clone)
            self.merged[pr_num] = sha
            return sha

        def ensure_draft_pr(self, worktree, branch, base_branch, title, body, pr_number=None):
            if pr_number:
                self.pr_heads[pr_number] = branch
                return pr_number, f"https://x/pr/{pr_number}"
            num = self.next_pr
            self.next_pr += 1
            self.pr_heads[num] = branch
            return num, f"https://x/pr/{num}"

        def update_pr(self, *a, **kw):
            pass

        def mark_pr_ready(self, *a, **kw):
            pass

        def comment_issue(self, *a, **kw):
            pass

        def edit_labels(self, *a, **kw):
            pass

        def reopen_issue(self, *a, **kw):
            pass

    gh = _RealMergeGH()
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)
    monkeypatch.setattr("factory.lib.pipeline.run_gate", lambda *a, **kw: (True, "VERDICT: PASS\n"))
    monkeypatch.setattr(
        "factory.lib.pipeline.plan_staging_deploy",
        lambda changed_files, repo_root=None: type(
            "P", (), {"skip_non_runtime": False, "extra_args": [], "reason": "runtime"}
        )(),
    )
    monkeypatch.setattr("factory.lib.pipeline.deploy_to_staging", lambda *a, **kw: (True, "deployed"))
    monkeypatch.setattr("factory.lib.pipeline.rollback_staging", lambda *a, **kw: (True, "rolled back"))

    qa_calls = {"n": 0}
    builder_prompts: list[str] = []
    ckpt_on_build: list[str] = []

    def fake_run_agent(factory_dir, jdir_arg, wt_arg, stage_name, role_name, model, prompt, schema, timeout_sec, **kw):
        if stage_name == "qa":
            qa_calls["n"] += 1
            if qa_calls["n"] == 1:
                return (
                    {
                        "verdict": "FAIL",
                        "summary": ["Bluesky profile URL returned 422 instead of 200"],
                        "scenarios_tested": ["Save Bluesky profile"],
                        "personas_used": ["pro"],
                        "failures": [
                            {
                                "scenario": "Save Bluesky profile",
                                "expected": "200 extracted profile",
                                "actual": "Bluesky profile URL returned 422 instead of 200",
                                "url": "https://staging.example.com/library",
                                "evidence": "qa1.png",
                            }
                        ],
                        "reproduction_steps": ["POST /api/items with bsky.app/profile/safety.bsky.app"],
                    },
                    {"total_tokens": 10},
                    0.01,
                )
            return (
                {
                    "verdict": "PASS",
                    "summary": ["All staging QA checks passed"],
                    "scenarios_tested": ["Save Bluesky profile"],
                    "personas_used": ["pro"],
                    "failures": [],
                    "reproduction_steps": [],
                },
                {"total_tokens": 10},
                0.01,
            )
        if stage_name == "build":
            builder_prompts.append(prompt)
            ckpt_on_build.append(s0._ls_remote_one(f"refs/factory/checkpoints/{jid}") or "")
            with open(os.path.join(wt_arg, "f25.py"), "w", encoding="utf-8") as f:
                f.write("VERSION = 2\n")
            subprocess.run(["git", "-C", wt_arg, "add", "f25.py"], check=True)
            subprocess.run(["git", "-C", wt_arg, "commit", "-m", "fix qa2"], check=True, capture_output=True)
            return (
                {"verdict": "DONE", "summary": ["Fixed Bluesky profile URL"], "files_changed": ["f25.py"], "tests_run": []},
                {"total_tokens": 10},
                0.01,
            )
        if stage_name == "review":
            return ({"verdict": "PASS", "summary": ["ok"], "findings": []}, {"total_tokens": 10}, 0.01)
        if stage_name == "approve":
            return ({"decision": "APPROVE", "summary": ["ok"], "blocking_issues": []}, {"total_tokens": 10}, 0.01)
        raise AssertionError(f"Unexpected stage {stage_name}")

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)

    final_job = process_job(c0, 25, gh=gh, state_store=s0)
    assert len(builder_prompts) == 1
    assert "## Staging QA Failure (Must Fix)" in builder_prompts[0]
    assert "Bluesky profile URL returned 422 instead of 200" in builder_prompts[0]
    assert ckpt_on_build[0] != "" and ckpt_on_build[0] != round1_head
    assert final_job["stage"] == "merged"
    assert s0._ls_remote_one(f"refs/factory/checkpoints/{jid}") in ("", None)


def test_staging_qa_escalation_at_limit_restores_qa_branch_and_checkpoints_for_resume(
    tmp_path, monkeypatch
):
    import subprocess
    from factory.lib.git_ops import ensure_worktree, head_sha
    from factory.lib.git_state import GitStateStore
    from factory.lib.pipeline import process_job
    from factory.lib.state import load_job, update_job

    bare = str(tmp_path / "origin.git")
    c0 = str(tmp_path / "w0")
    c1 = str(tmp_path / "w1")
    gh_clone = str(tmp_path / "gh_clone")
    subprocess.run(["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True)
    for c in (c0, c1, gh_clone):
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
                    "max_qa_rounds": 1,
                    "max_batch_size": 1,
                    "coalesce_window_sec": 0,
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
    jid = "job-26"
    jdir_root = os.path.join(c0, "factory", "jobs")
    jdir = os.path.join(jdir_root, jid)
    create_or_resume_job(
        jdir_root,
        {"number": 26, "title": "Escalated QA job", "url": "https://x/26"},
        max_rounds=3,
        max_qa_rounds=1,
    )
    os.makedirs(os.path.join(jdir, "round-1"), exist_ok=True)
    with open(os.path.join(jdir, "intake.json"), "w", encoding="utf-8") as f:
        json.dump({"decision": "ACCEPT", "risk": "low", "surfaces": ["backend"], "reasoning": "ok"}, f)
    with open(os.path.join(jdir, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": "s",
                "acceptance_criteria": ["ac"],
                "slices": [{"id": "s1", "title": "Slice 1", "files": ["f26.py"], "tests": []}],
            },
            f,
        )
    with open(os.path.join(jdir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump({"slices": [{"id": "s1", "title": "Slice 1", "files": ["f26.py"], "tests": []}]}, f)
    with open(os.path.join(jdir, "spec.md"), "w", encoding="utf-8") as f:
        f.write("# Spec\nKeep feature code intact across QA escalation.\n")
    with open(os.path.join(jdir, "round-1", "checks.md"), "w", encoding="utf-8") as f:
        f.write("VERDICT: PASS\n")

    wt, br = ensure_worktree(c0, jid, "main")
    with open(os.path.join(wt, "f26.py"), "w", encoding="utf-8") as f:
        f.write("FEATURE_CODE = 'preserved'\n")
    subprocess.run(["git", "-C", wt, "add", "f26.py"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-m", "feat 26"], check=True, capture_output=True)
    subprocess.run(["git", "-C", wt, "push", "-u", "origin", br], check=True, capture_output=True)
    round1_head = head_sha(wt)
    s0.push_slice_checkpoint(jid, wt)

    update_job(
        jdir_root,
        jid,
        pr_number=126,
        pr_url="https://x/pr/126",
        round=1,
        total_slices=1,
        completed_slices=1,
        risk="low",
        surfaces=["backend"],
    )
    for stg in ("spec", "build", "gate", "review", "approve", "merging"):
        advance_stage(jdir_root, jid, stg)
    s0.checkpoint_job(jid, jdir, load_job(jdir_root, jid), release_lease=False)

    class _RealMergeGH:
        def __init__(self):
            self.pr_heads = {126: "factory/job-26"}
            self.merged = {}
            self.next_pr = 200

        def fetch_issue(self, num):
            return {
                "number": num,
                "title": "Escalated QA job",
                "url": f"https://x/{num}",
                "body": "b",
                "comments": [{"author": {"login": "human"}, "body": "Fix the 503 prefetch too."}],
                "labels": ["factory"],
            }, f"# Issue #{num}\nFix the 503 prefetch too.\n"

        def get_pr_merge_commit(self, pr_num):
            return self.merged.get(pr_num)

        def merge_pr(self, pr_num, *_a, **_kw):
            head_br = self.pr_heads[pr_num]
            subprocess.run(["git", "-C", gh_clone, "fetch", "origin"], check=True, capture_output=True)
            subprocess.run(["git", "-C", gh_clone, "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", gh_clone, "reset", "--hard", "origin/main"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", gh_clone, "merge", "--no-ff", "-m", f"Merge PR #{pr_num}", f"origin/{head_br}"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", gh_clone, "push", "origin", "main"], check=True, capture_output=True)
            sha = head_sha(gh_clone)
            self.merged[pr_num] = sha
            return sha

        def ensure_draft_pr(self, worktree, branch, base_branch, title, body, pr_number=None):
            num = pr_number or self.next_pr
            if not pr_number:
                self.next_pr += 1
            self.pr_heads[num] = branch
            return num, f"https://x/pr/{num}"

        def update_pr(self, *a, **kw):
            pass

        def mark_pr_ready(self, *a, **kw):
            pass

        def comment_issue(self, *a, **kw):
            pass

        def edit_labels(self, *a, **kw):
            pass

        def reopen_issue(self, *a, **kw):
            pass

    gh = _RealMergeGH()
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)
    monkeypatch.setattr("factory.lib.pipeline.run_gate", lambda *a, **kw: (True, "VERDICT: PASS\n"))
    monkeypatch.setattr(
        "factory.lib.pipeline.plan_staging_deploy",
        lambda changed_files, repo_root=None: type(
            "P", (), {"skip_non_runtime": False, "extra_args": [], "reason": "runtime"}
        )(),
    )
    monkeypatch.setattr("factory.lib.pipeline.deploy_to_staging", lambda *a, **kw: (True, "deployed"))
    monkeypatch.setattr("factory.lib.pipeline.rollback_staging", lambda *a, **kw: (True, "rolled back"))

    qa_turn = {"n": 0}
    resumed_state: dict = {}

    def fake_run_agent(factory_dir, jdir_arg, wt_arg, stage_name, role_name, model, prompt, schema, timeout_sec, **kw):
        if stage_name == "qa":
            qa_turn["n"] += 1
            if qa_turn["n"] == 1:
                return (
                    {
                        "verdict": "FAIL",
                        "summary": ["Speculative prefetch 503"],
                        "scenarios_tested": ["Login"],
                        "personas_used": ["pro"],
                        "failures": [
                            {
                                "scenario": "Login",
                                "expected": "0 5xx responses",
                                "actual": "GET /library/unread returned 503",
                                "url": "https://staging.example.com/login",
                                "evidence": "net.log",
                            }
                        ],
                        "reproduction_steps": ["Open /login"],
                    },
                    {"total_tokens": 10},
                    0.01,
                )
            return (
                {
                    "verdict": "PASS",
                    "summary": ["Clean"],
                    "scenarios_tested": ["Login"],
                    "personas_used": ["pro"],
                    "failures": [],
                    "reproduction_steps": [],
                },
                {"total_tokens": 10},
                0.01,
            )
        if stage_name == "build":
            resumed_state["prompt"] = prompt
            resumed_state["f26_exists_before_edit"] = os.path.isfile(os.path.join(wt_arg, "f26.py"))
            resumed_state["branch"] = subprocess.run(
                ["git", "-C", wt_arg, "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            with open(os.path.join(wt_arg, "f26.py"), "a", encoding="utf-8") as f:
                f.write("PREFETCH_FIXED = True\n")
            subprocess.run(["git", "-C", wt_arg, "add", "f26.py"], check=True)
            subprocess.run(["git", "-C", wt_arg, "commit", "-m", "fix prefetch"], check=True, capture_output=True)
            return (
                {"verdict": "DONE", "summary": ["Fixed prefetch"], "files_changed": ["f26.py"], "tests_run": []},
                {"total_tokens": 10},
                0.01,
            )
        if stage_name == "review":
            return ({"verdict": "PASS", "summary": ["ok"], "findings": []}, {"total_tokens": 10}, 0.01)
        if stage_name == "approve":
            return ({"decision": "APPROVE", "summary": ["ok"], "blocking_issues": []}, {"total_tokens": 10}, 0.01)
        raise AssertionError(f"Unexpected stage {stage_name}")

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)

    esc_job = process_job(c0, 26, gh=gh, state_store=s0)
    assert esc_job["stage"] == "needs-human"
    assert esc_job["branch"] == "factory/job-26-qa2"
    ckpt_sha = s0._ls_remote_one(f"refs/factory/checkpoints/{jid}")
    assert ckpt_sha and ckpt_sha != round1_head

    # Resume on second clean worker c1: feature code from PR 1 must already be restored on factory/job-26-qa2!
    merged_job = process_job(c1, 26, gh=gh, state_store=s1)
    assert merged_job["stage"] == "merged"
    assert resumed_state["branch"] == "factory/job-26-qa2"
    assert resumed_state["f26_exists_before_edit"] is True
    assert "## Staging QA Failure (Must Fix)" in resumed_state["prompt"]
    assert "GET /library/unread returned 503" in resumed_state["prompt"]


def test_run_doctor_honors_configured_wrangler_command_from_staging_hooks(tmp_path, monkeypatch):
    import subprocess
    import sys
    from factory.lib.pipeline import run_doctor

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/r.git"], check=True)
    (repo / ".gitignore").write_text("/factory/jobs/*\n/.worktrees/\n", encoding="utf-8")
    fdir = repo / "factory"
    fdir.mkdir()
    (fdir / "config.json").write_text(
        json.dumps(
            {
                "auto_merge": "all",
                "staging_qa": {
                    "enabled": True,
                    "base_url": "https://staging.example.com",
                },
            }
        ),
        encoding="utf-8",
    )
    checks = fdir / "checks.sh"
    checks.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    checks.chmod(0o755)
    (fdir / "staging_hooks.py").write_text(
        f"def build_deploy_env(secrets, staging_cfg, repo_root=None):\n"
        f"    return {{'WRANGLER': '{sys.executable} -c \"print(\\'4.147.0\\')\"'}}\n",
        encoding="utf-8",
    )

    real_which = __import__("shutil").which
    monkeypatch.setattr(
        "factory.lib.pipeline.shutil.which",
        lambda name: None if name in {"wrangler", "npx"} else (sys.executable if name in {"gh", "agy", sys.executable} else real_which(name)),
    )
    orig_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "-c" and "sync_playwright" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == sys.executable and cmd[1:] in (["auth", "status"], ["--version"]):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
        return orig_run(cmd, *a, **kw)

    monkeypatch.setattr("factory.lib.pipeline.subprocess.run", fake_run)
    ok, messages = run_doctor(str(repo))
    assert ok is True, messages
    assert "OK: wrangler available" in messages
