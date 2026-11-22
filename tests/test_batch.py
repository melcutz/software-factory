import copy
import graphlib

from factory.lib.batch import TriageItem, pack_batches
from factory.lib.config import DEFAULT_CONFIG


def _item(
    issue: int,
    *,
    risk: str = "low",
    weight: int = 2,
    files: list[str] | None = None,
    deps: list[int] | None = None,
    topic: str = "",
) -> TriageItem:
    return TriageItem(
        issue=issue,
        title=f"Issue {issue}",
        body=f"Body {issue}",
        url=f"https://github.com/o/r/issues/{issue}",
        decision="ACCEPT",
        risk=risk,
        surfaces=[],
        complexity_weight=weight,
        predicted_files=files or [],
        depends_on=deps or [],
        topic_group=topic,
        summary=f"Summary {issue}",
    )


def test_pack_batches_merges_overlapping_files_and_splits_oversized_waves():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["batch"]["max_batch_weight"] = 6
    items = [
        _item(1, weight=3, files=["factory/cli.py"]),
        _item(2, weight=3, files=["factory/cli.py"]),
        _item(3, weight=2, files=["factory/cli.py"]),
    ]
    plan = pack_batches(items, {1: set(), 2: set(), 3: set()}, cfg)
    assert len(plan.batches) == 2
    assert [it.issue for it in plan.batches[0].issues] == [1, 2]
    assert [it.issue for it in plan.batches[1].issues] == [3]
    assert plan.batches[0].id in plan.batch_deps[plan.batches[1].id]


def test_pack_batches_keeps_disjoint_surfaces_parallel():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    items = [
        _item(1, weight=2, files=["factory/cli.py"]),
        _item(2, weight=2, files=["factory/lib/onboarding.py"]),
    ]
    plan = pack_batches(items, {1: set(), 2: set()}, cfg)
    assert len(plan.batches) == 2
    assert plan.batch_deps[plan.batches[0].id] == set()
    assert plan.batch_deps[plan.batches[1].id] == set()


def test_pack_batches_isolates_human_gated_issues_solo():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["auto_merge"] = "low"
    items = [
        _item(1, risk="low", weight=2, files=["factory/cli.py"]),
        _item(2, risk="medium", weight=2, files=["factory/cli.py"]),
        _item(3, risk="low", weight=2, files=["factory/cli.py"]),
    ]
    plan = pack_batches(items, {1: set(), 2: set(), 3: set()}, cfg)
    low_batches = [b for b in plan.batches if not b.solo_only]
    solo_batches = [b for b in plan.batches if b.solo_only]
    assert len(low_batches) == 1
    assert [it.issue for it in low_batches[0].issues] == [1, 3]
    assert len(solo_batches) == 1
    assert [it.issue for it in solo_batches[0].issues] == [2]


def test_pack_batches_breaks_cycles_and_respects_hard_deps():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    items = [
        _item(1, weight=2, files=["a.py"], deps=[2]),
        _item(2, weight=2, files=["b.py"], deps=[1]),
        _item(3, weight=2, files=["a.py"], deps=[1]),
    ]
    plan = pack_batches(items, {1: {2}, 2: {1}, 3: {1}}, cfg)
    graphlib.TopologicalSorter(plan.batch_deps).prepare()
    # Issue 1 must appear before issue 3 wherever they are placed.
    order = [it.issue for b in plan.batches for it in b.issues]
    assert order.index(1) < order.index(3)


def test_pack_batches_respects_max_batch_issues_one():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["batch"]["max_batch_issues"] = 1
    items = [
        _item(1, weight=1, files=["factory/cli.py"]),
        _item(2, weight=1, files=["factory/cli.py"]),
    ]
    plan = pack_batches(items, {1: set(), 2: set()}, cfg)
    assert len(plan.batches) == 2
    assert all(len(b.issues) == 1 for b in plan.batches)


def test_triage_and_plan_queue_fills_omitted_issues_and_enforces_explicit_deps(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import triage_and_plan_queue
    from factory.lib.state import load_job

    fdir = tmp_path / "factory"
    (fdir / "jobs").mkdir(parents=True)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    issues = [
        {"number": 1, "title": "Base", "body": "Do base", "url": "https://x/1"},
        {"number": 2, "title": "Child", "body": "Depends on #1", "url": "https://x/2"},
        {"number": 3, "title": "Vague", "body": "Help", "url": "https://x/3"},
    ]

    def fake_planner(prompt, **kwargs):
        return (
            {
                "items": [
                    {
                        "issue": 1,
                        "decision": "ACCEPT",
                        "risk": "low",
                        "surfaces": [],
                        "complexity_weight": 2,
                        "predicted_files": ["factory/cli.py"],
                        "depends_on": [],
                        "topic_group": "cli",
                        "summary": "Base change",
                        "questions": [],
                        "decision_reason": "",
                    },
                    {
                        "issue": 3,
                        "decision": "NEEDS_INFO",
                        "risk": "low",
                        "surfaces": [],
                        "complexity_weight": 1,
                        "predicted_files": [],
                        "depends_on": [],
                        "topic_group": "",
                        "summary": "Needs detail",
                        "questions": ["Which endpoint?"],
                        "decision_reason": "Missing endpoint",
                    },
                ],
                "reasoning": "Planned",
            },
            {"total_tokens": 100},
            1.0,
        )

    gh = MagicMock()
    dry_plan = triage_and_plan_queue(
        str(tmp_path),
        issues,
        config=cfg,
        gh=gh,
        dry_run=True,
        planner_runner=fake_planner,
    )
    assert [it.issue for it in dry_plan.accepted] == [1, 2]
    assert [it.issue for it in dry_plan.needs_info] == [3]
    assert 1 in dry_plan.hard_deps[2]
    gh.edit_labels.assert_not_called()
    gh.comment_issue.assert_not_called()

    live_plan = triage_and_plan_queue(
        str(tmp_path),
        issues,
        config=cfg,
        gh=gh,
        dry_run=False,
        planner_runner=fake_planner,
    )
    assert len(live_plan.needs_info) == 1
    gh.edit_labels.assert_called_once_with(3, add=[cfg["labels"]["needs_info"]])
    gh.comment_issue.assert_called_once()
    assert load_job(str(fdir / "jobs"), "job-3")["stage"] == "needs-info"


def test_cli_plan_prints_waves_and_verdicts(tmp_path, monkeypatch, capsys):
    from unittest.mock import MagicMock
    from factory import cli
    from factory.lib.batch import BatchPlan, BatchSpec

    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "config.json").write_text("{}", encoding="utf-8")
    fake_gh = MagicMock()
    fake_gh.list_queue_issues.return_value = [
        {"number": 1, "title": "Fix A", "body": "", "url": "https://x/1"},
    ]
    monkeypatch.setattr(cli, "GitHubClient", lambda repo: fake_gh)
    fake_plan = BatchPlan(
        accepted=[_item(1, weight=2, files=["factory/cli.py"])],
        rejected=[],
        needs_info=[],
        batches=[
            BatchSpec(
                id="batch-1",
                issues=[_item(1, weight=2, files=["factory/cli.py"])],
                solo_only=False,
                total_weight=2,
                predicted_files=["factory/cli.py"],
                hard_deps={1: set()},
            )
        ],
        batch_deps={"batch-1": set()},
        hard_batch_deps={"batch-1": set()},
        hard_deps={1: set()},
        reasoning="1 batch",
    )
    monkeypatch.setattr(cli, "triage_and_plan_queue", lambda *a, **kw: fake_plan)

    rc = cli.main(["plan", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Wave 1" in out
    assert "batch-1" in out
    assert "#1" in out


def _init_temp_git_repo(tmp_path):
    import json
    import subprocess

    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "main"], check=True, capture_output=True)

    fdir = repo / "factory"
    (fdir / "jobs").mkdir(parents=True)
    cfg = {
        "allow_self_host": False,
        "base_branch": "main",
        "merge_method": "squash",
        "auto_merge": "low",
        "protected_paths": ["factory/", ".github/"],
    }
    (fdir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    checks = fdir / "checks.sh"
    checks.write_text("#!/usr/bin/env bash\nset -euo pipefail\ntest ! -f FAIL_GATE\n", encoding="utf-8")
    checks.chmod(0o755)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return origin, repo


def _git_commit_file(wt: str, filename: str, content: str, msg: str) -> str:
    import os
    import subprocess

    p = os.path.join(wt, filename)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["git", "-C", wt, "add", filename], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-m", msg], check=True, capture_output=True)
    res = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def test_execute_batch_happy_path_three_issues_non_squash_and_proportional_metrics(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import BatchSpec, execute_batch
    from factory.lib.config import load_config
    from factory.lib.state import enqueue_job, load_job

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    jobs_dir = str(repo / "factory" / "jobs")
    items = [
        _item(1, weight=1, files=["f1.py"]),
        _item(2, weight=2, files=["f2.py"]),
        _item(3, weight=3, files=["f3.py"]),
    ]
    for it in items:
        enqueue_job(jobs_dir, {"number": it.issue, "title": it.title, "body": it.body, "url": it.url}, max_rounds=3)

    batch = BatchSpec(id="batch-1", issues=items, solo_only=False, total_weight=6, predicted_files=["f1.py", "f2.py", "f3.py"])

    from factory.lib.git_ops import GitHubClient
    gh = MagicMock(spec=GitHubClient)
    gh.ensure_draft_pr.return_value = (50, "https://github.com/o/r/pull/50")

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        if stage == "batch_build":
            s1 = _git_commit_file(wt, "f1.py", "x = 1\n", "feat: one (#1)")
            s2 = _git_commit_file(wt, "f2.py", "y = 2\n", "feat: two (#2)")
            s3 = _git_commit_file(wt, "f3.py", "z = 3\n", "feat: three (#3)")
            return (
                {
                    "results": [
                        {"issue": 1, "status": "DONE", "commit": s1, "summary": ["one"], "changes": ["f1.py"], "tests_run": ["t1"], "blocked_reason": ""},
                        {"issue": 2, "status": "DONE", "commit": s2, "summary": ["two"], "changes": ["f2.py"], "tests_run": ["t2"], "blocked_reason": ""},
                        {"issue": 3, "status": "DONE", "commit": s3, "summary": ["three"], "changes": ["f3.py"], "tests_run": ["t3"], "blocked_reason": ""},
                    ],
                    "notes": "ok",
                },
                {"input_tokens": 600, "output_tokens": 120, "total_tokens": 720},
                6.0,
            )
        if stage == "review":
            return ({"verdict": "PASS", "summary": ["clean"], "findings": [], "checked": ["f1.py"]}, {"total_tokens": 60}, 1.0)
        raise AssertionError(f"Unexpected stage {stage}")

    outcome = execute_batch(str(repo), batch, config=cfg, gh=gh, agent_runner=fake_agent)
    assert outcome.merged_issues == [1, 2, 3]
    assert outcome.failed_issues == []
    # Multi-issue batch overrides "squash" -> "merge" to preserve per-issue commits.
    gh.merge_pr.assert_called_once_with(50, "merge")
    pr_args = gh.ensure_draft_pr.call_args.args
    assert "Closes #1" in pr_args[4] and "Closes #2" in pr_args[4] and "Closes #3" in pr_args[4]
    gh.mark_pr_ready.assert_called_once_with(50)
    assert not (repo / ".worktrees" / "batch-1").exists()

    j1 = load_job(jobs_dir, "job-1")
    j2 = load_job(jobs_dir, "job-2")
    j3 = load_job(jobs_dir, "job-3")
    assert j1["stage"] == "merged" and j2["stage"] == "merged" and j3["stage"] == "merged"
    assert j1["usage"]["total_tokens"] < j2["usage"]["total_tokens"] < j3["usage"]["total_tokens"]


def test_execute_batch_handles_blocked_and_skipped_dep_mid_batch(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import BatchSpec, execute_batch
    from factory.lib.config import load_config
    from factory.lib.state import enqueue_job, load_job

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    jobs_dir = str(repo / "factory" / "jobs")
    items = [_item(1, weight=1, files=["f1.py"]), _item(2, weight=1, files=["f2.py"]), _item(3, weight=1, files=["f3.py"], deps=[2])]
    for it in items:
        enqueue_job(jobs_dir, {"number": it.issue, "title": it.title, "body": it.body, "url": it.url}, max_rounds=3)
    batch = BatchSpec(id="batch-1", issues=items, solo_only=False, total_weight=3, predicted_files=["f1.py", "f2.py", "f3.py"], hard_deps={1: set(), 2: set(), 3: {2}})
    from factory.lib.git_ops import GitHubClient
    gh = MagicMock(spec=GitHubClient)
    gh.ensure_draft_pr.return_value = (51, "https://github.com/o/r/pull/51")

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        if stage == "batch_build":
            s1 = _git_commit_file(wt, "f1.py", "x = 1\n", "feat: one (#1)")
            return (
                {
                    "results": [
                        {"issue": 1, "status": "DONE", "commit": s1, "summary": ["one"], "changes": ["f1.py"], "tests_run": [], "blocked_reason": ""},
                        {"issue": 2, "status": "BLOCKED", "commit": "", "summary": [], "changes": [], "tests_run": [], "blocked_reason": "Needs secret"},
                        {"issue": 3, "status": "SKIPPED_DEP", "commit": "", "summary": [], "changes": [], "tests_run": [], "blocked_reason": "Depends on #2"},
                    ],
                    "notes": "partial",
                },
                {"total_tokens": 100},
                2.0,
            )
        return ({"verdict": "PASS", "summary": ["clean"], "findings": [], "checked": ["f1.py"]}, {"total_tokens": 20}, 0.5)

    outcome = execute_batch(str(repo), batch, config=cfg, gh=gh, agent_runner=fake_agent)
    assert outcome.merged_issues == [1]
    assert set(outcome.failed_issues) == {2, 3}
    assert load_job(jobs_dir, "job-1")["stage"] == "merged"
    assert load_job(jobs_dir, "job-2")["stage"] == "needs-human"
    assert load_job(jobs_dir, "job-3")["stage"] == "needs-human"


def test_execute_batch_audits_missing_commit_for_done_issue(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import BatchSpec, execute_batch
    from factory.lib.config import load_config
    from factory.lib.state import enqueue_job, load_job

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    jobs_dir = str(repo / "factory" / "jobs")
    items = [_item(1, weight=1, files=["f1.py"])]
    enqueue_job(jobs_dir, {"number": 1, "title": "Issue 1", "body": "", "url": ""}, max_rounds=3)
    batch = BatchSpec(id="batch-1", issues=items, solo_only=False, total_weight=1, predicted_files=["f1.py"])
    gh = MagicMock()

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        return (
            {
                "results": [{"issue": 1, "status": "DONE", "commit": "", "summary": ["lied"], "changes": [], "tests_run": [], "blocked_reason": ""}],
                "notes": "",
            },
            {"total_tokens": 50},
            1.0,
        )

    outcome = execute_batch(str(repo), batch, config=cfg, gh=gh, agent_runner=fake_agent)
    assert outcome.merged_issues == []
    assert outcome.failed_issues == [1]
    assert load_job(jobs_dir, "job-1")["stage"] == "needs-human"


def test_salvage_passing_commits_drops_broken_commit_and_ships_healthy_subset(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import BatchSpec, execute_batch
    from factory.lib.config import load_config
    from factory.lib.state import enqueue_job, load_job

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    cfg["max_rounds"] = 1  # Trigger salvage immediately after first gate failure
    jobs_dir = str(repo / "factory" / "jobs")
    items = [_item(1, weight=1, files=["f1.py"]), _item(2, weight=1, files=["FAIL_GATE"]), _item(3, weight=1, files=["f3.py"])]
    for it in items:
        enqueue_job(jobs_dir, {"number": it.issue, "title": it.title, "body": it.body, "url": it.url}, max_rounds=1)
    batch = BatchSpec(id="batch-1", issues=items, solo_only=False, total_weight=3, predicted_files=["f1.py", "FAIL_GATE", "f3.py"])
    from factory.lib.git_ops import GitHubClient
    gh = MagicMock(spec=GitHubClient)
    gh.ensure_draft_pr.return_value = (52, "https://github.com/o/r/pull/52")

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        if stage == "batch_build":
            s1 = _git_commit_file(wt, "f1.py", "x = 1\n", "feat: one (#1)")
            s2 = _git_commit_file(wt, "FAIL_GATE", "bad\n", "feat: break gate (#2)")
            s3 = _git_commit_file(wt, "f3.py", "z = 3\n", "feat: three (#3)")
            return (
                {
                    "results": [
                        {"issue": 1, "status": "DONE", "commit": s1, "summary": ["one"], "changes": ["f1.py"], "tests_run": [], "blocked_reason": ""},
                        {"issue": 2, "status": "DONE", "commit": s2, "summary": ["two"], "changes": ["FAIL_GATE"], "tests_run": [], "blocked_reason": ""},
                        {"issue": 3, "status": "DONE", "commit": s3, "summary": ["three"], "changes": ["f3.py"], "tests_run": [], "blocked_reason": ""},
                    ],
                    "notes": "ok",
                },
                {"total_tokens": 150},
                3.0,
            )
        return ({"verdict": "PASS", "summary": ["clean"], "findings": [], "checked": ["f1.py", "f3.py"]}, {"total_tokens": 30}, 0.5)

    outcome = execute_batch(str(repo), batch, config=cfg, gh=gh, agent_runner=fake_agent)
    assert outcome.merged_issues == [1, 3]
    assert outcome.failed_issues == [2]
    assert load_job(jobs_dir, "job-1")["stage"] == "merged"
    assert load_job(jobs_dir, "job-2")["stage"] == "needs-human"
    assert load_job(jobs_dir, "job-3")["stage"] == "merged"


def test_run_batch_factory_skips_downstream_dependent_of_failed_parent(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import run_batch_factory
    from factory.lib.config import load_config
    from factory.lib.state import load_job

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    cfg["max_rounds"] = 1
    issues = [
        {"number": 1, "title": "Base A", "body": "a", "url": "https://x/1"},
        {"number": 2, "title": "Base B", "body": "b", "url": "https://x/2"},
        {"number": 3, "title": "Child of 2", "body": "Depends on #2", "url": "https://x/3"},
        {"number": 4, "title": "Child of 1", "body": "Depends on #1", "url": "https://x/4"},
    ]
    from factory.lib.git_ops import GitHubClient
    gh = MagicMock(spec=GitHubClient)
    gh.ensure_draft_pr.return_value = (60, "https://github.com/o/r/pull/60")

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        if stage == "batch_plan":
            return (
                {
                    "items": [
                        {"issue": 1, "decision": "ACCEPT", "risk": "low", "surfaces": [], "complexity_weight": 3, "predicted_files": ["a.py"], "depends_on": [], "topic_group": "", "summary": "1", "questions": [], "decision_reason": ""},
                        {"issue": 2, "decision": "ACCEPT", "risk": "low", "surfaces": [], "complexity_weight": 3, "predicted_files": ["a.py"], "depends_on": [], "topic_group": "", "summary": "2", "questions": [], "decision_reason": ""},
                        {"issue": 3, "decision": "ACCEPT", "risk": "low", "surfaces": [], "complexity_weight": 2, "predicted_files": ["a.py"], "depends_on": [2], "topic_group": "", "summary": "3", "questions": [], "decision_reason": ""},
                        {"issue": 4, "decision": "ACCEPT", "risk": "low", "surfaces": [], "complexity_weight": 2, "predicted_files": ["a.py"], "depends_on": [1], "topic_group": "", "summary": "4", "questions": [], "decision_reason": ""},
                    ],
                    "reasoning": "two waves",
                },
                {"total_tokens": 100},
                1.0,
            )
        if stage == "batch_build":
            if "[#1" in prompt:
                s1 = _git_commit_file(wt, "a.py", "1\n", "feat: one (#1)")
                return (
                    {
                        "results": [
                            {"issue": 1, "status": "DONE", "commit": s1, "summary": ["1"], "changes": ["a.py"], "tests_run": [], "blocked_reason": ""},
                            {"issue": 2, "status": "BLOCKED", "commit": "", "summary": [], "changes": [], "tests_run": [], "blocked_reason": "Broken"},
                        ],
                        "notes": "",
                    },
                    {"total_tokens": 80},
                    1.0,
                )
            s4 = _git_commit_file(wt, "a4.py", "4\n", "feat: four (#4)")
            return (
                {
                    "results": [
                        {"issue": 4, "status": "DONE", "commit": s4, "summary": ["4"], "changes": ["a4.py"], "tests_run": [], "blocked_reason": ""},
                    ],
                    "notes": "",
                },
                {"total_tokens": 50},
                1.0,
            )
        return ({"verdict": "PASS", "summary": ["ok"], "findings": [], "checked": ["a.py"]}, {"total_tokens": 20}, 0.5)

    jobs = run_batch_factory(str(repo), issues, config=cfg, gh=gh, agent_runner=fake_agent)
    by_id = {j["id"]: j for j in jobs}
    assert by_id["job-1"]["stage"] == "merged"
    assert by_id["job-2"]["stage"] == "needs-human"
    assert by_id["job-3"]["stage"] == "needs-human"
    assert by_id["job-4"]["stage"] == "merged"


def test_run_factory_uses_batch_by_default_and_respects_no_batch_and_max_batch(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from factory import cli
    from factory.lib import pipeline

    _, repo = _init_temp_git_repo(tmp_path)
    fake_gh = MagicMock()
    fake_gh.list_queue_issues.return_value = [
        {"number": 1, "title": "One", "body": "", "url": "https://x/1"},
        {"number": 2, "title": "Two", "body": "", "url": "https://x/2"},
    ]
    monkeypatch.setattr(pipeline, "GitHubClient", lambda r: fake_gh)
    monkeypatch.setattr(pipeline.ProjectBoardSync, "discover", lambda r: None)

    batch_calls = []
    legacy_calls = []

    def fake_batch_factory(repo_root, issues, *, config, gh, board=None, state_store=None):
        batch_calls.append((len(issues), config["batch"]["max_batch_issues"]))
        return [{"id": f"job-{iss['number']}", "issue": iss["number"], "stage": "merged"} for iss in issues]

    def fake_process_job(repo_root, issue_num, **kwargs):
        legacy_calls.append(issue_num)
        return {"id": f"job-{issue_num}", "issue": issue_num, "stage": "merged"}

    monkeypatch.setattr(pipeline, "run_batch_factory", fake_batch_factory)
    monkeypatch.setattr(pipeline, "process_job", fake_process_job)

    # Default -> batch mode
    res = pipeline.run_factory(str(repo))
    assert len(res) == 2
    assert batch_calls == [(2, 5)]
    assert legacy_calls == []

    # CLI --max-batch 2
    rc = cli.main(["run", "--repo", str(repo), "--max-batch", "2"])
    assert rc == 0
    assert batch_calls[-1] == (2, 2)

    # CLI --no-batch -> legacy process_job
    rc2 = cli.main(["run", "--repo", str(repo), "--no-batch"])
    assert rc2 == 0
    assert sorted(legacy_calls) == [1, 2]


def test_run_batch_factory_skips_issue_whose_prereq_was_rejected_or_needs_info(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import run_batch_factory
    from factory.lib.config import load_config

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    issues = [
        {"number": 1, "title": "Vague Base", "body": "unclear", "url": "https://x/1"},
        {"number": 2, "title": "Dependent", "body": "Depends on #1", "url": "https://x/2"},
    ]
    gh = MagicMock()
    built = []

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        if stage == "batch_plan":
            return (
                {
                    "items": [
                        {"issue": 1, "decision": "NEEDS_INFO", "risk": "low", "surfaces": [], "complexity_weight": 1, "predicted_files": [], "depends_on": [], "topic_group": "", "summary": "1", "questions": ["What?"], "decision_reason": ""},
                        {"issue": 2, "decision": "ACCEPT", "risk": "low", "surfaces": [], "complexity_weight": 1, "predicted_files": ["a.py"], "depends_on": [1], "topic_group": "", "summary": "2", "questions": [], "decision_reason": ""},
                    ],
                    "reasoning": "1 needs info",
                },
                {"total_tokens": 20},
                0.5,
            )
        built.append(stage)
        raise AssertionError("Should not build #2 when prerequisite #1 is NEEDS_INFO")

    jobs = run_batch_factory(str(repo), issues, config=cfg, gh=gh, agent_runner=fake_agent)
    by_id = {j["id"]: j for j in jobs}
    assert by_id["job-1"]["stage"] == "needs-info"
    assert by_id["job-2"]["stage"] == "needs-human"
    assert built == []


def test_execute_batch_enforces_max_job_cost_usd_budget(tmp_path):
    from unittest.mock import MagicMock
    from factory.lib.batch import BatchSpec, execute_batch
    from factory.lib.config import load_config
    from factory.lib.state import enqueue_job, load_job

    _, repo = _init_temp_git_repo(tmp_path)
    cfg = load_config(str(repo / "factory"))
    cfg["max_job_cost_usd"] = 0.01
    jobs_dir = str(repo / "factory" / "jobs")
    items = [_item(1, weight=1, files=["f1.py"])]
    enqueue_job(jobs_dir, {"number": 1, "title": "Issue 1", "body": "", "url": ""}, max_rounds=3)
    batch = BatchSpec(id="batch-1", issues=items, solo_only=False, total_weight=1, predicted_files=["f1.py"])
    gh = MagicMock()

    def fake_agent(factory_dir, job_dir, wt, stage, agent_name, model, prompt, schema, timeout_sec, **kwargs):
        if stage == "batch_build":
            s1 = _git_commit_file(wt, "f1.py", "x = 1\n", "feat: one (#1)")
            return (
                {
                    "results": [{"issue": 1, "status": "DONE", "commit": s1, "summary": ["one"], "changes": ["f1.py"], "tests_run": [], "blocked_reason": ""}],
                    "notes": "",
                },
                {"cost_usd": 0.05, "total_tokens": 1000},
                1.0,
            )
        return ({"verdict": "PASS", "summary": ["ok"], "findings": [], "checked": ["f1.py"]}, {"total_tokens": 10}, 0.1)

    outcome = execute_batch(str(repo), batch, config=cfg, gh=gh, agent_runner=fake_agent)
    assert outcome.merged_issues == []
    assert outcome.failed_issues == [1]
    assert load_job(jobs_dir, "job-1")["stage"] == "needs-human"
    gh.ensure_draft_pr.assert_not_called()
