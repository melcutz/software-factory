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
