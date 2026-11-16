"""Smart batch triage, Batch-DAG packing, and hot batch execution engine."""
from __future__ import annotations

from dataclasses import dataclass, field
import graphlib

from factory.lib.planner import (
    _ensure_acyclic,
    _has_path,
    _normalize_conflict_surfaces,
    _surfaces_overlap,
)


@dataclass
class TriageItem:
    issue: int
    title: str
    body: str
    url: str
    decision: str
    risk: str
    surfaces: list[str]
    complexity_weight: int
    predicted_files: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)
    topic_group: str = ""
    summary: str = ""
    questions: list[str] = field(default_factory=list)
    decision_reason: str = ""


@dataclass
class BatchSpec:
    id: str
    issues: list[TriageItem]
    solo_only: bool
    total_weight: int
    predicted_files: list[str]
    hard_deps: dict[int, set[int]] = field(default_factory=dict)


@dataclass
class BatchPlan:
    accepted: list[TriageItem]
    rejected: list[TriageItem]
    needs_info: list[TriageItem]
    batches: list[BatchSpec]
    batch_deps: dict[str, set[str]]
    hard_batch_deps: dict[str, set[str]]
    hard_deps: dict[int, set[int]]
    reasoning: str = ""


def _is_auto_merge_eligible(item: TriageItem, config: dict) -> bool:
    auto_merge = str(config.get("auto_merge", "low")).strip().lower()
    if auto_merge == "all":
        return True
    if auto_merge == "low":
        return item.risk == "low"
    return False


def _connected_components(
    items: list[TriageItem],
    clean_deps: dict[int, set[int]],
    solo_map: dict[int, bool],
) -> dict[int, int]:
    """Return `{issue_num: component_id}` grouping non-solo issues by deps, files, and topic_group."""
    adj: dict[int, set[int]] = {it.issue: set() for it in items}
    non_solo = [it for it in items if not solo_map[it.issue]]
    for idx, a in enumerate(non_solo):
        for b in non_solo[idx + 1 :]:
            linked = (
                b.issue in clean_deps.get(a.issue, set())
                or a.issue in clean_deps.get(b.issue, set())
                or _surfaces_overlap(a.predicted_files, b.predicted_files)
                or (bool(a.topic_group.strip()) and a.topic_group.strip() == b.topic_group.strip())
            )
            if linked:
                adj[a.issue].add(b.issue)
                adj[b.issue].add(a.issue)

    comp_id: dict[int, int] = {}
    current = 0
    for it in sorted(items, key=lambda x: x.issue):
        if it.issue in comp_id:
            continue
        stack = [it.issue]
        comp_id[it.issue] = current
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, set()):
                if nxt not in comp_id:
                    comp_id[nxt] = current
                    stack.append(nxt)
        current += 1
    return comp_id


def pack_batches(
    items: list[TriageItem],
    hard_deps: dict[int, set[int]],
    config: dict,
) -> BatchPlan:
    """Deterministically pack accepted issues into an acyclic parallel/sequential Batch-DAG."""
    accepted = [it for it in items if it.decision == "ACCEPT"]
    rejected = [it for it in items if it.decision == "REJECT"]
    needs_info = [it for it in items if it.decision == "NEEDS_INFO"]

    by_id: dict[int, TriageItem] = {it.issue: it for it in accepted}
    accepted_ids = set(by_id.keys())
    clean_deps = _ensure_acyclic(
        {num: set(hard_deps.get(num, set())) & accepted_ids for num in accepted_ids}
    )

    batch_cfg = config.get("batch") or {}
    max_issues = max(1, int(batch_cfg.get("max_batch_issues", 5)))
    max_weight = max(1, int(batch_cfg.get("max_batch_weight", 6)))

    solo_map: dict[int, bool] = {
        it.issue: (max_issues == 1 or not _is_auto_merge_eligible(it, config))
        for it in accepted
    }
    comp_map = _connected_components(accepted, clean_deps, solo_map)
    topo_order = list(graphlib.TopologicalSorter(clean_deps).static_order())

    batches: list[BatchSpec] = []
    issue_to_batch: dict[int, str] = {}
    batch_comp: dict[str, int] = {}
    tentative_deps: dict[str, set[str]] = {}

    def _would_cycle(candidate_bid: str, dep_issue_ids: set[int]) -> bool:
        dep_batches = {
            issue_to_batch[d]
            for d in dep_issue_ids
            if d in issue_to_batch and issue_to_batch[d] != candidate_bid
        }
        for db in dep_batches:
            if _has_path(tentative_deps, db, candidate_bid):
                return True
        return False

    for num in topo_order:
        it = by_id[num]
        u_deps = clean_deps.get(num, set())
        u_files = _normalize_conflict_surfaces(it.predicted_files)
        is_solo = solo_map[num]

        chosen: BatchSpec | None = None
        if not is_solo:
            best_score = (-1, -1)
            for b in batches:
                if b.solo_only or batch_comp[b.id] != comp_map[num]:
                    continue
                if len(b.issues) >= max_issues:
                    continue
                if b.total_weight + it.complexity_weight > max_weight:
                    continue
                if _would_cycle(b.id, u_deps):
                    continue
                b_ids = {x.issue for x in b.issues}
                shares_dep = 1 if (u_deps & b_ids) else 0
                shares_file = 1 if _surfaces_overlap(b.predicted_files, u_files) else 0
                score = (shares_dep, shares_file)
                if score > best_score:
                    best_score = score
                    chosen = b

        if chosen is None:
            bid = f"batch-{len(batches) + 1}"
            chosen = BatchSpec(
                id=bid,
                issues=[],
                solo_only=is_solo,
                total_weight=0,
                predicted_files=[],
                hard_deps={},
            )
            batches.append(chosen)
            batch_comp[bid] = comp_map[num]
            tentative_deps[bid] = set()

        chosen.issues.append(it)
        chosen.total_weight += it.complexity_weight
        chosen.hard_deps[num] = set(u_deps)
        for pf in u_files:
            if pf not in chosen.predicted_files:
                chosen.predicted_files.append(pf)
        issue_to_batch[num] = chosen.id
        for d in u_deps:
            db = issue_to_batch.get(d)
            if db and db != chosen.id:
                tentative_deps[chosen.id].add(db)

    hard_batch_deps: dict[str, set[str]] = {b.id: set() for b in batches}
    for b in batches:
        for it in b.issues:
            for d in clean_deps.get(it.issue, set()):
                db = issue_to_batch.get(d)
                if db and db != b.id:
                    hard_batch_deps[b.id].add(db)

    batch_deps: dict[str, set[str]] = {bid: set(deps) for bid, deps in hard_batch_deps.items()}
    for idx, b1 in enumerate(batches):
        if not b1.predicted_files:
            continue
        for b2 in batches[idx + 1 :]:
            if not b2.predicted_files:
                continue
            if _surfaces_overlap(b1.predicted_files, b2.predicted_files):
                if not _has_path(batch_deps, b1.id, b2.id) and not _has_path(batch_deps, b2.id, b1.id):
                    batch_deps[b2.id].add(b1.id)

    graphlib.TopologicalSorter(batch_deps).prepare()
    return BatchPlan(
        accepted=accepted,
        rejected=rejected,
        needs_info=needs_info,
        batches=batches,
        batch_deps=batch_deps,
        hard_batch_deps=hard_batch_deps,
        hard_deps=clean_deps,
    )
