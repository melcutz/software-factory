"""Smart batch triage, Batch-DAG packing, and hot batch execution engine."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import graphlib
import os

from factory.lib.agy_runner import run_agent
from factory.lib.config import estimate_usage_cost_usd
from factory.lib.git_ops import GitHubClient, format_untrusted_issue_md
from factory.lib.onboarding import generate_repo_map
from factory.lib.planner import (
    _ensure_acyclic,
    _has_path,
    _normalize_conflict_surfaces,
    _surfaces_overlap,
    parse_explicit_dependencies,
)
from factory.lib.project_board import ProjectBoardSync
from factory.lib.schemas import render_intake_md
from factory.lib.state import advance_stage, enqueue_job, record_metrics, record_usage


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


def _refresh_repo_map_if_needed(repo_root: str, factory_dir: str, config: dict, *, dry_run: bool) -> str:
    map_text = generate_repo_map(repo_root)
    if not dry_run and (config.get("batch") or {}).get("refresh_repo_map", True):
        map_path = os.path.join(factory_dir, "REPO_MAP.md")
        try:
            with open(map_path, "w", encoding="utf-8") as f:
                f.write(map_text)
        except OSError:
            pass
    return map_text


def _build_batch_planner_prompt(issues: list[dict], repo_map: str) -> str:
    issue_blocks = [format_untrusted_issue_md(iss) for iss in issues]
    parts = [
        "Triage the following queued GitHub issues and return a single `batch_plan` JSON payload.",
        "",
        "## Repository Map",
        repo_map.strip() or "(unavailable)",
        "",
        "## Queued Issues",
        *issue_blocks,
    ]
    return "\n\n".join(parts)


def _transition_non_accept(
    jobs_dir: str,
    item: TriageItem,
    config: dict,
    gh: GitHubClient,
    board: ProjectBoardSync | None,
) -> None:
    job_id = f"job-{item.issue}"
    jdir = os.path.join(jobs_dir, job_id)
    os.makedirs(jdir, exist_ok=True)
    intake_payload = {
        "decision": item.decision,
        "risk": item.risk,
        "surfaces": item.surfaces,
        "summary": [item.summary] if item.summary else [item.title],
        "scope_in": item.summary or item.title,
        "scope_out": "",
        "questions": item.questions,
        "reasoning": item.decision_reason or item.summary or item.decision,
    }
    with open(os.path.join(jdir, "INTAKE.md"), "w", encoding="utf-8") as f:
        f.write(render_intake_md(intake_payload))

    if item.decision == "NEEDS_INFO":
        advance_stage(jobs_dir, job_id, "intake")
        advance_stage(jobs_dir, job_id, "needs-info")
        gh.add_label(item.issue, config["labels"]["needs_info"])
        q_lines = "\n".join(f"- {q}" for q in item.questions if str(q).strip())
        gh.comment_issue(item.issue, f"Factory needs more information before building:\n\n{q_lines}")
        if board:
            board.sync_stage(item.url, "needs-info")
    elif item.decision == "REJECT":
        advance_stage(jobs_dir, job_id, "intake")
        advance_stage(jobs_dir, job_id, "rejected")
        reason = item.decision_reason or "Rejected during batch triage."
        gh.comment_issue(item.issue, f"Factory rejected this issue during triage:\n\n{reason}")
        if board:
            board.sync_stage(item.url, "rejected")


def triage_and_plan_queue(
    repo_root: str,
    issues: list[dict],
    *,
    config: dict,
    gh: GitHubClient,
    board: ProjectBoardSync | None = None,
    dry_run: bool = False,
    planner_runner: Callable[..., tuple[dict, dict, float]] | None = None,
) -> BatchPlan:
    """Triage all queued issues in one fast planner call and pack an acyclic Batch-DAG."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)

    if not issues:
        return BatchPlan([], [], [], [], {}, {}, {}, reasoning="Empty queue")

    repo_map = _refresh_repo_map_if_needed(repo_root, factory_dir, config, dry_run=dry_run)
    explicit_deps = parse_explicit_dependencies(issues)
    issue_by_num = {int(iss["number"]): iss for iss in issues}

    prompt = _build_batch_planner_prompt(issues, repo_map)
    model = str((config.get("models") or {}).get("planner") or "gemini-3.8-flash-low")
    if planner_runner is not None:
        payload, usage, duration = planner_runner(
            prompt,
            repo_root=repo_root,
            factory_dir=factory_dir,
            model=model,
        )
    else:
        payload, usage, duration = run_agent(
            repo_root,
            factory_dir,
            "batch-planner",
            "batch_plan",
            model,
            prompt,
            timeout_sec=int(config.get("agent_timeout_sec", 900)),
            read_only=True,
            config=config,
        )

    raw_by_num = {int(entry["issue"]): entry for entry in (payload.get("items") or []) if int(entry["issue"]) in issue_by_num}
    triage_items: list[TriageItem] = []
    hard_deps: dict[int, set[int]] = {}
    for num in sorted(issue_by_num):
        iss = issue_by_num[num]
        raw = raw_by_num.get(num)
        if raw is None:
            deps = sorted(explicit_deps.get(num, set()))
            triage_items.append(
                TriageItem(
                    issue=num,
                    title=str(iss.get("title") or ""),
                    body=str(iss.get("body") or ""),
                    url=str(iss.get("url") or ""),
                    decision="ACCEPT",
                    risk="medium",
                    surfaces=[],
                    complexity_weight=3,
                    predicted_files=[],
                    depends_on=deps,
                    summary=str(iss.get("title") or ""),
                )
            )
            hard_deps[num] = set(deps)
        else:
            merged_deps = sorted(
                (explicit_deps.get(num, set()) | {int(d) for d in (raw.get("depends_on") or []) if int(d) in issue_by_num and int(d) != num})
            )
            triage_items.append(
                TriageItem(
                    issue=num,
                    title=str(iss.get("title") or ""),
                    body=str(iss.get("body") or ""),
                    url=str(iss.get("url") or ""),
                    decision=str(raw["decision"]),
                    risk=str(raw["risk"]),
                    surfaces=list(raw.get("surfaces") or []),
                    complexity_weight=int(raw["complexity_weight"]),
                    predicted_files=list(raw.get("predicted_files") or []),
                    depends_on=merged_deps,
                    topic_group=str(raw.get("topic_group") or ""),
                    summary=str(raw.get("summary") or iss.get("title") or ""),
                    questions=[str(q) for q in (raw.get("questions") or [])],
                    decision_reason=str(raw.get("decision_reason") or ""),
                )
            )
            hard_deps[num] = set(merged_deps)

    plan = pack_batches(triage_items, hard_deps, config)
    plan.reasoning = str(payload.get("reasoning") or "")

    if not dry_run:
        max_rounds = int(config.get("max_rounds", 3))
        max_qa_rounds = int((config.get("staging_qa") or {}).get("max_qa_rounds", 3))
        n_items = max(1, len(triage_items))
        for it in triage_items:
            iss = issue_by_num[it.issue]
            enqueue_job(
                jobs_dir,
                iss,
                max_rounds=max_rounds,
                max_qa_rounds=max_qa_rounds,
                waiting_on=hard_deps.get(it.issue),
                pid=os.getpid(),
            )
            job_id = f"job-{it.issue}"
            if usage:
                scaled = {
                    k: int(v // n_items) if isinstance(v, int) else v
                    for k, v in usage.items()
                }
                cost = estimate_usage_cost_usd(model, scaled, config)
                record_usage(jobs_dir, job_id, scaled, duration / n_items, cost_usd=cost)
            if it.decision in {"NEEDS_INFO", "REJECT"}:
                _transition_non_accept(jobs_dir, it, config, gh, board)

    return plan


def format_batch_plan(plan: BatchPlan) -> str:
    """Format a BatchPlan into a human-readable wave & verdict summary."""
    lines = ["## Smart Batch Plan"]
    if plan.reasoning:
        lines.append(f"Reasoning: {plan.reasoning}")
    if plan.needs_info:
        lines.append("NEEDS_INFO: " + ", ".join(f"#{it.issue}" for it in plan.needs_info))
    if plan.rejected:
        lines.append("REJECT: " + ", ".join(f"#{it.issue}" for it in plan.rejected))
    if not plan.batches:
        lines.append("(no accepted batches)")
        return "\n".join(lines)

    by_id = {b.id: b for b in plan.batches}
    sorter = graphlib.TopologicalSorter(plan.batch_deps)
    sorter.prepare()
    wave_idx = 1
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        lines.append(f"\nWave {wave_idx} ({len(ready)} parallel batch(es)):")
        for bid in ready:
            b = by_id[bid]
            nums = ", ".join(f"#{it.issue}" for it in b.issues)
            mode = "solo" if b.solo_only else "batch"
            files = ", ".join(b.predicted_files[:5]) or "unspecified"
            deps = ", ".join(sorted(plan.batch_deps.get(bid, set()))) or "none"
            lines.append(f"  - {bid} [{mode}] issues=[{nums}] weight={b.total_weight} deps=[{deps}] files=[{files}]")
            sorter.done(bid)
        wave_idx += 1
    return "\n".join(lines)
