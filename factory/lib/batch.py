"""Smart batch triage, Batch-DAG packing, and hot batch execution engine."""
from __future__ import annotations

from collections.abc import Callable
import concurrent.futures
from dataclasses import dataclass, field
import graphlib
import os
import re
import threading

from factory.lib.agy_runner import QuotaExhaustedError, run_agent
from factory.lib.config import estimate_usage_cost_usd, resolve_reviewers
from factory.lib.gate_runner import find_protected_violations, run_gate
from factory.lib.git_ops import (
    GitHubClient,
    _run_git,
    changed_files_against_base,
    diff_stat,
    ensure_worktree,
    format_untrusted_issue_md,
    head_sha,
    push_branch,
    remove_worktree,
    repo_lock,
    sync_with_base,
)
from factory.lib.git_state import GitStateStore
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
from factory.lib.state import advance_stage, enqueue_job, load_job, record_metrics, record_usage, update_job


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
        gh.edit_labels(item.issue, add=[config["labels"]["needs_info"]])
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


@dataclass
class BatchExecutionOutcome:
    batch_id: str
    merged_issues: list[int]
    ready_issues: list[int]
    failed_issues: list[int]
    pr_url: str = ""


def _record_batch_usage(
    jobs_dir: str,
    issues: list[TriageItem],
    usage: dict | None,
    duration: float,
    model_spec: str,
    config: dict,
) -> None:
    if not issues or not usage:
        return
    total_w = sum(max(1, it.complexity_weight) for it in issues)
    for it in issues:
        ratio = max(1, it.complexity_weight) / total_w
        scaled = {
            k: int(round(v * ratio)) if isinstance(v, int) else v
            for k, v in usage.items()
        }
        cost = estimate_usage_cost_usd(model_spec, scaled, config)
        record_usage(jobs_dir, f"job-{it.issue}", scaled, duration * ratio, cost_usd=cost)


def _escalate_issue(
    jobs_dir: str,
    item: TriageItem,
    reason: str,
    config: dict,
    gh: GitHubClient,
    board: ProjectBoardSync | None,
) -> None:
    job_id = f"job-{item.issue}"
    try:
        cur = load_job(jobs_dir, job_id)
        if cur.get("stage") == "queued":
            advance_stage(jobs_dir, job_id, "intake")
            cur = load_job(jobs_dir, job_id)
        if cur.get("stage") != "needs-human":
            advance_stage(jobs_dir, job_id, "needs-human", last_error=reason)
    except Exception:
        pass
    try:
        gh.edit_labels(item.issue, add=[config["labels"]["needs_human"]])
        gh.comment_issue(item.issue, f"Factory escalated #{item.issue} to `needs-human`:\n\n{reason}")
    except Exception:
        pass
    if board:
        try:
            board.sync_stage(item.url, "needs-human")
        except Exception:
            pass


def _collect_issue_commits(
    worktree: str,
    base_branch: str,
    reported_results: dict[int, dict],
) -> dict[int, list[str]]:
    res = _run_git(worktree, "log", "--reverse", "--format=%H %s", f"origin/{base_branch}..HEAD", check=False)
    commits: list[tuple[str, str]] = []
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            if " " in line:
                sha, subj = line.split(" ", 1)
                commits.append((sha.strip(), subj.strip()))

    by_issue: dict[int, list[str]] = {}
    for sha, subj in commits:
        matched_nums = [int(m) for m in re.findall(r"\(#(\d+)\)", subj)]
        for num in matched_nums:
            if num in reported_results:
                by_issue.setdefault(num, []).append(sha)

    for num, entry in reported_results.items():
        if by_issue.get(num):
            continue
        rep_sha = str(entry.get("commit") or "").strip()
        if rep_sha:
            for sha, _ in commits:
                if sha.startswith(rep_sha):
                    by_issue.setdefault(num, []).append(sha)
                    break
    return by_issue


def salvage_passing_commits(
    repo_root: str,
    worktree: str,
    batch_branch: str,
    base_branch: str,
    ordered_issues: list[int],
    issue_commits: dict[int, list[str]],
    hard_deps: dict[int, set[int]],
    gate_fn: Callable[[str], tuple[bool, str]],
    protected_fn: Callable[[str], list[str]],
) -> tuple[list[int], dict[int, str]]:
    """Reset worktree to origin/<base_branch> and cherry-pick passing per-issue commits in order."""
    _run_git(worktree, "checkout", "-B", batch_branch, f"origin/{base_branch}")
    last_good = head_sha(worktree)
    passed: list[int] = []
    failed: dict[int, str] = {}

    for num in ordered_issues:
        broken_deps = [d for d in sorted(hard_deps.get(num, set())) if d in failed]
        if broken_deps:
            failed[num] = f"Prerequisite #{broken_deps[0]} failed batch salvage"
            continue
        shas = issue_commits.get(num) or []
        if not shas:
            failed[num] = "No commit found for issue during salvage"
            continue
        cp_ok = True
        cp_err = ""
        for sha in shas:
            res = _run_git(worktree, "cherry-pick", "--no-edit", sha, check=False)
            if res.returncode != 0:
                _run_git(worktree, "cherry-pick", "--abort", check=False)
                cp_ok = False
                cp_err = (res.stderr or res.stdout or "cherry-pick conflict").strip()
                break
        if not cp_ok:
            _run_git(worktree, "reset", "--hard", last_good)
            failed[num] = f"Cherry-pick failed during salvage: {cp_err}"
            continue

        prot = protected_fn(worktree)
        if prot:
            _run_git(worktree, "reset", "--hard", last_good)
            failed[num] = f"Protected paths modified: {', '.join(prot)}"
            continue

        ok, gate_out = gate_fn(worktree)
        if not ok:
            _run_git(worktree, "reset", "--hard", last_good)
            failed[num] = f"Gate failed on issue #{num}:\n{gate_out[-600:]}"
            continue

        last_good = head_sha(worktree)
        passed.append(num)

    return passed, failed


def _build_batch_builder_prompt(
    batch: BatchSpec,
    repo_map: str,
    *,
    fix_feedback: str = "",
) -> str:
    issue_sections = [format_untrusted_issue_md({"number": it.issue, "title": it.title, "body": it.body}) for it in batch.issues]
    order_list = ", ".join(f"#{it.issue}" for it in batch.issues)
    parts = [
        f"Execute batch `{batch.id}` in exact issue order: [{order_list}].",
        "Create one atomic commit per issue ending with `(#<n>)` and run only targeted tests during the build.",
        "",
        "## Repository Map",
        repo_map.strip() or "(unavailable)",
        "",
        "## Ordered Issues",
        *issue_sections,
    ]
    if fix_feedback:
        parts.extend(["", "## Gate / Review Feedback To Fix", fix_feedback])
    return "\n\n".join(parts)


def _run_batch_review(
    repo_root: str,
    factory_dir: str,
    worktree: str,
    batch: BatchSpec,
    surviving: list[TriageItem],
    config: dict,
    jobs_dir: str,
    agent_runner: Callable[..., tuple[dict, dict, float]],
) -> tuple[bool, str]:
    base_branch = config.get("base_branch", "main")
    stat = diff_stat(worktree, base_branch)
    surfaces = sorted({s for it in surviving for s in it.surfaces})
    max_risk = "low"
    for r in ("high", "medium", "low"):
        if any(it.risk == r for it in surviving):
            max_risk = r
            break
    reviewers = resolve_reviewers(config, max_risk, surfaces)
    roles = ["code"]
    if "security" in reviewers:
        roles.append("security")

    issue_summary = "\n".join(f"- #{it.issue}: {it.title}" for it in surviving)
    findings_text: list[str] = []
    passed = True
    for role in roles:
        model = reviewers[role]
        prompt = (
            f"Review the batch diff against `origin/{base_branch}` for issues:\n{issue_summary}\n\n"
            f"## Diff Stat\n{stat}\n"
        )
        payload, usage, dur = agent_runner(
            repo_root,
            factory_dir,
            f"reviewer-{role}",
            "review",
            model,
            prompt,
            worktree=worktree,
            timeout_sec=int(config.get("agent_timeout_sec", 900)),
            read_only=True,
            config=config,
        )
        _record_batch_usage(jobs_dir, surviving, usage, dur, model, config)
        if payload.get("verdict") != "PASS":
            passed = False
            for fl in payload.get("findings") or []:
                if fl.get("severity") in {"must_fix", "should_fix"}:
                    findings_text.append(f"[{role}/{fl.get('severity')}] {fl.get('file')}:{fl.get('line')} - {fl.get('issue')} (Fix: {fl.get('fix')})")
    return passed, "\n".join(findings_text)


def execute_batch(
    repo_root: str,
    batch: BatchSpec,
    *,
    config: dict,
    gh: GitHubClient,
    board: ProjectBoardSync | None = None,
    state_store: GitStateStore | None = None,
    agent_runner: Callable[..., tuple[dict, dict, float]] | None = None,
    repo_map: str = "",
) -> BatchExecutionOutcome:
    """Execute one BatchSpec in a single worktree and hot builder session."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    base_branch = str(config.get("base_branch", "main"))
    runner = agent_runner or run_agent

    if not repo_map:
        repo_map = generate_repo_map(repo_root)

    batch_branch = f"factory/{batch.id}"
    wt, batch_branch = ensure_worktree(repo_root, batch.id, base_branch, branch=batch_branch)
    for it in batch.issues:
        job_id = f"job-{it.issue}"
        update_job(jobs_dir, job_id, batch_id=batch.id, worktree=wt)
        advance_stage(jobs_dir, job_id, "intake")
        advance_stage(jobs_dir, job_id, "build")

    builder_model = str((config.get("models") or {}).get("batch_builder") or config["models"]["builder"])
    if (
        len(batch.issues) == 1
        and batch.issues[0].risk == "low"
        and batch.issues[0].complexity_weight == 1
        and (config.get("models") or {}).get("builder_low")
    ):
        builder_model = str(config["models"]["builder_low"])

    timeout_sec = min(2400, int(config.get("agent_timeout_sec", 900)) + 240 * (len(batch.issues) - 1))
    build_prompt = _build_batch_builder_prompt(batch, repo_map)
    build_payload, usage, dur = runner(
        repo_root,
        factory_dir,
        "batch-builder",
        "batch_build",
        builder_model,
        build_prompt,
        worktree=wt,
        timeout_sec=timeout_sec,
        require_clean_worktree=True,
        config=config,
    )
    _record_batch_usage(jobs_dir, batch.issues, usage, dur, builder_model, config)

    reported_by_num = {int(r["issue"]): r for r in (build_payload.get("results") or [])}
    issue_commits = _collect_issue_commits(wt, base_branch, reported_by_num)

    surviving: list[TriageItem] = []
    failed_issues: list[int] = []
    for it in batch.issues:
        res = reported_by_num.get(it.issue)
        if res is None:
            _escalate_issue(jobs_dir, it, "Builder omitted issue from batch_build results", config, gh, board)
            failed_issues.append(it.issue)
            continue
        status = res.get("status")
        if status in {"BLOCKED", "SKIPPED_DEP"}:
            reason = str(res.get("blocked_reason") or f"Builder marked {status}")
            _escalate_issue(jobs_dir, it, reason, config, gh, board)
            failed_issues.append(it.issue)
            continue
        if not issue_commits.get(it.issue):
            _escalate_issue(jobs_dir, it, "Builder reported DONE without creating a commit", config, gh, board)
            failed_issues.append(it.issue)
            continue
        update_job(jobs_dir, f"job-{it.issue}", commit_sha=issue_commits[it.issue][-1])
        surviving.append(it)

    if not surviving:
        remove_worktree(repo_root, wt)
        return BatchExecutionOutcome(batch.id, [], [], failed_issues)

    def _gate_once(worktree_path: str, round_num: int) -> tuple[bool, str]:
        ok, out = run_gate(repo_root, batch.id, worktree_path, round_num, config)
        return ok, out

    max_rounds = max(1, int(config.get("max_rounds", 3)))
    gate_ok = False
    review_ok = False
    feedback = ""
    for rnd in range(1, max_rounds + 1):
        for it in surviving:
            advance_stage(jobs_dir, f"job-{it.issue}", "gate")
        gate_ok, gate_out = _gate_once(wt, rnd)
        if gate_ok:
            for it in surviving:
                advance_stage(jobs_dir, f"job-{it.issue}", "review")
            review_ok, rev_out = _run_batch_review(repo_root, factory_dir, wt, batch, surviving, config, jobs_dir, runner)
            if review_ok:
                break
            feedback = f"Review findings:\n{rev_out}"
        else:
            feedback = f"Gate failed:\n{gate_out[-1200:]}"

        if rnd < max_rounds:
            for it in surviving:
                advance_stage(jobs_dir, f"job-{it.issue}", "build", last_error=feedback)
            fix_prompt = _build_batch_builder_prompt(batch, repo_map, fix_feedback=feedback)
            _, fix_usage, fix_dur = runner(
                repo_root,
                factory_dir,
                "batch-builder",
                "batch_build",
                builder_model,
                fix_prompt,
                worktree=wt,
                timeout_sec=timeout_sec,
                require_clean_worktree=True,
                config=config,
            )
            _record_batch_usage(jobs_dir, surviving, fix_usage, fix_dur, builder_model, config)

    if not (gate_ok and review_ok):
        if len(surviving) > 1 and not gate_ok:
            passed_nums, salvage_failures = salvage_passing_commits(
                repo_root,
                wt,
                batch_branch,
                base_branch,
                [it.issue for it in surviving],
                issue_commits,
                batch.hard_deps,
                gate_fn=lambda p: run_gate(repo_root, f"{batch.id}-salvage", p, 1, config),
                protected_fn=lambda p: find_protected_violations(
                    changed_files_against_base(p, base_branch),
                    config.get("protected_paths", []),
                    allow_self_host=bool(config.get("allow_self_host", False)),
                ),
            )
            by_num = {it.issue: it for it in surviving}
            for fnum, freason in salvage_failures.items():
                _escalate_issue(jobs_dir, by_num[fnum], freason, config, gh, board)
                failed_issues.append(fnum)
            surviving = [by_num[n] for n in passed_nums]
            if surviving:
                for it in surviving:
                    advance_stage(jobs_dir, f"job-{it.issue}", "review")
                review_ok, _ = _run_batch_review(repo_root, factory_dir, wt, batch, surviving, config, jobs_dir, runner)
                gate_ok = True

        if not surviving or not (gate_ok and review_ok):
            for it in surviving:
                _escalate_issue(jobs_dir, it, feedback or "Batch gate/review failed", config, gh, board)
                failed_issues.append(it.issue)
            remove_worktree(repo_root, wt)
            return BatchExecutionOutcome(batch.id, [], [], failed_issues)

    for it in surviving:
        advance_stage(jobs_dir, f"job-{it.issue}", "approve")

    if len(surviving) == 1:
        pr_title = f"{surviving[0].title} (#{surviving[0].issue})"
    else:
        nums_str = ", ".join(f"#{it.issue}" for it in surviving)
        pr_title = f"batch: {nums_str} ({len(surviving)} issues)"
    closes_lines = "\n".join(f"Closes #{it.issue}" for it in surviving)
    summary_lines = "\n".join(f"- #{it.issue}: {it.summary or it.title}" for it in surviving)
    pr_body = f"{closes_lines}\n\n## Batch Summary\n{summary_lines}\n"

    auto_merge_cfg = str(config.get("auto_merge", "low")).lower()
    can_auto_merge = auto_merge_cfg == "all" or (auto_merge_cfg == "low" and all(it.risk == "low" for it in surviving))
    merge_method = str(config.get("merge_method", "merge"))
    if len(surviving) > 1 and merge_method == "squash":
        merge_method = "merge"

    with repo_lock(repo_root):
        if can_auto_merge:
            ok_sync, sync_changed, sync_detail = sync_with_base(wt, base_branch)
            if not ok_sync:
                for it in surviving:
                    _escalate_issue(jobs_dir, it, f"Pre-merge sync conflict: {sync_detail}", config, gh, board)
                    failed_issues.append(it.issue)
                remove_worktree(repo_root, wt)
                return BatchExecutionOutcome(batch.id, [], [], failed_issues)
            if sync_changed:
                recheck_ok, _ = _gate_once(wt, max_rounds + 1)
                if not recheck_ok:
                    for it in surviving:
                        _escalate_issue(jobs_dir, it, "Post-rebase gate failed", config, gh, board)
                        failed_issues.append(it.issue)
                    remove_worktree(repo_root, wt)
                    return BatchExecutionOutcome(batch.id, [], [], failed_issues)
        push_branch(wt, batch_branch)
        pr = gh.ensure_draft_pr(branch=batch_branch, base=base_branch, title=pr_title, body=pr_body, draft=False)
        pr_num = int(pr["number"])
        pr_url = str(pr.get("url") or "")
        for it in surviving:
            update_job(jobs_dir, f"job-{it.issue}", pr_number=pr_num, pr_url=pr_url)

        if can_auto_merge:
            for it in surviving:
                advance_stage(jobs_dir, f"job-{it.issue}", "merging")
            gh.merge_pr(pr_num, merge_method)
            for it in surviving:
                advance_stage(jobs_dir, f"job-{it.issue}", "merged")
                if board:
                    board.sync_stage(it.url, "merged")
            remove_worktree(repo_root, wt)
            return BatchExecutionOutcome(batch.id, [it.issue for it in surviving], [], failed_issues, pr_url=pr_url)

    for it in surviving:
        advance_stage(jobs_dir, f"job-{it.issue}", "ready")
        if board:
            board.sync_stage(it.url, "ready")
    return BatchExecutionOutcome(batch.id, [], [it.issue for it in surviving], failed_issues, pr_url=pr_url)


def run_batch_factory(
    repo_root: str,
    issues: list[dict],
    *,
    config: dict,
    gh: GitHubClient,
    board: ProjectBoardSync | None = None,
    state_store: GitStateStore | None = None,
    agent_runner: Callable[..., tuple[dict, dict, float]] | None = None,
) -> list[dict]:
    """Triage a queue, pack a Batch-DAG, and execute waves in parallel/sequential order."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    if not issues:
        return []

    runner = agent_runner or run_agent

    def _planner_hook(prompt: str, **kw):
        model = kw["model"]
        return runner(
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

    plan = triage_and_plan_queue(
        repo_root,
        issues,
        config=config,
        gh=gh,
        board=board,
        dry_run=False,
        planner_runner=_planner_hook if agent_runner is not None else None,
    )
    if not plan.batches:
        return [load_job(jobs_dir, f"job-{int(iss['number'])}") for iss in issues]

    repo_map = generate_repo_map(repo_root)
    by_id = {b.id: b for b in plan.batches}
    sorter = graphlib.TopologicalSorter(plan.batch_deps)
    sorter.prepare()

    max_workers = max(1, int((config.get("batch") or {}).get("max_parallel_batches", 2)))
    unmerged_lock = threading.Lock()
    unmerged_issues: set[int] = set()

    def _run_one_batch(bid: str) -> BatchExecutionOutcome:
        b = by_id[bid]
        with unmerged_lock:
            bad = set(unmerged_issues)
        eligible: list[TriageItem] = []
        for it in b.issues:
            failed_deps = [d for d in sorted(plan.hard_deps.get(it.issue, set())) if d in bad]
            if failed_deps:
                _escalate_issue(
                    jobs_dir,
                    it,
                    f"Prerequisite #{failed_deps[0]} failed to merge",
                    config,
                    gh,
                    board,
                )
                with unmerged_lock:
                    unmerged_issues.add(it.issue)
            else:
                eligible.append(it)
        if not eligible:
            return BatchExecutionOutcome(bid, [], [], [it.issue for it in b.issues])
        filtered_batch = BatchSpec(
            id=b.id,
            issues=eligible,
            solo_only=b.solo_only,
            total_weight=sum(it.complexity_weight for it in eligible),
            predicted_files=list(b.predicted_files),
            hard_deps=b.hard_deps,
        )
        outcome = execute_batch(
            repo_root,
            filtered_batch,
            config=config,
            gh=gh,
            board=board,
            state_store=state_store,
            agent_runner=runner,
            repo_map=repo_map,
        )
        with unmerged_lock:
            unmerged_issues.update(outcome.failed_issues)
        return outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        in_flight: dict[concurrent.futures.Future, str] = {}
        while sorter.is_active():
            for bid in sorter.get_ready():
                in_flight[pool.submit(_run_one_batch, bid)] = bid
            if not in_flight:
                break
            done, _ = concurrent.futures.wait(
                in_flight.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                bid = in_flight.pop(fut)
                try:
                    fut.result()
                except QuotaExhaustedError:
                    raise
                except Exception as exc:
                    for it in by_id[bid].issues:
                        _escalate_issue(jobs_dir, it, str(exc), config, gh, board)
                        with unmerged_lock:
                            unmerged_issues.add(it.issue)
                sorter.done(bid)

    return [load_job(jobs_dir, f"job-{int(iss['number'])}") for iss in issues]
