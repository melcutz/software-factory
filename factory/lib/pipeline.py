"""End-to-end autonomous software factory pipeline orchestrator and doctor checks."""
from __future__ import annotations

import concurrent.futures
import graphlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

from factory.lib.agy_runner import AgentExecutionError, run_agent
from factory.lib.config import (
    estimate_usage_cost_usd,
    load_config,
    resolve_reviewers,
    split_model_and_effort,
)
from factory.lib.gate_runner import run_gate
from factory.lib.planner import plan_dependency_dag
from factory.lib.git_ops import (
    GitHubClient,
    abort_merge,
    commits_ahead,
    conflicted_files,
    delete_remote_branch,
    diff_stat,
    ensure_worktree,
    head_sha,
    is_worktree_clean,
    merge_in_progress,
    push_branch,
    repo_lock,
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
    enqueue_job,
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


def _ts() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(seconds: float) -> str:
    sec = max(0.0, float(seconds or 0.0))
    if sec < 60.0:
        return f"{sec:.1f}s"
    mins = int(sec // 60)
    rem = int(round(sec - mins * 60))
    if rem == 60:
        mins += 1
        rem = 0
    return f"{mins}m{rem:02d}s ({sec:.1f}s)"


def _fmt_model_tag(model_spec: str) -> str:
    base_model, effort = split_model_and_effort(model_spec)
    return f"model={base_model} effort={effort}"


def _record_and_format_step(
    jobs_dir: str,
    job_id: str,
    model_spec: str,
    usage: dict,
    duration_sec: float,
    config: dict,
) -> str:
    step_cost = estimate_usage_cost_usd(model_spec, usage, config)
    updated_job = record_usage(jobs_dir, job_id, usage, duration_sec, cost_usd=step_cost)
    u = usage or {}
    in_tok = int(u.get("input_tokens", 0) or 0)
    cache_tok = int(u.get("cache_read_tokens", 0) or 0)
    out_tok = int(u.get("output_tokens", 0) or 0)
    think_tok = int(u.get("thinking_tokens", 0) or 0)
    tot_tok = int(u.get("total_tokens", 0) or (in_tok + out_tok))
    parts = [
        _fmt_model_tag(model_spec),
        f"dur={_fmt_duration(duration_sec)}",
        f"tokens={tot_tok:,} (in={in_tok:,} cache={cache_tok:,} out={out_tok:,} think={think_tok:,})",
    ]
    if step_cost is not None:
        total_cost = float((updated_job.get("usage") or {}).get("cost_usd", 0.0) or 0.0)
        parts.append(f"cost=${step_cost:.4f} total_cost=${total_cost:.4f}")
    return " ".join(parts)


def _log(job_id: str, message: str) -> None:
    print(f"[{_ts()}] [{job_id}] {message}", flush=True)


def _set_activity(jobs_dir: str, job_id: str, activity: str | None) -> None:
    update_job(
        jobs_dir,
        job_id,
        activity=activity,
        pid=os.getpid() if activity is not None else None,
    )


def _is_pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _format_activity(job: dict) -> str:
    stage = str(job.get("stage", ""))
    if stage in TERMINAL_OR_READY_STAGES:
        return "-"
    activity = job.get("activity")
    pid = job.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and _is_pid_alive(pid):
        return str(activity or ("queued" if stage == "queued" else "running"))
    if stage in {"needs-human", "needs-info"}:
        return "-"
    if stage == "queued":
        if isinstance(activity, str) and activity.startswith("blocked"):
            return activity
        return "-"
    if activity:
        label = str(activity).split(":", 1)[-1]
        return f"stale ({label})"
    return "stale"


def _escalate_to_human(
    jobs_dir: str,
    job_id: str,
    issue_number: int,
    gh: GitHubClient,
    config: dict,
    reason: str,
    resume_stage: str | None = None,
) -> dict:
    labels = config["labels"]
    job = advance_stage(jobs_dir, job_id, "needs-human", last_error=reason)
    if resume_stage:
        job = update_job(jobs_dir, job_id, escalated_from=resume_stage)
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


def _aggregate_slice_builds(jdir: str, total_completed: int) -> tuple[dict, str]:
    """Aggregate completed slice JSON/Markdown outputs across rounds into one unified build summary."""
    summaries: list[str] = []
    changes: list[str] = []
    tests_added: list[str] = []
    notes_list: list[str] = []
    slice_mds: list[str] = []

    round_dirs = sorted(
        (d for d in os.listdir(jdir) if d.startswith("round-") and os.path.isdir(os.path.join(jdir, d))),
        key=lambda x: int(x.split("-", 1)[1]) if x.split("-", 1)[1].isdigit() else 0,
    )
    for s_num in range(1, total_completed + 1):
        # Find latest slice-{s_num}-build.json / .md across rounds
        for rdir in reversed(round_dirs):
            s_json_path = os.path.join(jdir, rdir, f"slice-{s_num}-build.json")
            s_md_path = os.path.join(jdir, rdir, f"slice-{s_num}-build.md")
            if os.path.isfile(s_json_path):
                s_data = _read_json(s_json_path)
                summaries.extend(s_data.get("summary") or [])
                changes.extend(s_data.get("changes") or [])
                tests_added.extend(s_data.get("tests_added") or [])
                if s_data.get("notes"):
                    notes_list.append(str(s_data["notes"]).strip())
                if os.path.isfile(s_md_path):
                    slice_mds.append(_read_text(s_md_path).strip())
                break

    agg = {
        "verdict": "DONE",
        "summary": summaries or ["Completed vertical slices"],
        "changes": changes,
        "tests_added": tests_added,
        "blocked_reason": "",
        "notes": "\n".join(notes_list),
    }
    return agg, "\n\n".join(slice_mds)


def _sync_pr_with_base(
    repo_root: str,
    factory_dir: str,
    jobs_dir: str,
    jdir: str,
    job_id: str,
    worktree: str,
    branch: str,
    base_branch: str,
    round_num: int,
    cfg: dict,
) -> tuple[bool, str]:
    """Sync branch with origin/<base_branch>, using builder to resolve conflicts before pre-merge gate."""
    ok, head_changed, detail = sync_with_base(worktree, base_branch, abort_on_conflict=False)
    if not ok:
        if not merge_in_progress(worktree):
            abort_merge(worktree)
            return (
                False,
                f"Merge conflict while syncing `{branch}` with `origin/{base_branch}`:\n\n```\n{detail[:1000]}\n```",
            )

        before_resolve = head_sha(worktree)
        conflicts = conflicted_files(worktree)
        spec_md = _read_text(os.path.join(jdir, "spec.md"))
        builder_model = cfg["models"]["builder"]
        timeout_sec = int(cfg["agent_timeout_sec"])
        conflict_list = "\n".join(f"- {path}" for path in conflicts) or "(see git status)"
        conflict_prompt = (
            f"JOB_ID: {job_id}\nROUND: {round_num}\nBRANCH: {branch}\nBASE_BRANCH: {base_branch}\n\n"
            f"A `git merge --no-edit origin/{base_branch}` is currently in progress in this worktree "
            f"and hit merge conflicts in:\n{conflict_list}\n\n"
            f"## Git Output\n```\n{detail[:1500]}\n```\n\n"
            "## Instructions\n"
            f"1. Inspect the conflicted files and resolve all conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) "
            f"preserving both `origin/{base_branch}`'s changes and this branch's intended behavior.\n"
            "2. Do NOT run `git merge --abort` or `git push`.\n"
            "3. Run targeted tests and fast linters on the affected files, stage the resolved files (`git add`), "
            "and complete the merge commit (`git commit --no-edit`), leaving `git status --porcelain` clean.\n"
            "4. Return `verdict: \"BLOCKED\"` with a concrete `blocked_reason` if the conflict cannot be safely resolved.\n"
        )
        if spec_md:
            conflict_prompt += f"\n## Specification\n{spec_md}"

        _set_activity(jobs_dir, job_id, "running:conflict-resolve")
        _log(job_id, f"merge-conflict resolve start files={len(conflicts)} ({_fmt_model_tag(builder_model)})")
        try:
            resolve_out, usage, dur = run_agent(
                factory_dir,
                jdir,
                worktree,
                "build",
                "builder",
                builder_model,
                conflict_prompt,
                SCHEMAS["build"],
                timeout_sec,
                sandbox=False,
                log_name=f"r{round_num}-merge-conflict",
            )
        except Exception as exc:
            abort_merge(worktree)
            return (
                False,
                f"Merge conflict resolution failed while syncing `{branch}` with `origin/{base_branch}`: {exc}",
            )

        metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
        _log(job_id, f"merge-conflict resolve verdict={resolve_out['verdict']} ({metrics_str})")

        if resolve_out["verdict"] == "BLOCKED":
            abort_merge(worktree)
            return (
                False,
                f"Merge conflict while syncing `{branch}` with `origin/{base_branch}` "
                f"(builder blocked: {resolve_out['blocked_reason']}):\n\n```\n{detail[:800]}\n```",
            )

        if (
            merge_in_progress(worktree)
            or conflicted_files(worktree)
            or not is_worktree_clean(worktree)
            or head_sha(worktree) == before_resolve
        ):
            abort_merge(worktree)
            return (
                False,
                f"Merge conflict resolution for `{branch}` with `origin/{base_branch}` "
                "did not complete a clean merge commit.",
            )
        head_changed = True

    if head_changed:
        _set_activity(jobs_dir, job_id, "running:pre-merge-gate")
        _log(job_id, "gate pre-merge scope=full running checks.sh")
        t_gate = time.monotonic()
        passed, sync_checks_md = run_gate(
            repo_root,
            job_id,
            worktree,
            round_num,
            cfg,
            tag="pre-merge",
            scope="full",
        )
        gate_dur = time.monotonic() - t_gate
        _log(
            job_id,
            f"gate pre-merge scope=full verdict={'PASS' if passed else 'FAIL'} (dur={_fmt_duration(gate_dur)})",
        )
        if not passed:
            return (
                False,
                f"Post-sync gate failed after merging `origin/{base_branch}`:\n\n{sync_checks_md[:1200]}",
            )
        push_branch(worktree, branch)

    return True, ""


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
        try:
            gh_client.edit_labels(issue_number, remove=[labels["queue"]])
        except Exception as exc:
            print(f"[{job_id}] warning: failed to remove queue label on skipped job: {exc}", file=sys.stderr)
        return job

    try:
        worktree, branch = ensure_worktree(repo_root, job_id, base_branch, branch=job.get("branch"))

        while True:
            job = load_job(jobs_dir, job_id)
            branch = job.get("branch") or f"factory/{job_id}"
            stage = job["stage"]
            if stage in TERMINAL_OR_READY_STAGES or stage in {"needs-info", "needs-human"}:
                return job

            _log(job_id, f"stage={stage} round={job['round']}")

            if stage == "intake":
                intake_model = cfg["models"]["intake"]
                _set_activity(jobs_dir, job_id, "running:intake")
                _log(job_id, f"intake start ({_fmt_model_tag(intake_model)})")
                prompt = f"JOB_ID: {job_id}\nBASE_BRANCH: {base_branch}\n\n{issue_md}"
                intake_out, usage, dur = run_agent(
                    factory_dir,
                    jdir,
                    worktree,
                    "intake",
                    "intake",
                    intake_model,
                    prompt,
                    SCHEMAS["intake"],
                    timeout_sec,
                    sandbox=True,
                    log_name="intake",
                )
                metrics_str = _record_and_format_step(jobs_dir, job_id, intake_model, usage, dur, cfg)
                _log(job_id, f"intake done decision={intake_out['decision']} ({metrics_str})")
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
                spec_model = cfg["models"]["spec_writer"]
                _set_activity(jobs_dir, job_id, "running:spec")
                _log(job_id, f"spec start ({_fmt_model_tag(spec_model)})")
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
                    spec_model,
                    prompt,
                    SCHEMAS["spec"],
                    timeout_sec,
                    sandbox=True,
                    log_name="spec",
                )
                metrics_str = _record_and_format_step(jobs_dir, job_id, spec_model, usage, dur, cfg)
                _log(job_id, f"spec done slices={len(spec_out['slices'])} ({metrics_str})")
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
                prior_escalation = job.get("last_error") or ""
                builder_model = cfg["models"]["builder"]

                if job["completed_slices"] < total_slices:
                    for idx in range(job["completed_slices"], total_slices):
                        slice_num = idx + 1
                        sl = slices[idx]
                        retry_feedback = ""
                        slice_done = False
                        _, prior_slices_md = _aggregate_slice_builds(jdir, idx)

                        for attempt in range(cfg["max_slice_retries"] + 1):
                            if attempt == 0 and not is_worktree_clean(worktree):
                                _log(
                                    job_id,
                                    f"build slice={slice_num}/{total_slices} cleaning uncommitted leftovers from prior interrupted run",
                                )
                                reset_worktree_clean(worktree)
                            _set_activity(jobs_dir, job_id, f"running:slice-{slice_num}")
                            _log(
                                job_id,
                                f"build slice={slice_num}/{total_slices} attempt={attempt + 1} start ({_fmt_model_tag(builder_model)})",
                            )
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
                            if prior_slices_md:
                                prompt_parts.extend(["", "## Previously Completed Slices", prior_slices_md])
                            if prior_escalation:
                                prompt_parts.extend(
                                    ["", "## Prior Escalation Context", prior_escalation, "", "## Latest Issue & Comments", issue_md]
                                )
                            if retry_feedback:
                                prompt_parts.extend(["", "## Previous Attempt Failure (Must Fix)", retry_feedback])

                            build_out, usage, dur = run_agent(
                                factory_dir,
                                jdir,
                                worktree,
                                "build",
                                "builder",
                                builder_model,
                                "\n".join(prompt_parts),
                                SCHEMAS["build"],
                                timeout_sec,
                                sandbox=False,
                                log_name=f"r{round_num}-slice-{slice_num}-a{attempt + 1}",
                            )
                            metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
                            _log(
                                job_id,
                                f"build slice={slice_num}/{total_slices} attempt={attempt + 1} verdict={build_out['verdict']} ({metrics_str})",
                            )

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

                            gate_scope = "slice" if slice_num < total_slices else "full"
                            _set_activity(jobs_dir, job_id, f"running:slice-{slice_num}-gate")
                            _log(
                                job_id,
                                f"gate slice={slice_num}/{total_slices} scope={gate_scope} running checks.sh",
                            )
                            t_gate = time.monotonic()
                            passed, slice_checks_md = run_gate(
                                repo_root,
                                job_id,
                                worktree,
                                round_num,
                                cfg,
                                tag=f"slice-{slice_num}",
                                scope=gate_scope,
                            )
                            gate_dur = time.monotonic() - t_gate
                            _log(
                                job_id,
                                f"gate slice={slice_num}/{total_slices} scope={gate_scope} verdict={'PASS' if passed else 'FAIL'} (dur={_fmt_duration(gate_dur)})",
                            )
                            if passed:
                                _write_json(os.path.join(round_dir, f"slice-{slice_num}-build.json"), build_out)
                                _write_text(
                                    os.path.join(round_dir, f"slice-{slice_num}-build.md"),
                                    render_build_md(build_out, heading=f"Slice {slice_num}: {sl['title']}"),
                                )
                                agg_build, _ = _aggregate_slice_builds(jdir, slice_num)
                                _write_json(os.path.join(round_dir, "build.json"), agg_build)
                                _write_text(os.path.join(round_dir, "build.md"), render_build_md(agg_build))
                                update_fields: dict = {"completed_slices": slice_num}
                                if gate_scope == "full":
                                    update_fields["verified_gate_sha"] = head_sha(worktree)
                                update_job(jobs_dir, job_id, **update_fields)
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
                    prev_round_dir = os.path.join(jdir, f"round-{max(1, round_num - 1)}")
                    findings_text = _collect_actionable_review_findings(prev_round_dir)
                    prev_checks = _read_text(os.path.join(prev_round_dir, "checks.md"))
                    prev_approve = _read_text(os.path.join(prev_round_dir, "approve.md"))
                    prompt_parts = [
                        f"JOB_ID: {job_id}",
                        f"FIX_ROUND: {round_num}",
                        f"BRANCH: {branch}",
                        f"BASE_BRANCH: {base_branch}",
                        "",
                        "## Actionable Review Findings to Fix",
                        findings_text or "(None from reviewers; check escalation/gate context below.)",
                    ]
                    if prior_escalation or prev_approve:
                        prompt_parts.extend(
                            [
                                "",
                                "## Escalation / Approver Context",
                                prior_escalation or prev_approve,
                                "",
                                "## Latest Issue & Human Comments",
                                issue_md,
                            ]
                        )
                    prompt_parts.extend(
                        [
                            "",
                            "## Previous Gate Output",
                            prev_checks,
                            "",
                            "## Specification",
                            spec_md,
                        ]
                    )
                    _set_activity(jobs_dir, job_id, f"running:fix-round-{round_num}")
                    _log(job_id, f"build fix-round={round_num} start ({_fmt_model_tag(builder_model)})")
                    build_out, usage, dur = run_agent(
                        factory_dir,
                        jdir,
                        worktree,
                        "build",
                        "builder",
                        builder_model,
                        "\n".join(prompt_parts),
                        SCHEMAS["build"],
                        timeout_sec,
                        sandbox=False,
                        log_name=f"r{round_num}-fix-build",
                    )
                    metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
                    _log(job_id, f"build fix-round={round_num} verdict={build_out['verdict']} ({metrics_str})")
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

                # Preserve last_error until approver completes so approver also sees escalation history if needed
                advance_stage(jobs_dir, job_id, "gate", preserve_last_error=bool(prior_escalation))
                continue

            if stage == "gate":
                round_num = job["round"]
                spec_md = _read_text(os.path.join(jdir, "spec.md"))
                passed, checks_md = False, ""
                builder_model = cfg["models"]["builder"]

                for attempt in range(cfg["max_slice_retries"] + 1):
                    if is_worktree_clean(worktree) and commits_ahead(worktree, base_branch) > 0:
                        cur_sha = head_sha(worktree)
                        existing_checks = _read_text(os.path.join(jdir, f"round-{round_num}", "checks.md"))
                        if (
                            attempt == 0
                            and job.get("verified_gate_sha") == cur_sha
                            and existing_checks.startswith("VERDICT: PASS")
                        ):
                            _log(
                                job_id,
                                f"gate already verified at {cur_sha[:7]} (skipping duplicate checks.sh)",
                            )
                            passed, checks_md = True, existing_checks
                            break
                        _set_activity(jobs_dir, job_id, f"running:gate-{attempt + 1}")
                        _log(job_id, f"gate scope=full running checks.sh (attempt={attempt + 1})")
                        t_gate = time.monotonic()
                        passed, checks_md = run_gate(repo_root, job_id, worktree, round_num, cfg, scope="full")
                        gate_dur = time.monotonic() - t_gate
                        _log(
                            job_id,
                            f"gate scope=full verdict={'PASS' if passed else 'FAIL'} (dur={_fmt_duration(gate_dur)})",
                        )
                        if passed:
                            update_job(jobs_dir, job_id, verified_gate_sha=head_sha(worktree))
                            break
                    else:
                        checks_md = (
                            "Worktree has uncommitted changes or zero commits ahead of base branch. "
                            "Commit all changes cleanly."
                        )
                        _write_text(os.path.join(jdir, f"round-{round_num}", "checks.md"), f"VERDICT: FAIL\n\n{checks_md}\n")

                    if attempt == cfg["max_slice_retries"]:
                        break

                    fix_prompt = (
                        f"JOB_ID: {job_id}\nROUND: {round_num}\nBRANCH: {branch}\nBASE_BRANCH: {base_branch}\n\n"
                        f"## Gate Failure to Fix\n{checks_md}\n\n## Specification\n{spec_md}"
                    )
                    _set_activity(jobs_dir, job_id, f"running:gate-fix-{attempt + 1}")
                    _log(job_id, f"gate-fix attempt={attempt + 1} start ({_fmt_model_tag(builder_model)})")
                    fix_out, usage, dur = run_agent(
                        factory_dir,
                        jdir,
                        worktree,
                        "build",
                        "builder",
                        builder_model,
                        fix_prompt,
                        SCHEMAS["build"],
                        timeout_sec,
                        sandbox=False,
                        log_name=f"r{round_num}-gate-fix-{attempt + 1}",
                    )
                    metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
                    _log(job_id, f"gate-fix attempt={attempt + 1} verdict={fix_out['verdict']} ({metrics_str})")
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
                        advance_stage(jobs_dir, job_id, "build", preserve_last_error=bool(job.get("last_error")))
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
                advance_stage(jobs_dir, job_id, "review", preserve_last_error=bool(job.get("last_error")))
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
                        "## Diff Stat (`origin/<base_branch>...HEAD`)",
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
                    _log(job_id, f"review={rev_name} start ({_fmt_model_tag(rev_model)})")
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
                        clean_worktree=False,
                        log_name=f"r{round_num}-review-{rev_name}",
                    )
                    metrics_str = _record_and_format_step(jobs_dir, job_id, rev_model, u, d, cfg)
                    _write_json(os.path.join(round_dir, f"review-{rev_name}.json"), out)
                    _write_text(
                        os.path.join(round_dir, f"review-{rev_name}.md"),
                        render_review_md(rev_name, out),
                    )
                    _log(job_id, f"review={rev_name} verdict={out['verdict']} ({metrics_str})")
                    return rev_name, out

                _set_activity(jobs_dir, job_id, "running:review")
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
                    results = dict(pool.map(_run_single_reviewer, reviewers.items()))

                if not is_worktree_clean(worktree):
                    reset_worktree_clean(worktree)

                has_changes = any(r["verdict"] == "CHANGES" for r in results.values())
                if has_changes:
                    if job["round"] < job["round_limit"]:
                        advance_stage(jobs_dir, job_id, "build", preserve_last_error=bool(job.get("last_error")))
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

                advance_stage(jobs_dir, job_id, "approve", preserve_last_error=bool(job.get("last_error")))
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

                approve_parts = [
                    f"JOB_ID: {job_id}",
                    f"ROUND: {round_num}",
                    f"BASE_BRANCH: {base_branch}",
                    "",
                    "## Diff Stat (`origin/<base_branch>...HEAD`)",
                    diff_stat(worktree, base_branch),
                    "",
                    "## Issue & Human Comments",
                    issue_md,
                ]
                if job.get("last_error"):
                    approve_parts.extend(["", "## Prior Escalation History", job["last_error"]])
                approve_parts.extend(
                    [
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
                approver_model = cfg["models"]["approver"]
                _set_activity(jobs_dir, job_id, "running:approve")
                _log(job_id, f"approve start ({_fmt_model_tag(approver_model)})")
                approve_out, usage, dur = run_agent(
                    factory_dir,
                    jdir,
                    worktree,
                    "approve",
                    "approver",
                    approver_model,
                    "\n".join(approve_parts),
                    SCHEMAS["approve"],
                    timeout_sec,
                    sandbox=True,
                    log_name=f"r{round_num}-approve",
                )
                metrics_str = _record_and_format_step(jobs_dir, job_id, approver_model, usage, dur, cfg)
                _log(job_id, f"approve decision={approve_out['decision']} ({metrics_str})")
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
                ready_job = advance_stage(jobs_dir, job_id, "ready")
                try:
                    gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                    gh_client.comment_issue(
                        issue_number,
                        f"### PR Ready for Merge (`{job_id}`)\n\n"
                        f"Pull Request {job.get('pr_url') or ('#' + str(job.get('pr_number')))} passed all deterministic checks "
                        f"and reviews (`risk={job.get('risk')}`). Marked ready for 1-click merge.",
                    )
                except Exception as exc:
                    print(f"[{job_id}] warning: post-ready issue update failed: {exc}", file=sys.stderr)
                return ready_job

            if stage == "merging":
                round_num = job["round"]
                pr_num = int(job["pr_number"])
                _set_activity(jobs_dir, job_id, "waiting:repo_lock")
                with repo_lock(repo_root):
                    _set_activity(jobs_dir, job_id, "running:merging")
                    already_merged_sha = gh_client.get_pr_merge_commit(pr_num)
                    if not already_merged_sha:
                        sync_ok, sync_err = _sync_pr_with_base(
                            repo_root,
                            factory_dir,
                            jobs_dir,
                            jdir,
                            job_id,
                            worktree,
                            branch,
                            base_branch,
                            round_num,
                            cfg,
                        )
                        if not sync_ok:
                            return _escalate_to_human(
                                jobs_dir,
                                job_id,
                                issue_number,
                                gh_client,
                                cfg,
                                sync_err,
                            )

                        gh_client.mark_pr_ready(pr_num)
                        gh_client.merge_pr(pr_num, cfg["merge_method"])
                    merged_job = advance_stage(jobs_dir, job_id, "merged")
                    delete_remote_branch(repo_root, branch)

                try:
                    gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                except Exception as exc:
                    print(f"[{job_id}] warning: post-merge label cleanup failed: {exc}", file=sys.stderr)
                return merged_job

            raise RuntimeError(f"Unhandled stage {stage!r} for {job_id}")

    except (AgentExecutionError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
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
    finally:
        _set_activity(jobs_dir, job_id, None)


def _plan_queue_batch(
    repo_root: str,
    factory_dir: str,
    jobs_dir: str,
    cfg: dict,
    issues: list[dict],
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """Build `(order_deps, hard_deps)` DAGs for a batch of issues using explicit refs + optional LLM planner."""
    planner_agent_path = os.path.join(factory_dir, "agents", "planner.md")

    def _run_planner(batch: list[dict]) -> dict:
        if not os.path.isfile(planner_agent_path):
            return {}
        models = cfg.get("models") or {}
        planner_model = models.get("planner") or models.get("intake", "")
        if not planner_model:
            return {}
        timeout_sec = int(cfg.get("agent_timeout_sec", 900))
        lines = ["Plan execution dependencies for the following queued GitHub issues:", ""]
        for item in batch:
            lines.append(f"## Issue #{int(item['number'])}: {item.get('title') or ''}")
            body = (item.get("body") or "").strip()
            if body:
                lines.append(body[:2000])
            lines.append("")
        plan_dir = os.path.join(jobs_dir, "_planner")
        os.makedirs(plan_dir, exist_ok=True)
        try:
            out, _, _ = run_agent(
                factory_dir,
                plan_dir,
                repo_root,
                "plan",
                "planner",
                planner_model,
                "\n".join(lines),
                SCHEMAS["plan"],
                timeout_sec,
                sandbox=True,
                clean_worktree=False,
                log_name="queue-plan",
            )
            return out
        except Exception as exc:
            print(f"[factory] warning: queue planner failed, using explicit dependencies only: {exc}", file=sys.stderr)
            return {}

    order_deps, hard_deps, _ = plan_dependency_dag(
        issues,
        planner_fn=_run_planner if len(issues) > 1 else None,
    )
    return order_deps, hard_deps


def run_factory(
    repo_root: str,
    issue_number: int | None = None,
    issue_numbers: list[int] | None = None,
) -> list[dict]:
    """Process specific issue(s) or drain all eligible queued issues using a dependency DAG."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    pid_path = os.path.join(jobs_dir, ".factory.pid")
    cfg = load_config(factory_dir)
    gh = GitHubClient(repo_root)

    explicit_nums: list[int] = []
    if issue_number is not None:
        explicit_nums = [int(issue_number)]
    elif issue_numbers:
        explicit_nums = [int(n) for n in issue_numbers]

    _write_text(pid_path, f"{os.getpid()}\n")
    try:
        if len(explicit_nums) == 1:
            return [process_job(repo_root, explicit_nums[0], config=cfg, gh=gh)]

        def _safe_worker(num: int) -> dict | None:
            try:
                return process_job(repo_root, num, config=cfg, gh=gh)
            except Exception as exc:
                print(f"[issue-{num}] error before job state could escalate: {exc}", file=sys.stderr)
                return None

        max_workers = max(1, int(cfg["concurrency"]))
        results: list[dict] = []
        seen_issues: set[int] = set()
        failed_parents: dict[int, str] = {}

        def _next_batch() -> list[dict]:
            if explicit_nums:
                if seen_issues:
                    return []
                return [gh.fetch_issue(num)[0] for num in explicit_nums]
            try:
                queued = gh.list_queue_issues(cfg)
            except Exception as exc:
                if not seen_issues:
                    raise
                print(f"[factory] warning: queue refresh failed: {exc}", file=sys.stderr)
                return []
            return [item for item in queued if int(item["number"]) not in seen_issues]

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            while True:
                batch = _next_batch()
                if not batch:
                    break
                for item in batch:
                    seen_issues.add(int(item["number"]))

                order_deps, hard_deps = _plan_queue_batch(repo_root, factory_dir, jobs_dir, cfg, batch)
                max_rounds = int(cfg.get("max_rounds", 3))
                runner_pid = os.getpid()
                for item in batch:
                    item_num = int(item["number"])
                    enqueue_job(
                        jobs_dir,
                        item,
                        max_rounds=max_rounds,
                        waiting_on=order_deps.get(item_num),
                        pid=runner_pid,
                    )

                sorter = graphlib.TopologicalSorter(order_deps)
                sorter.prepare()

                in_flight: dict[concurrent.futures.Future, int] = {}
                ready_queue: list[int] = []

                while sorter.is_active():
                    for num in sorter.get_ready():
                        if num not in ready_queue and num not in in_flight.values():
                            ready_queue.append(num)

                    while ready_queue and len(in_flight) < max_workers:
                        num = ready_queue.pop(0)
                        blocked_by = next((p for p in sorted(hard_deps.get(num, ())) if p in failed_parents), None)
                        if blocked_by is not None:
                            reason = failed_parents[blocked_by]
                            failed_parents[num] = reason
                            print(f"[job-{num}] skipped: blocked by prerequisite {reason}", file=sys.stderr)
                            try:
                                update_job(
                                    jobs_dir,
                                    f"job-{num}",
                                    last_error=f"Blocked by prerequisite {reason}",
                                    activity=f"blocked by #{blocked_by}",
                                    pid=None,
                                )
                            except FileNotFoundError:
                                pass
                            try:
                                gh.comment_issue(
                                    num,
                                    f"### Skipped (`job-{num}`)\n\nBlocked by prerequisite {reason}.",
                                )
                            except Exception:
                                pass
                            sorter.done(num)
                            continue
                        fut = pool.submit(_safe_worker, num)
                        in_flight[fut] = num

                    if not in_flight:
                        continue

                    done, _ = concurrent.futures.wait(
                        in_flight.keys(),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for fut in done:
                        num = in_flight.pop(fut)
                        res = fut.result()
                        if res is not None:
                            results.append(res)
                            if res.get("stage") != "merged":
                                failed_parents[num] = f"#{num} (stage={res.get('stage')})"
                        else:
                            failed_parents[num] = f"#{num} (worker error)"
                        sorter.done(num)

                if explicit_nums:
                    break

        return results
    finally:
        cur_pid = os.getpid()
        for existing in list_jobs(jobs_dir):
            if existing.get("pid") == cur_pid and existing.get("stage") == "queued":
                act = existing.get("activity")
                cleared_act = None if isinstance(act, str) and act.startswith("queued") else act
                try:
                    update_job(jobs_dir, existing["id"], activity=cleared_act, pid=None)
                except FileNotFoundError:
                    pass
        try:
            if os.path.isfile(pid_path) and _read_text(pid_path).strip() == str(cur_pid):
                os.remove(pid_path)
        except OSError:
            pass


def factory_runner_status(jobs_dir: str, jobs: list[dict] | None = None) -> str:
    """Return a clear one-line banner stating whether the factory runner is active."""
    pid_path = os.path.join(jobs_dir, ".factory.pid")
    raw_pid = _read_text(pid_path).strip() if os.path.isfile(pid_path) else ""
    runner_pid = int(raw_pid) if raw_pid.isdigit() else None
    all_jobs = jobs if jobs is not None else []
    queued_jobs = [j for j in all_jobs if j.get("stage") == "queued"]
    active_jobs = [
        j
        for j in all_jobs
        if j.get("stage") not in TERMINAL_OR_READY_STAGES
        and j.get("stage") not in {"needs-human", "needs-info", "queued"}
    ]
    live_workers = sum(1 for j in active_jobs if _is_pid_alive(j.get("pid")))

    if _is_pid_alive(runner_pid):
        if queued_jobs:
            return f"Factory: RUNNING (pid={runner_pid}, active_jobs={len(active_jobs)}, queued={len(queued_jobs)})"
        return f"Factory: RUNNING (pid={runner_pid}, active_jobs={len(active_jobs)})"
    if live_workers > 0:
        return f"Factory: RUNNING ({live_workers} live worker(s))"
    if active_jobs:
        stale_note = f"last run pid={runner_pid} exited" if runner_pid else "no live worker"
        return f"Factory: NOT RUNNING ({len(active_jobs)} unfinished/stale job(s); {stale_note})"
    return "Factory: NOT RUNNING"


def format_status_table(jobs: list[dict]) -> str:
    if not jobs:
        return "No factory jobs recorded."
    headers = ("JOB", "STAGE", "ACTIVITY", "ROUND", "SLICES", "RISK", "TOKENS", "COST", "PR")
    rows = [headers]
    for j in jobs:
        slices = f"{j.get('completed_slices', 0)}/{j.get('total_slices', 0)}"
        usage = j.get("usage") or {}
        tokens = str(usage.get("total_tokens", 0))
        cost_val = usage.get("cost_usd")
        cost_str = (
            f"${float(cost_val):.2f}"
            if isinstance(cost_val, (int, float)) and not isinstance(cost_val, bool) and cost_val > 0
            else "-"
        )
        pr_str = j.get("pr_url") or (f"#{j['pr_number']}" if j.get("pr_number") else "-")
        rows.append(
            (
                str(j.get("id", "")),
                str(j.get("stage", "")),
                _format_activity(j),
                f"{j.get('round', 0)}/{j.get('round_limit', 0)}",
                slices,
                str(j.get("risk", "-")),
                tokens,
                cost_str,
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
