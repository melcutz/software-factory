"""End-to-end autonomous software factory pipeline orchestrator and doctor checks."""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys

from factory.lib.agy_runner import AgentExecutionError, run_agent
from factory.lib.config import load_config, resolve_reviewers
from factory.lib.gate_runner import run_gate
from factory.lib.git_ops import (
    GitHubClient,
    commits_ahead,
    diff_stat,
    ensure_worktree,
    is_worktree_clean,
    push_branch,
    reset_worktree_clean,
    sync_with_base,
)
from factory.lib.schemas import (
    SCHEMAS,
    render_approve_md,
    render_build_md,
    render_intake_md,
    render_pr_body,
    render_review_md,
    render_spec_md,
)
from factory.lib.state import (
    TERMINAL_OR_READY_STAGES,
    advance_stage,
    create_or_resume_job,
    list_jobs,
    load_job,
    record_usage,
    update_job,
)


def _read_text(path: str, default: str = "") -> str:
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _read_json(path: str, default: dict | None = None) -> dict:
    if not os.path.isfile(path):
        return {} if default is None else default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _escalate_to_human(
    jobs_dir: str,
    job_id: str,
    issue_number: int,
    gh: GitHubClient,
    config: dict,
    reason: str,
) -> dict:
    labels = config["labels"]
    job = advance_stage(jobs_dir, job_id, "needs-human", last_error=reason)
    try:
        gh.edit_labels(issue_number, add=[labels["needs_human"]], remove=[labels["queue"]])
        pr_note = f"\nPR: {job['pr_url']}" if job.get("pr_url") else ""
        gh.comment_issue(
            issue_number,
            f"### Factory Escalation (`{job_id}`)\n\n{reason}{pr_note}\n\n"
            f"Reply on this issue and re-add the `{labels['queue']}` label (removing `{labels['needs_human']}`) to resume.",
        )
    except Exception as exc:
        print(f"[{job_id}] warning: failed to update GitHub on escalation: {exc}", file=sys.stderr)
    return job


def _collect_actionable_review_findings(round_dir: str) -> str:
    lines: list[str] = []
    if not os.path.isdir(round_dir):
        return ""
    for fname in sorted(os.listdir(round_dir)):
        if not (fname.startswith("review-") and fname.endswith(".json")):
            continue
        rev_name = fname[len("review-") : -len(".json")]
        data = _read_json(os.path.join(round_dir, fname))
        if data.get("verdict") != "CHANGES":
            continue
        for f in data.get("findings", []):
            if f.get("severity") in {"must_fix", "should_fix"}:
                lines.append(
                    f"- [{rev_name} / {f['severity'].upper()}] `{f['file']}:{f['line']}`: {f['issue']} (Fix: {f['fix']})"
                )
    return "\n".join(lines)


def process_job(
    repo_root: str,
    issue_number: int,
    config: dict | None = None,
    gh: GitHubClient | None = None,
) -> dict:
    """Drive one GitHub issue from its current/resumed stage until paused or completed."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    cfg = config or load_config(factory_dir)
    gh_client = gh or GitHubClient(repo_root)
    labels = cfg["labels"]
    base_branch = cfg["base_branch"]
    timeout_sec = int(cfg["agent_timeout_sec"])

    issue_data, issue_md = gh_client.fetch_issue(issue_number)
    job, action = create_or_resume_job(jobs_dir, issue_data, max_rounds=cfg["max_rounds"])
    job_id = job["id"]
    jdir = os.path.join(jobs_dir, job_id)
    _write_text(os.path.join(jdir, "issue.md"), issue_md)

    if action == "skip":
        gh_client.edit_labels(issue_number, remove=[labels["queue"]])
        return job

    worktree, branch = ensure_worktree(repo_root, job_id, base_branch)

    try:
        while True:
            job = load_job(jobs_dir, job_id)
            stage = job["stage"]
            if stage in TERMINAL_OR_READY_STAGES or stage in {"needs-info", "needs-human"}:
                return job

            print(f"[{job_id}] stage={stage} round={job['round']}", flush=True)

            if stage == "intake":
                prompt = f"JOB_ID: {job_id}\nBASE_BRANCH: {base_branch}\n\n{issue_md}"
                intake_out, usage, dur = run_agent(
                    factory_dir,
                    jdir,
                    worktree,
                    "intake",
                    "intake",
                    cfg["models"]["intake"],
                    prompt,
                    SCHEMAS["intake"],
                    timeout_sec,
                    sandbox=True,
                    log_name="intake",
                )
                record_usage(jobs_dir, job_id, usage, dur)
                _write_json(os.path.join(jdir, "intake.json"), intake_out)
                _write_text(os.path.join(jdir, "intake.md"), render_intake_md(intake_out))

                decision = intake_out["decision"]
                if decision == "NEEDS_INFO":
                    q_block = "\n".join(f"{i}. {q}" for i, q in enumerate(intake_out["questions"], start=1))
                    gh_client.comment_issue(
                        issue_number,
                        f"### Clarification Needed (`{job_id}`)\n\n{q_block}\n\n"
                        f"Reply here and swap label `{labels['needs_info']}` back to `{labels['queue']}` to continue.",
                    )
                    gh_client.edit_labels(issue_number, add=[labels["needs_info"]], remove=[labels["queue"]])
                    return advance_stage(jobs_dir, job_id, "needs-info")

                if decision == "REJECT":
                    gh_client.comment_issue(
                        issue_number,
                        f"### Closing Issue (`{job_id}`)\n\n{intake_out['reasoning']}",
                    )
                    gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                    gh_client.close_issue(issue_number)
                    return advance_stage(jobs_dir, job_id, "rejected")

                update_job(jobs_dir, job_id, risk=intake_out["risk"], surfaces=intake_out["surfaces"])
                advance_stage(jobs_dir, job_id, "spec")
                continue

            if stage == "spec":
                intake_md = _read_text(os.path.join(jdir, "intake.md"))
                prompt = (
                    f"JOB_ID: {job_id}\nBASE_BRANCH: {base_branch}\n\n"
                    f"## Intake\n{intake_md}\n\n## Issue\n{issue_md}"
                )
                spec_out, usage, dur = run_agent(
                    factory_dir,
                    jdir,
                    worktree,
                    "spec",
                    "spec-writer",
                    cfg["models"]["spec_writer"],
                    prompt,
                    SCHEMAS["spec"],
                    timeout_sec,
                    sandbox=True,
                    log_name="spec",
                )
                record_usage(jobs_dir, job_id, usage, dur)
                _write_json(os.path.join(jdir, "spec.json"), spec_out)
                _write_json(os.path.join(jdir, "plan.json"), {"slices": spec_out["slices"]})
                _write_text(os.path.join(jdir, "spec.md"), render_spec_md(spec_out))
                update_job(
                    jobs_dir,
                    job_id,
                    total_slices=len(spec_out["slices"]),
                    completed_slices=0,
                )
                advance_stage(jobs_dir, job_id, "build")
                continue

            if stage == "build":
                round_num = job["round"]
                round_dir = os.path.join(jdir, f"round-{round_num}")
                os.makedirs(round_dir, exist_ok=True)
                spec_md = _read_text(os.path.join(jdir, "spec.md"))
                plan_data = _read_json(os.path.join(jdir, "plan.json"), {"slices": []})
                slices = plan_data.get("slices", [])
                total_slices = len(slices)

                if job["completed_slices"] < total_slices:
                    # Execute remaining vertical TDD slices one by one
                    for idx in range(job["completed_slices"], total_slices):
                        slice_num = idx + 1
                        sl = slices[idx]
                        retry_feedback = ""
                        slice_done = False

                        for attempt in range(cfg["max_slice_retries"] + 1):
                            prompt_parts = [
                                f"JOB_ID: {job_id}",
                                f"ROUND: {round_num}",
                                f"SLICE_INDEX: {slice_num}/{total_slices}",
                                f"BRANCH: {branch}",
                                f"BASE_BRANCH: {base_branch}",
                                "",
                                "## Assigned Vertical Slice",
                                json.dumps(sl, indent=2),
                                "",
                                "## Full Specification",
                                spec_md,
                            ]
                            if retry_feedback:
                                prompt_parts.extend(["", "## Previous Attempt Failure (Must Fix)", retry_feedback])

                            build_out, usage, dur = run_agent(
                                factory_dir,
                                jdir,
                                worktree,
                                "build",
                                "builder",
                                cfg["models"]["builder"],
                                "\n".join(prompt_parts),
                                SCHEMAS["build"],
                                timeout_sec,
                                sandbox=False,
                                log_name=f"r{round_num}-slice-{slice_num}-a{attempt + 1}",
                            )
                            record_usage(jobs_dir, job_id, usage, dur)

                            if build_out["verdict"] == "BLOCKED":
                                return _escalate_to_human(
                                    jobs_dir,
                                    job_id,
                                    issue_number,
                                    gh_client,
                                    cfg,
                                    f"Builder blocked on slice {slice_num}/{total_slices}: {build_out['blocked_reason']}",
                                )

                            if not is_worktree_clean(worktree):
                                retry_feedback = (
                                    "Uncommitted changes remain in the worktree. Stage and `git commit` all intended changes "
                                    "or remove temporary files so `git status --porcelain` is empty."
                                )
                                continue

                            if commits_ahead(worktree, base_branch) == 0:
                                retry_feedback = "No commits exist on branch ahead of base branch. Commit your implementation and tests."
                                continue

                            passed, slice_checks_md = run_gate(
                                repo_root,
                                job_id,
                                worktree,
                                round_num,
                                cfg,
                                tag=f"slice-{slice_num}",
                            )
                            if passed:
                                _write_text(
                                    os.path.join(round_dir, f"slice-{slice_num}-build.md"),
                                    render_build_md(build_out, heading=f"Slice {slice_num}: {sl['title']}"),
                                )
                                _write_json(os.path.join(round_dir, "build.json"), build_out)
                                _write_text(os.path.join(round_dir, "build.md"), render_build_md(build_out))
                                update_job(jobs_dir, job_id, completed_slices=slice_num)
                                slice_done = True
                                break

                            retry_feedback = f"Deterministic gate failed after slice {slice_num}:\n\n{slice_checks_md}"

                        if not slice_done:
                            return _escalate_to_human(
                                jobs_dir,
                                job_id,
                                issue_number,
                                gh_client,
                                cfg,
                                f"Slice {slice_num}/{total_slices} (`{sl['title']}`) failed deterministic gate after "
                                f"{cfg['max_slice_retries'] + 1} attempts.\n\n{retry_feedback[:1500]}",
                            )
                else:
                    # Review-fix or gate-fix round (all slices already implemented)
                    prev_round_dir = os.path.join(jdir, f"round-{max(1, round_num - 1)}")
                    findings_text = _collect_actionable_review_findings(prev_round_dir)
                    prev_checks = _read_text(os.path.join(prev_round_dir, "checks.md"))
                    prompt = "\n".join(
                        [
                            f"JOB_ID: {job_id}",
                            f"FIX_ROUND: {round_num}",
                            f"BRANCH: {branch}",
                            f"BASE_BRANCH: {base_branch}",
                            "",
                            "## Actionable Review Findings to Fix",
                            findings_text or "(Fix deterministic gate failures below.)",
                            "",
                            "## Previous Gate Output",
                            prev_checks,
                            "",
                            "## Specification",
                            spec_md,
                        ]
                    )
                    build_out, usage, dur = run_agent(
                        factory_dir,
                        jdir,
                        worktree,
                        "build",
                        "builder",
                        cfg["models"]["builder"],
                        prompt,
                        SCHEMAS["build"],
                        timeout_sec,
                        sandbox=False,
                        log_name=f"r{round_num}-fix-build",
                    )
                    record_usage(jobs_dir, job_id, usage, dur)
                    if build_out["verdict"] == "BLOCKED":
                        return _escalate_to_human(
                            jobs_dir,
                            job_id,
                            issue_number,
                            gh_client,
                            cfg,
                            f"Builder blocked in round {round_num}: {build_out['blocked_reason']}",
                        )
                    _write_json(os.path.join(round_dir, "build.json"), build_out)
                    _write_text(os.path.join(round_dir, "build.md"), render_build_md(build_out))

                advance_stage(jobs_dir, job_id, "gate")
                continue

            if stage == "gate":
                round_num = job["round"]
                spec_md = _read_text(os.path.join(jdir, "spec.md"))
                passed, checks_md = False, ""

                for attempt in range(cfg["max_slice_retries"] + 1):
                    if is_worktree_clean(worktree) and commits_ahead(worktree, base_branch) > 0:
                        passed, checks_md = run_gate(repo_root, job_id, worktree, round_num, cfg)
                        if passed:
                            break
                    else:
                        checks_md = (
                            "Worktree has uncommitted changes or zero commits ahead of base branch. "
                            "Commit all changes cleanly."
                        )

                    if attempt == cfg["max_slice_retries"]:
                        break

                    fix_prompt = (
                        f"JOB_ID: {job_id}\nROUND: {round_num}\nBRANCH: {branch}\nBASE_BRANCH: {base_branch}\n\n"
                        f"## Gate Failure to Fix\n{checks_md}\n\n## Specification\n{spec_md}"
                    )
                    fix_out, usage, dur = run_agent(
                        factory_dir,
                        jdir,
                        worktree,
                        "build",
                        "builder",
                        cfg["models"]["builder"],
                        fix_prompt,
                        SCHEMAS["build"],
                        timeout_sec,
                        sandbox=False,
                        log_name=f"r{round_num}-gate-fix-{attempt + 1}",
                    )
                    record_usage(jobs_dir, job_id, usage, dur)
                    if fix_out["verdict"] == "BLOCKED":
                        return _escalate_to_human(
                            jobs_dir,
                            job_id,
                            issue_number,
                            gh_client,
                            cfg,
                            f"Builder blocked while fixing gate in round {round_num}: {fix_out['blocked_reason']}",
                        )

                if not passed:
                    if job["round"] < job["round_limit"]:
                        advance_stage(jobs_dir, job_id, "build")
                        continue
                    return _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Deterministic gate failed after {job['round']} round(s):\n\n{checks_md[:1500]}",
                    )

                push_branch(worktree, branch)
                intake_data = _read_json(os.path.join(jdir, "intake.json"))
                spec_data = _read_json(os.path.join(jdir, "spec.json"))
                job = load_job(jobs_dir, job_id)
                pr_title = job["issue_title"] or f"Fix #{issue_number}"
                pr_body = render_pr_body(job, intake_data, spec_data)
                pr_num, pr_url = gh_client.ensure_draft_pr(
                    worktree,
                    branch,
                    base_branch,
                    pr_title,
                    pr_body,
                    pr_number=job.get("pr_number"),
                )
                update_job(jobs_dir, job_id, pr_number=pr_num, pr_url=pr_url)
                advance_stage(jobs_dir, job_id, "review")
                continue

            if stage == "review":
                round_num = job["round"]
                round_dir = os.path.join(jdir, f"round-{round_num}")
                os.makedirs(round_dir, exist_ok=True)
                reviewers = resolve_reviewers(cfg, job["risk"], job["surfaces"])
                spec_md = _read_text(os.path.join(jdir, "spec.md"))
                build_md = _read_text(os.path.join(round_dir, "build.md"))
                checks_md = _read_text(os.path.join(round_dir, "checks.md"))
                stat_text = diff_stat(worktree, base_branch)

                review_prompt = "\n".join(
                    [
                        f"JOB_ID: {job_id}",
                        f"ROUND: {round_num}",
                        f"BASE_BRANCH: {base_branch}",
                        "",
                        "## Diff Stat (`origin/<base_branch>..HEAD`)",
                        stat_text or "(empty)",
                        "",
                        "## Specification",
                        spec_md,
                        "",
                        "## Build Summary",
                        build_md,
                        "",
                        "## Gate Status",
                        checks_md,
                    ]
                )

                def _run_single_reviewer(item: tuple[str, str]) -> tuple[str, dict]:
                    rev_name, rev_model = item
                    agent_file = f"reviewer-{rev_name}"
                    out, u, d = run_agent(
                        factory_dir,
                        jdir,
                        worktree,
                        "review",
                        agent_file,
                        rev_model,
                        review_prompt,
                        SCHEMAS["review"],
                        timeout_sec,
                        sandbox=True,
                        log_name=f"r{round_num}-review-{rev_name}",
                    )
                    record_usage(jobs_dir, job_id, u, d)
                    _write_json(os.path.join(round_dir, f"review-{rev_name}.json"), out)
                    _write_text(
                        os.path.join(round_dir, f"review-{rev_name}.md"),
                        render_review_md(rev_name, out),
                    )
                    return rev_name, out

                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
                    results = dict(pool.map(_run_single_reviewer, reviewers.items()))

                if not is_worktree_clean(worktree):
                    reset_worktree_clean(worktree)

                has_changes = any(r["verdict"] == "CHANGES" for r in results.values())
                if has_changes:
                    if job["round"] < job["round_limit"]:
                        advance_stage(jobs_dir, job_id, "build")
                        continue
                    unresolved = _collect_actionable_review_findings(round_dir)
                    return _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Reviewers still requested changes after {job['round']} round(s):\n\n{unresolved}",
                    )

                advance_stage(jobs_dir, job_id, "approve")
                continue

            if stage == "approve":
                round_num = job["round"]
                round_dir = os.path.join(jdir, f"round-{round_num}")
                intake_data = _read_json(os.path.join(jdir, "intake.json"))
                spec_data = _read_json(os.path.join(jdir, "spec.json"))
                intake_md = _read_text(os.path.join(jdir, "intake.md"))
                spec_md = _read_text(os.path.join(jdir, "spec.md"))
                build_md = _read_text(os.path.join(round_dir, "build.md"))
                checks_md = _read_text(os.path.join(round_dir, "checks.md"))

                reviews_dict: dict[str, dict] = {}
                review_blocks: list[str] = []
                for fname in sorted(os.listdir(round_dir)):
                    if fname.startswith("review-") and fname.endswith(".json"):
                        rname = fname[len("review-") : -len(".json")]
                        reviews_dict[rname] = _read_json(os.path.join(round_dir, fname))
                    elif fname.startswith("review-") and fname.endswith(".md"):
                        review_blocks.append(_read_text(os.path.join(round_dir, fname)))

                approve_prompt = "\n".join(
                    [
                        f"JOB_ID: {job_id}",
                        f"ROUND: {round_num}",
                        f"BASE_BRANCH: {base_branch}",
                        "",
                        "## Diff Stat",
                        diff_stat(worktree, base_branch),
                        "",
                        "## Intake",
                        intake_md,
                        "",
                        "## Specification",
                        spec_md,
                        "",
                        "## Build Summary",
                        build_md,
                        "",
                        "## Gate Status",
                        checks_md,
                        "",
                        "## Reviews",
                        "\n\n".join(review_blocks),
                    ]
                )
                approve_out, usage, dur = run_agent(
                    factory_dir,
                    jdir,
                    worktree,
                    "approve",
                    "approver",
                    cfg["models"]["approver"],
                    approve_prompt,
                    SCHEMAS["approve"],
                    timeout_sec,
                    sandbox=True,
                    log_name=f"r{round_num}-approve",
                )
                record_usage(jobs_dir, job_id, usage, dur)
                _write_json(os.path.join(round_dir, "approve.json"), approve_out)
                _write_text(os.path.join(round_dir, "approve.md"), render_approve_md(approve_out))

                if approve_out["decision"] == "ESCALATE":
                    return _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Approver requested human review:\n\n**Question:** {approve_out['question']}\n\n{approve_out['reasoning']}",
                    )

                job = load_job(jobs_dir, job_id)
                pr_title = job["issue_title"] or f"Fix #{issue_number}"
                final_body = render_pr_body(job, intake_data, spec_data, reviews_dict, approve_out)
                if job.get("pr_number"):
                    gh_client.update_pr(int(job["pr_number"]), pr_title, final_body)

                auto_merge_mode = cfg["auto_merge"]
                should_auto_merge = auto_merge_mode == "all" or (
                    auto_merge_mode == "low" and job.get("risk") == "low"
                )
                if should_auto_merge:
                    advance_stage(jobs_dir, job_id, "merging")
                    continue

                if job.get("pr_number"):
                    gh_client.mark_pr_ready(int(job["pr_number"]))
                gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                gh_client.comment_issue(
                    issue_number,
                    f"### PR Ready for Merge (`{job_id}`)\n\n"
                    f"Pull Request {job.get('pr_url') or ('#' + str(job.get('pr_number')))} passed all deterministic checks "
                    f"and reviews (`risk={job.get('risk')}`). Marked ready for 1-click merge.",
                )
                return advance_stage(jobs_dir, job_id, "ready")

            if stage == "merging":
                round_num = job["round"]
                ok, head_changed, detail = sync_with_base(worktree, base_branch)
                if not ok:
                    return _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Merge conflict while syncing `{branch}` with `origin/{base_branch}`:\n\n```\n{detail[:1000]}\n```",
                    )

                if head_changed:
                    passed, sync_checks_md = run_gate(
                        repo_root,
                        job_id,
                        worktree,
                        round_num,
                        cfg,
                        tag="pre-merge",
                    )
                    if not passed:
                        return _escalate_to_human(
                            jobs_dir,
                            job_id,
                            issue_number,
                            gh_client,
                            cfg,
                            f"Post-sync gate failed after merging `origin/{base_branch}`:\n\n{sync_checks_md[:1200]}",
                        )
                    push_branch(worktree, branch)

                pr_num = int(job["pr_number"])
                gh_client.mark_pr_ready(pr_num)
                gh_client.merge_pr(pr_num, cfg["merge_method"])
                gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                return advance_stage(jobs_dir, job_id, "merged")

            raise RuntimeError(f"Unhandled stage {stage!r} for {job_id}")

    except (AgentExecutionError, subprocess.CalledProcessError, ValueError) as exc:
        cur = load_job(jobs_dir, job_id)
        if cur["stage"] not in TERMINAL_OR_READY_STAGES and cur["stage"] not in {"needs-info", "needs-human"}:
            return _escalate_to_human(
                jobs_dir,
                job_id,
                issue_number,
                gh_client,
                cfg,
                f"Pipeline error during `{cur['stage']}`: {exc}",
            )
        raise


def run_factory(repo_root: str, issue_number: int | None = None) -> list[dict]:
    """Process a single issue or drain all eligible queued issues concurrently."""
    repo_root = os.path.abspath(repo_root)
    cfg = load_config(os.path.join(repo_root, "factory"))
    gh = GitHubClient(repo_root)

    if issue_number is not None:
        return [process_job(repo_root, int(issue_number), config=cfg, gh=gh)]

    issues = gh.list_queue_issues(cfg)
    if not issues:
        return []

    max_workers = min(int(cfg["concurrency"]), len(issues))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(process_job, repo_root, int(item["number"]), cfg, gh)
            for item in issues
        ]
        return [fut.result() for fut in futures]


def format_status_table(jobs: list[dict]) -> str:
    if not jobs:
        return "No factory jobs recorded."
    headers = ("JOB", "STAGE", "ROUND", "SLICES", "RISK", "TOKENS", "PR")
    rows = [headers]
    for j in jobs:
        slices = f"{j.get('completed_slices', 0)}/{j.get('total_slices', 0)}"
        tokens = str((j.get("usage") or {}).get("total_tokens", 0))
        pr_str = j.get("pr_url") or (f"#{j['pr_number']}" if j.get("pr_number") else "-")
        rows.append(
            (
                str(j.get("id", "")),
                str(j.get("stage", "")),
                f"{j.get('round', 0)}/{j.get('round_limit', 0)}",
                slices,
                str(j.get("risk", "-")),
                tokens,
                pr_str,
            )
        )
    widths = [max(len(r[col]) for r in rows) for col in range(len(headers))]
    out_lines = []
    for idx, r in enumerate(rows):
        line = "  ".join(r[col].ljust(widths[col]) for col in range(len(headers))).rstrip()
        out_lines.append(line)
        if idx == 0:
            out_lines.append("  ".join("-" * w for w in widths))
    return "\n".join(out_lines)


def run_doctor(repo_root: str) -> tuple[bool, list[str]]:
    """Verify repository, CLI prerequisites, config, checks.sh, and .gitignore."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    messages: list[str] = []
    ok = True

    if sys.version_info < (3, 10):
        ok = False
        messages.append(f"FAIL: Python >= 3.10 required (found {sys.version.split()[0]})")
    else:
        messages.append(f"OK: Python {sys.version.split()[0]}")

    for binary, args in (("git", ["--version"]), ("gh", ["auth", "status"]), ("agy", ["--version"])):
        if not shutil.which(binary):
            ok = False
            messages.append(f"FAIL: `{binary}` not found on PATH")
            continue
        res = subprocess.run([binary, *args], cwd=repo_root, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            ok = False
            messages.append(f"FAIL: `{binary} {' '.join(args)}` exited {res.returncode}")
        else:
            messages.append(f"OK: `{binary} {' '.join(args)}`")

    rem = subprocess.run(["git", "-C", repo_root, "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    if rem.returncode != 0:
        ok = False
        messages.append("FAIL: git remote `origin` is not configured")
    else:
        messages.append(f"OK: git origin -> {rem.stdout.strip()}")

    try:
        cfg = load_config(factory_dir)
        messages.append(f"OK: factory/config.json valid (auto_merge={cfg['auto_merge']}, concurrency={cfg['concurrency']})")
    except Exception as exc:
        ok = False
        messages.append(f"FAIL: factory/config.json invalid: {exc}")

    checks_sh = os.path.join(factory_dir, "checks.sh")
    if not (os.path.isfile(checks_sh) and os.access(checks_sh, os.X_OK)):
        ok = False
        messages.append("FAIL: factory/checks.sh missing or not executable")
    else:
        messages.append("OK: factory/checks.sh present and executable")

    ig = subprocess.run(
        ["git", "-C", repo_root, "check-ignore", "factory/jobs/probe-job", ".worktrees/probe-job"],
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = {line.strip() for line in ig.stdout.splitlines() if line.strip()}
    if {"factory/jobs/probe-job", ".worktrees/probe-job"} <= ignored:
        messages.append("OK: .gitignore excludes factory/jobs/* and .worktrees/*")
    else:
        ok = False
        messages.append("FAIL: .gitignore must ignore `/factory/jobs/*` and `/.worktrees/`")

    return ok, messages
