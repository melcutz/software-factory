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
    gh.add_label.assert_not_called()
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
    gh.add_label.assert_called_once_with(3, cfg["labels"]["needs_info"])
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
