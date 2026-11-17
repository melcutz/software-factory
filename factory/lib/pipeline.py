"""End-to-end autonomous software factory pipeline orchestrator and doctor checks."""
from __future__ import annotations

import concurrent.futures
import contextlib
import graphlib
import hashlib
import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

import factory.lib.governor as _governor
from factory.lib.agy_runner import AgentExecutionError, QuotaExhaustedError, resolve_agent_path, run_agent
from factory.lib.batch import run_batch_factory

_ORIG_RUN_AGENT = run_agent
from factory.lib.config import (
    estimate_usage_cost_usd,
    load_config,
    resolve_reviewers,
    split_model_and_effort,
)
from factory.lib.gate_runner import (
    auto_polish_worktree,
    find_protected_violations,
    run_gate,
    select_pre_merge_gate_scope,
)
from factory.lib.git_state import GitStateStore, LeaseLostError
from factory.lib.herdr_bridge import HerdrBridge
from factory.lib.planner import plan_dependency_dag
from factory.lib.project_board import ProjectBoardSync


def load_host_config():
    """Load local worker host configuration lazily to avoid circular imports."""
    from factory.lib.worker import load_host_config as _worker_load_host_config

    return _worker_load_host_config()


def is_host_draining() -> bool:
    """Return True if host is in `pause --drain` state."""
    from factory.lib import worker as _worker

    return _worker.is_host_draining()


_ORIG_CHECK_HOST_ADMISSION = _governor.check_host_admission
check_host_admission = _governor.check_host_admission
from factory.lib.git_ops import (
    GitHubClient,
    _run_git,
    abort_merge,
    commits_ahead,
    conflicted_files,
    delete_remote_branch,
    diff_stat,
    ensure_worktree,
    remove_worktree,
    head_sha,
    is_worktree_clean,
    merge_in_progress,
    push_branch,
    repo_lock,
    reset_worktree_clean,
    sync_with_base,
    try_resolve_generated_asset_conflicts,
)
from factory.lib.schemas import (
    SCHEMAS,
    render_approve_md,
    render_build_md,
    render_intake_md,
    render_pr_body,
    render_qa_failure_md,
    render_qa_md,
    render_review_md,
    render_spec_md,
)
from factory.lib.staging_qa import (
    StagingTrainCoordinator,
    TrainOutcome,
    TrainTicket,
    build_deploy_env,
    build_qa_agent_env,
    deploy_to_staging,
    ensure_staging_deploy_worktree,
    ensure_staging_qa_accounts,
    load_staging_secrets,
    plan_staging_deploy,
    prepare_qa_fix_branch,
    prepare_requeue_branch,
    redact_secrets,
    revert_commit_on_base,
    rollback_staging,
    snapshot_staging_versions,
    staging_lock,
    try_revert_commits_on_base,
)
from factory.lib.state import (
    TERMINAL_OR_READY_STAGES,
    advance_stage,
    create_or_resume_job,
    enqueue_job,
    list_jobs,
    load_job,
    record_metrics,
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


def _clear_quota_cooldown_if_needed(
    state_store: GitStateStore | None,
    model_spec: str,
) -> None:
    if state_store is None:
        return
    raw_spec = (model_spec or "").strip()
    if not raw_spec:
        return
    base_model = split_model_and_effort(raw_spec)[0].split(":", 1)[0].split()[0]
    if not base_model:
        return
    models = state_store.get_quota_state().get("models")
    if isinstance(models, dict) and base_model in models:
        state_store.clear_quota_cooldown(base_model)


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


def _is_job_live(job: dict) -> bool:
    pid = job.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and _is_pid_alive(pid):
        return True
    return bool(job.get("_lease_active"))


def _format_activity(job: dict) -> str:
    stage = str(job.get("stage", ""))
    if stage in TERMINAL_OR_READY_STAGES:
        return "-"
    activity = job.get("activity")
    if _is_job_live(job):
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


def _format_escalation_comment(job: dict, job_id: str, reason: str, labels: dict, resume_stage: str | None) -> str:
    clean_reason = (reason or "").strip()
    first_line = next((ln.strip() for ln in clean_reason.splitlines() if ln.strip()), "Unknown failure")
    if len(first_line) > 220:
        first_line = first_line[:217].rstrip() + "..."
    meta_parts = [f"Round: `{job.get('round', 0)}/{job.get('round_limit', 0)}`"]
    stage_label = resume_stage or job.get("escalated_from")
    if stage_label:
        meta_parts.insert(0, f"Failed at: `{stage_label}`")
    if job.get("last_worker_id"):
        meta_parts.append(f"Host: `{job['last_worker_id']}`")
    if job.get("pr_url"):
        meta_parts.append(f"PR: {job['pr_url']}")
    meta_line = " | ".join(meta_parts)

    if len(clean_reason) > 260 or "\n" in clean_reason:
        body = (
            f"**Summary:** {first_line}\n\n"
            f"<details>\n<summary>Full failure details</summary>\n\n{clean_reason}\n\n</details>"
        )
    else:
        body = f"**Summary:** {clean_reason}"

    return (
        f"### Factory Escalation (`{job_id}`)\n\n"
        f"{meta_line}\n\n"
        f"{body}\n\n"
        f"**Inspect:** `factory status {job_id}` / `factory logs {job_id}`\n"
        f"**Resume:** Reply on this issue and swap `{labels['needs_human']}` -> `{labels['queue']}`."
    )


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
        gh.comment_issue(
            issue_number,
            _format_escalation_comment(job, job_id, reason, labels, resume_stage),
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


def should_auto_approve_clean(
    cfg: dict,
    job: dict,
    checks_md: str,
    reviews_dict: dict[str, dict],
    qa_failure_md: str = "",
) -> bool:
    """Return True when a clean PR can be deterministically approved without an LLM call."""
    mode = cfg.get("auto_approve_clean", True)
    if not mode:
        return False
    approve_all_clean = mode == "all"
    if not approve_all_clean:
        if int(job.get("round", 0) or 0) != 1:
            return False
        if job.get("risk") == "high":
            return False
        if job.get("last_error"):
            return False
    if (qa_failure_md or "").strip():
        return False
    if not (checks_md or "").startswith("VERDICT: PASS"):
        return False

    expected = resolve_reviewers(cfg, job.get("risk", "low"), job.get("surfaces") or [])
    if not expected:
        return False
    for rev_name in expected:
        rev = reviews_dict.get(rev_name)
        if not isinstance(rev, dict) or rev.get("verdict") != "PASS":
            return False
        for finding in rev.get("findings") or ():
            if isinstance(finding, dict) and finding.get("severity") in {"must_fix", "should_fix"}:
                return False
    return True


def build_auto_approve_payload() -> dict:
    """Construct a schema-valid deterministic APPROVE payload."""
    return {
        "decision": "APPROVE",
        "summary": ["All deterministic gates and routed reviews passed cleanly on round 1."],
        "reasoning": (
            "Auto-approved deterministically: checks.sh passed and all routed reviewers "
            "returned PASS with zero must_fix/should_fix findings."
        ),
        "merge_danger": "none",
        "question": "",
    }


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
    *,
    state_store: GitStateStore | None = None,
) -> tuple[bool, str]:
    """Sync branch with origin/<base_branch>, using builder to resolve conflicts before pre-merge gate."""
    pre_sync_head = head_sha(worktree)
    had_code_conflicts = False
    ok, head_changed, detail = sync_with_base(worktree, base_branch, abort_on_conflict=False)
    if not ok:
        if not merge_in_progress(worktree):
            abort_merge(worktree)
            return (
                False,
                f"Merge conflict while syncing `{branch}` with `origin/{base_branch}`:\n\n```\n{detail[:1000]}\n```",
            )

        before_resolve = head_sha(worktree)
        if try_resolve_generated_asset_conflicts(worktree):
            _log(job_id, "merge-conflict auto-resolved generated asset conflict deterministically")
            head_changed = True
        else:
            had_code_conflicts = True
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

            _clear_quota_cooldown_if_needed(state_store, builder_model)
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

    force_full_gate = False
    try:
        force_full_gate = bool(load_job(jobs_dir, job_id).get("needs_pre_merge_gate"))
    except FileNotFoundError:
        force_full_gate = False

    if head_changed or force_full_gate:
        if cfg.get("auto_polish", True):
            if auto_polish_worktree(
                worktree,
                base_branch,
                cfg.get("protected_paths", []),
                allow_self_host=bool(cfg.get("allow_self_host", False)),
            ):
                _log(job_id, "auto-polished formatting/assets after base sync")
        if force_full_gate:
            pre_merge_scope = "full"
        else:
            upstream_diff = _run_git(
                worktree,
                "diff",
                "-z",
                "--name-only",
                "--no-renames",
                f"{pre_sync_head}..HEAD",
                check=False,
            )
            upstream_changed = [p for p in upstream_diff.stdout.split("\0") if p]
            pre_merge_scope = select_pre_merge_gate_scope(
                upstream_changed,
                had_code_conflicts=had_code_conflicts,
            )
        _set_activity(jobs_dir, job_id, f"running:pre-merge-gate:{pre_merge_scope}")
        _log(job_id, f"gate pre-merge scope={pre_merge_scope} running checks.sh")
        t_gate = time.monotonic()
        passed, sync_checks_md = run_gate(
            repo_root,
            job_id,
            worktree,
            round_num,
            cfg,
            tag="pre-merge",
            scope=pre_merge_scope,
        )
        gate_dur = time.monotonic() - t_gate
        record_metrics(jobs_dir, job_id, gate_seconds=gate_dur)
        _log(
            job_id,
            f"gate pre-merge scope={pre_merge_scope} verdict={'PASS' if passed else 'FAIL'} (dur={_fmt_duration(gate_dur)})",
        )
        if not passed:
            return (
                False,
                f"Post-sync gate failed after merging `origin/{base_branch}`:\n\n{sync_checks_md[:1200]}",
            )
        try:
            update_job(
                jobs_dir,
                job_id,
                needs_pre_merge_gate=False,
                verified_gate_sha=head_sha(worktree),
            )
        except FileNotFoundError:
            pass
        if head_sha(worktree) != pre_sync_head:
            push_branch(worktree, branch)

    return True, ""


def _call_deploy_to_staging(
    deploy_wt: str,
    deploy_env: dict[str, str],
    timeout_sec: int,
    extra_args: list[str],
    *,
    repo_root: str | None = None,
    staging_cfg: dict | None = None,
) -> tuple[bool, str]:
    sig = inspect.signature(deploy_to_staging)
    kwargs: dict = {}
    if "extra_args" in sig.parameters:
        kwargs["extra_args"] = extra_args
    if "repo_root" in sig.parameters:
        kwargs["repo_root"] = repo_root
    if "staging_cfg" in sig.parameters:
        kwargs["staging_cfg"] = staging_cfg
    if kwargs:
        return deploy_to_staging(deploy_wt, deploy_env, timeout_sec, **kwargs)
    return deploy_to_staging(deploy_wt, deploy_env, timeout_sec)


def _compute_train_changed_files(
    deploy_wt: str,
    train_base_sha: str,
    train_head_sha: str,
    merged_tickets: list[TrainTicket],
) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(paths: list[str]) -> None:
        for p in paths:
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)

    if train_base_sha:
        res = _run_git(
            deploy_wt,
            "diff",
            "-z",
            "--name-only",
            f"{train_base_sha}..{train_head_sha}",
            check=False,
        )
        if res.returncode == 0:
            _add([p for p in res.stdout.split("\0") if p])

    for t in merged_tickets:
        if not t.merged_sha:
            continue
        res = _run_git(
            deploy_wt,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            "-m",
            "--first-parent",
            t.merged_sha,
            check=False,
        )
        if res.returncode == 0:
            _add([p for p in res.stdout.split("\0") if p])

    return ordered


def _mark_ticket_merged(
    jobs_dir: str,
    gh_client: GitHubClient,
    labels: dict,
    secrets: dict[str, str],
    ticket: TrainTicket,
    qa_attempt: int,
    qa_limit: int,
    *,
    qa_out: dict | None = None,
    note: str = "",
) -> None:
    qa_fail_file = os.path.join(ticket.jdir, "qa-failure.md")
    if os.path.isfile(qa_fail_file):
        os.remove(qa_fail_file)
    merged_job = advance_stage(jobs_dir, ticket.job_id, "merged")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(jobs_dir)))
    _cleanup_terminal_job(repo_root, ticket.job_id, merged_job)
    try:
        gh_client.edit_labels(ticket.issue_number, remove=[labels["queue"]])
        if qa_out is not None:
            gh_client.comment_issue(
                ticket.issue_number,
                f"### Staging QA Passed (`{ticket.job_id}`, QA round {qa_attempt}/{qa_limit})\n\n"
                f"{redact_secrets(render_qa_md(qa_out), secrets)}",
            )
        elif note:
            gh_client.comment_issue(
                ticket.issue_number,
                f"### Merged (`{ticket.job_id}`)\n\n{note}",
            )
    except Exception as exc:
        print(
            f"[{ticket.job_id}] warning: post-QA issue update failed: {exc}",
            file=sys.stderr,
        )
    ticket.outcome = TrainOutcome(action="return", job=merged_job)
    ticket.done_event.set()


def _handle_qa_failure_for_ticket(
    repo_root: str,
    jobs_dir: str,
    base_branch: str,
    cfg: dict,
    gh_client: GitHubClient,
    secrets: dict[str, str],
    ticket: TrainTicket,
    qa_attempt: int,
    qa_limit: int,
    qa_dir: str,
    failure_reason: str,
    qa_out: dict | None,
    revert_sha: str,
    rb_healthy: bool,
    rb_log: str,
) -> None:
    t_job = load_job(jobs_dir, ticket.job_id)
    _write_text(
        os.path.join(qa_dir, "rollback.log"),
        redact_secrets(rb_log, secrets),
    )
    try:
        gh_client.reopen_issue(ticket.issue_number)
    except Exception as exc:
        print(
            f"[{ticket.job_id}] warning: reopen_issue failed: {exc}",
            file=sys.stderr,
        )

    failure_md = redact_secrets(
        render_qa_failure_md(qa_attempt, qa_limit, failure_reason, qa_out),
        secrets,
    )
    _write_text(os.path.join(qa_dir, "failure.md"), failure_md)
    _write_text(os.path.join(ticket.jdir, "qa-failure.md"), failure_md)

    prior_prs = list(t_job.get("prior_prs") or [])
    if t_job.get("pr_number"):
        prior_prs.append(
            {
                "pr_number": t_job["pr_number"],
                "pr_url": t_job.get("pr_url"),
                "merged_sha": ticket.merged_sha,
                "revert_sha": revert_sha,
                "qa_round": qa_attempt,
            }
        )

    if not rb_healthy:
        update_job(
            jobs_dir,
            ticket.job_id,
            qa_round=qa_attempt,
            pr_number=None,
            pr_url=None,
            pre_merge_main_sha="",
            merged_commit_sha="",
            solo_staging_qa=False,
            prior_prs=prior_prs,
        )
        esc_job = _escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            f"CRITICAL: Staging QA failed AND post-rollback health check failed on QA round "
            f"{qa_attempt}/{qa_limit}.\n\n{failure_md}\n\n"
            f"Rollback log:\n```\n{redact_secrets(rb_log, secrets)}\n```",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()
        return

    ext_blocker = str((qa_out or {}).get("external_blocker") or "").strip()
    if qa_attempt >= qa_limit or ext_blocker:
        update_job(
            jobs_dir,
            ticket.job_id,
            qa_round=qa_attempt,
            pr_number=None,
            pr_url=None,
            pre_merge_main_sha="",
            merged_commit_sha="",
            solo_staging_qa=False,
            prior_prs=prior_prs,
        )
        prefix = (
            f"External/infra blocker detected on QA round {qa_attempt}/{qa_limit}: {ext_blocker}."
            if ext_blocker
            else f"Staging QA failed {qa_attempt}/{qa_limit} rounds."
        )
        esc_job = _escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            f"{prefix} "
            f"Reverted `{ticket.merged_sha[:7]}` on `{base_branch}` (`{revert_sha[:7]}`) and rolled back "
            f"staging cleanly.\n\n{failure_md}",
            resume_stage="staging_qa",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()
        return

    new_branch = prepare_qa_fix_branch(
        repo_root,
        ticket.worktree,
        ticket.job_id,
        qa_attempt + 1,
        base_branch,
        revert_sha,
        pre_train_head_sha=ticket.pre_train_head_sha,
    )
    update_job(
        jobs_dir,
        ticket.job_id,
        qa_round=qa_attempt,
        branch=new_branch,
        pr_number=None,
        pr_url=None,
        pre_merge_main_sha="",
        merged_commit_sha="",
        solo_staging_qa=False,
        prior_prs=prior_prs,
    )
    try:
        gh_client.comment_issue(
            ticket.issue_number,
            f"### Staging QA Rollback (`{ticket.job_id}`, QA round {qa_attempt}/{qa_limit})\n\n"
            f"Reverted `{ticket.merged_sha[:7]}` on `{base_branch}` (`{revert_sha[:7]}`) and rolled back "
            f"staging. Retrying fix on `{new_branch}`.\n\n{failure_md[:2000]}",
        )
    except Exception as exc:
        print(
            f"[{ticket.job_id}] warning: post-rollback comment failed: {exc}",
            file=sys.stderr,
        )

    advance_stage(
        jobs_dir,
        ticket.job_id,
        "build",
        preserve_last_error=bool(t_job.get("last_error")),
        expand_round_by=int(cfg["max_rounds"]),
    )
    ticket.outcome = TrainOutcome(action="continue")
    ticket.done_event.set()


def _requeue_innocent_ticket(
    repo_root: str,
    jobs_dir: str,
    base_branch: str,
    cfg: dict,
    gh_client: GitHubClient,
    secrets: dict[str, str],
    ticket: TrainTicket,
    revert_sha: str,
    rb_healthy: bool,
    rb_log: str,
    *,
    solo_staging_qa: bool = False,
) -> None:
    try:
        gh_client.reopen_issue(ticket.issue_number)
    except Exception as exc:
        print(
            f"[{ticket.job_id}] warning: reopen_issue on innocent requeue failed: {exc}",
            file=sys.stderr,
        )

    if not rb_healthy:
        esc_job = _escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            "Staging QA passed for this PR, but a sibling PR in the coalesced staging train "
            "failed and post-rollback health check failed.\n\n"
            f"Rollback log:\n```\n{redact_secrets(rb_log, secrets)}\n```",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()
        return

    p_job = load_job(jobs_dir, ticket.job_id)
    prior_prs = list(p_job.get("prior_prs") or [])
    requeue_num = sum(1 for item in prior_prs if item.get("requeued")) + 1
    new_branch = prepare_requeue_branch(
        repo_root,
        ticket.worktree,
        ticket.job_id,
        requeue_num,
        base_branch,
        revert_sha,
        pre_train_head_sha=ticket.pre_train_head_sha,
    )
    push_branch(ticket.worktree, new_branch)
    intake_data = _read_json(os.path.join(ticket.jdir, "intake.json"))
    spec_data = _read_json(os.path.join(ticket.jdir, "spec.json"))
    pr_title = p_job["issue_title"] or f"Fix #{ticket.issue_number}"
    pr_body = render_pr_body(p_job, intake_data, spec_data)
    new_pr_num, new_pr_url = gh_client.ensure_draft_pr(
        ticket.worktree,
        new_branch,
        base_branch,
        pr_title,
        pr_body,
        pr_number=None,
    )
    gh_client.update_pr(new_pr_num, pr_title, pr_body)
    if p_job.get("pr_number"):
        prior_prs.append(
            {
                "pr_number": p_job["pr_number"],
                "pr_url": p_job.get("pr_url"),
                "merged_sha": ticket.merged_sha,
                "revert_sha": revert_sha,
                "qa_round": int(p_job.get("qa_round", 0)),
                "requeued": True,
            }
        )
    update_job(
        jobs_dir,
        ticket.job_id,
        branch=new_branch,
        pr_number=new_pr_num,
        pr_url=new_pr_url,
        pre_merge_main_sha="",
        merged_commit_sha="",
        needs_pre_merge_gate=True,
        verified_gate_sha="",
        solo_staging_qa=solo_staging_qa,
        prior_prs=prior_prs,
    )
    advance_stage(
        jobs_dir,
        ticket.job_id,
        "merging",
        preserve_last_error=bool(p_job.get("last_error")),
    )
    mode_tag = " (solo=true)" if solo_staging_qa else ""
    _log(
        ticket.job_id,
        f"requeued innocent PR onto {new_branch} (#{new_pr_num}) at stage=merging{mode_tag}",
    )
    ticket.outcome = TrainOutcome(action="continue")
    ticket.done_event.set()


def _safe_finalize_train_ticket(
    jobs_dir: str,
    gh_client: GitHubClient,
    cfg: dict,
    ticket: TrainTicket,
    fn,
) -> None:
    try:
        fn()
    except Exception as exc:
        if os.path.isdir(ticket.worktree):
            abort_merge(ticket.worktree)
        esc_job = _escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            f"Failed to restore PR branch after staging train revert ({type(exc).__name__}): {exc}",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()


@contextlib.contextmanager
def _distributed_staging_lock(
    state_store: GitStateStore | None,
    repo_root: str,
    batch: list[TrainTicket],
):
    if state_store is None:
        yield
        return
    while not state_store.acquire_staging_lock([t.job_id for t in batch]):
        check_halt_or_lease(repo_root, batch[0].job_id, state_store)
        time.sleep(2.0)
    try:
        yield
    finally:
        state_store.release_staging_lock()


def _run_distributed_staging_train(
    *,
    repo_root: str,
    factory_dir: str,
    jobs_dir: str,
    cfg: dict,
    gh_client: GitHubClient,
    state_store: GitStateStore,
    job_id: str,
    issue_number: int,
    jdir: str,
    worktree: str,
    branch: str,
    issue_md: str,
    initial_stage: str,
    solo: bool,
) -> TrainOutcome:
    """Coordinate multi-worker/multi-machine staging train batches via refs/factory/train/*."""
    staging_cfg = cfg.get("staging_qa") or {}
    base_branch = cfg["base_branch"]
    max_batch_size = max(1, int(staging_cfg.get("max_batch_size", 4)))
    coalesce_window_sec = max(0.0, float(staging_cfg.get("coalesce_window_sec", 15)))

    from factory.lib.worker import _is_lease_expired

    existing_remote = next(
        (rj for rj in state_store.list_remote_jobs() if rj.get("id") == job_id),
        None,
    )
    if existing_remote and str(existing_remote.get("stage") or "") in {"merged", "rejected"}:
        return TrainOutcome(action="exit", job=existing_remote)

    state_store.put_train_ticket(
        job_id,
        {
            "job_id": job_id,
            "issue_number": issue_number,
            "branch": branch,
            "solo": solo,
            "initial_stage": initial_stage,
        },
    )
    _checkpoint_if_store(state_store, job_id, jdir, jobs_dir)

    while True:
        if state_store.acquire_staging_lock([job_id]):
            try:
                if not solo and coalesce_window_sec > 0.0 and max_batch_size > 1:
                    deadline = time.monotonic() + coalesce_window_sec
                    while time.monotonic() < deadline:
                        tickets = state_store.list_train_tickets()
                        non_solo = [t for t in tickets if not t.get("solo")]
                        if len(non_solo) >= max_batch_size:
                            break
                        peers_near = any(
                            rj.get("id") != job_id and rj.get("stage") in {"review", "approve", "merging"}
                            for rj in state_store.list_remote_jobs()
                        )
                        if not peers_near:
                            break
                        time.sleep(min(0.2, max(0.05, deadline - time.monotonic())))

                raw_tickets = state_store.list_train_tickets()
                remote_jobs_by_id = {
                    str(rj.get("id")): rj
                    for rj in state_store.list_remote_jobs()
                    if rj.get("id")
                }
                workers_by_id = state_store.list_worker_heartbeats()
                now_ts = time.time()
                valid_tickets: list[dict] = []
                for t in raw_tickets:
                    tid = str(t.get("job_id") or "")
                    if tid and tid != job_id:
                        rj = remote_jobs_by_id.get(tid)
                        if rj is not None:
                            r_stage = str(rj.get("stage") or "")
                            r_lease = rj.get("lease")
                            if r_stage in {"merged", "rejected", "needs-human", "needs-info"} or (
                                isinstance(r_lease, dict)
                                and _is_lease_expired(r_lease, workers_by_id, now_ts)
                            ):
                                state_store.delete_train_ticket(tid)
                                continue
                    valid_tickets.append(t)
                raw_tickets = valid_tickets
                if not any(t.get("job_id") == job_id for t in raw_tickets):
                    raw_tickets.insert(
                        0,
                        {
                            "job_id": job_id,
                            "issue_number": issue_number,
                            "branch": branch,
                            "solo": solo,
                            "initial_stage": initial_stage,
                        },
                    )
                if solo or raw_tickets[0].get("solo"):
                    lead_id = raw_tickets[0]["job_id"] if raw_tickets[0].get("solo") else job_id
                    selected = [t for t in raw_tickets if t.get("job_id") == lead_id][:1]
                else:
                    selected = [t for t in raw_tickets if not t.get("solo")][:max_batch_size]

                batch: list[TrainTicket] = []
                for t_data in selected:
                    tid = str(t_data["job_id"])
                    t_issue = int(t_data["issue_number"])
                    t_branch = str(t_data.get("branch") or f"factory/{tid}")
                    t_jdir = os.path.join(jobs_dir, tid)
                    if tid == job_id:
                        t_wt = worktree
                        t_issue_md = issue_md
                    else:
                        state_store.hydrate_job(tid, t_jdir)
                        t_wt, t_branch = ensure_worktree(repo_root, tid, base_branch, branch=t_branch)
                        try:
                            _, t_issue_md = gh_client.fetch_issue(t_issue)
                        except Exception:
                            t_issue_md = _read_text(os.path.join(t_jdir, "issue.md"))
                    batch.append(
                        TrainTicket(
                            job_id=tid,
                            issue_number=t_issue,
                            jdir=t_jdir,
                            worktree=t_wt,
                            branch=t_branch,
                            issue_md=t_issue_md,
                            initial_stage=str(t_data.get("initial_stage") or "merging"),
                            solo=bool(t_data.get("solo")),
                        )
                    )

                _execute_staging_train_batch(
                    batch,
                    repo_root=repo_root,
                    factory_dir=factory_dir,
                    jobs_dir=jobs_dir,
                    cfg=cfg,
                    gh_client=gh_client,
                    state_store=state_store,
                    manage_distributed_lock=False,
                )

                for t in batch:
                    _checkpoint_if_store(
                        state_store,
                        t.job_id,
                        t.jdir,
                        jobs_dir,
                        release_lease=(t.job_id != job_id),
                    )
                    state_store.delete_train_ticket(t.job_id)

                if any(t.job_id == job_id for t in batch):
                    updated = load_job(jobs_dir, job_id)
                    if updated["stage"] == "build" or (
                        updated["stage"] == "staging_qa" and updated.get("solo_staging_qa")
                    ):
                        return TrainOutcome(action="continue")
                    return TrainOutcome(action="exit", job=updated)
            finally:
                state_store.release_staging_lock()
        else:
            # Non-leader: wait until leader processes and deletes our ticket, or lock frees up.
            while True:
                if not state_store._ls_remote_one(f"refs/factory/train/{job_id}"):
                    hydrated = state_store.hydrate_job(job_id, jdir) or load_job(jobs_dir, job_id)
                    if hydrated["stage"] == "build" or (
                        hydrated["stage"] == "staging_qa" and hydrated.get("solo_staging_qa")
                    ):
                        return TrainOutcome(action="continue")
                    return TrainOutcome(action="exit", job=hydrated)
                if not state_store._ls_remote_one("refs/factory/locks/staging"):
                    break
                time.sleep(0.2)


def _execute_staging_train_batch(
    batch: list[TrainTicket],
    *,
    repo_root: str,
    factory_dir: str,
    jobs_dir: str,
    cfg: dict,
    gh_client: GitHubClient,
    state_store: GitStateStore | None = None,
    manage_distributed_lock: bool = True,
) -> None:
    staging_cfg = cfg.get("staging_qa") or {}
    labels = cfg["labels"]
    base_branch = cfg["base_branch"]
    max_qa_rounds = int(staging_cfg.get("max_qa_rounds", 3))

    for t in batch:
        _set_activity(jobs_dir, t.job_id, "waiting:staging_lock")

    dist_ctx = (
        _distributed_staging_lock(state_store, repo_root, batch)
        if manage_distributed_lock
        else contextlib.nullcontext()
    )
    with dist_ctx, staging_lock(jobs_dir):
        secrets = load_staging_secrets(staging_cfg.get("env_files") or [], repo_root=repo_root)
        merged_tickets: list[TrainTicket] = []
        train_base_sha = ""

        already_merged: list[TrainTicket] = []
        to_merge: list[tuple[TrainTicket, dict]] = []

        for t in batch:
            t_job = load_job(jobs_dir, t.job_id)
            t_stage = t_job["stage"]
            if os.path.isdir(t.worktree):
                t.pre_train_head_sha = head_sha(t.worktree)

            if t_stage == "staging_qa":
                t.pre_merge_sha = str(t_job.get("pre_merge_main_sha") or "")
                t.merged_sha = str(t_job.get("merged_commit_sha") or "")
                if t_job.get("solo_staging_qa"):
                    update_job(jobs_dir, t.job_id, solo_staging_qa=False)
                already_merged.append(t)
            elif t_stage == "merging":
                pr_num = int(t_job["pr_number"])
                existing_sha = gh_client.get_pr_merge_commit(pr_num)
                if existing_sha:
                    pre_m = str(t_job.get("pre_merge_main_sha") or "")
                    update_job(
                        jobs_dir,
                        t.job_id,
                        pre_merge_main_sha=pre_m,
                        merged_commit_sha=existing_sha,
                        solo_staging_qa=False,
                    )
                    t_job = advance_stage(
                        jobs_dir,
                        t.job_id,
                        "staging_qa",
                        preserve_last_error=bool(t_job.get("last_error")),
                    )
                    _log(
                        t.job_id,
                        f"stage=staging_qa qa_round={t_job.get('qa_round', 0) + 1}/{t_job.get('qa_round_limit', max_qa_rounds)}",
                    )
                    t.pre_merge_sha = pre_m
                    t.merged_sha = existing_sha
                    already_merged.append(t)
                else:
                    to_merge.append((t, t_job))
            else:
                t.outcome = TrainOutcome(action="return", job=t_job)
                t.done_event.set()

        if already_merged:
            with repo_lock(repo_root):
                _run_git(repo_root, "fetch", "origin", base_branch)
                if len(already_merged) > 1:
                    rev_out = _run_git(
                        repo_root,
                        "rev-list",
                        "--first-parent",
                        "--reverse",
                        f"origin/{base_branch}",
                        check=False,
                    ).stdout
                    fp_index = {
                        sha: idx
                        for idx, sha in enumerate(rev_out.splitlines())
                        if sha.strip()
                    }
                    already_merged.sort(
                        key=lambda item: fp_index.get(item.merged_sha, 10**9)
                    )
                earliest = already_merged[0]
                if earliest.pre_merge_sha:
                    train_base_sha = earliest.pre_merge_sha
                elif earliest.merged_sha:
                    p_res = _run_git(
                        repo_root,
                        "rev-parse",
                        f"{earliest.merged_sha}^1",
                        check=False,
                    )
                    if p_res.returncode == 0:
                        train_base_sha = p_res.stdout.strip()
            merged_tickets.extend(already_merged)

        for t, t_job in to_merge:
            round_num = int(t_job["round"])
            pr_num = int(t_job["pr_number"])
            _set_activity(jobs_dir, t.job_id, "waiting:repo_lock")
            with repo_lock(repo_root):
                _set_activity(jobs_dir, t.job_id, "running:merging")
                sync_ok, sync_err = _sync_pr_with_base(
                    repo_root,
                    factory_dir,
                    jobs_dir,
                    t.jdir,
                    t.job_id,
                    t.worktree,
                    t.branch,
                    base_branch,
                    round_num,
                    cfg,
                    state_store=state_store,
                )
                if not sync_ok:
                    esc_job = _escalate_to_human(
                        jobs_dir,
                        t.job_id,
                        t.issue_number,
                        gh_client,
                        cfg,
                        sync_err,
                    )
                    t.outcome = TrainOutcome(action="return", job=esc_job)
                    t.done_event.set()
                    continue

                _run_git(repo_root, "fetch", "origin", base_branch)
                pre_merge_sha = _run_git(
                    repo_root, "rev-parse", f"origin/{base_branch}"
                ).stdout.strip()
                if not train_base_sha:
                    train_base_sha = pre_merge_sha
                gh_client.mark_pr_ready(pr_num)
                gh_client.merge_pr(pr_num, cfg["merge_method"])
                merged_sha = gh_client.get_pr_merge_commit(pr_num)
                _run_git(repo_root, "fetch", "origin", base_branch)
                if not merged_sha:
                    merged_sha = _run_git(
                        repo_root, "rev-parse", f"origin/{base_branch}"
                    ).stdout.strip()
                update_job(
                    jobs_dir,
                    t.job_id,
                    pre_merge_main_sha=pre_merge_sha,
                    merged_commit_sha=merged_sha,
                    solo_staging_qa=False,
                )
                t_job = advance_stage(
                    jobs_dir,
                    t.job_id,
                    "staging_qa",
                    preserve_last_error=bool(t_job.get("last_error")),
                )
                _log(
                    t.job_id,
                    f"stage=staging_qa qa_round={t_job.get('qa_round', 0) + 1}/{t_job.get('qa_round_limit', max_qa_rounds)}",
                )
                t.pre_merge_sha = pre_merge_sha
                t.merged_sha = merged_sha
                merged_tickets.append(t)

        if not merged_tickets:
            return

        deploy_wt, train_head_sha = ensure_staging_deploy_worktree(repo_root, base_branch)
        for t in merged_tickets:
            if not t.merged_sha:
                t.merged_sha = train_head_sha
                update_job(jobs_dir, t.job_id, merged_commit_sha=t.merged_sha)

        changed_files = _compute_train_changed_files(
            deploy_wt, train_base_sha, train_head_sha, merged_tickets
        )
        deploy_plan = plan_staging_deploy(changed_files, repo_root=repo_root)

        meta: dict[str, tuple[int, int, str]] = {}
        for t in merged_tickets:
            t_job = load_job(jobs_dir, t.job_id)
            qa_attempt = int(t_job.get("qa_round", 0)) + 1
            qa_limit = int(t_job.get("qa_round_limit") or max_qa_rounds)
            qa_dir = os.path.join(t.jdir, f"qa-{qa_attempt}")
            os.makedirs(qa_dir, exist_ok=True)
            meta[t.job_id] = (qa_attempt, qa_limit, qa_dir)

        if deploy_plan.skip_non_runtime:
            for t in merged_tickets:
                qa_attempt, qa_limit, qa_dir = meta[t.job_id]
                _write_text(
                    os.path.join(qa_dir, "deploy.log"),
                    "Skipped staging deploy and browser QA: non-runtime changes only.\n",
                )
                _log(t.job_id, "staging_qa skipped (non-runtime changes only)")
                _mark_ticket_merged(
                    jobs_dir,
                    gh_client,
                    labels,
                    secrets,
                    t,
                    qa_attempt,
                    qa_limit,
                    note="Skipped staging deploy and browser QA because diff touched non-runtime files only.",
                )
            return

        deploy_env = build_deploy_env(secrets, staging_cfg, repo_root=repo_root)
        qa_env = build_qa_agent_env(secrets, staging_cfg, repo_root=repo_root)
        base_url = staging_cfg.get("base_url", "")
        ops_url = staging_cfg.get("ops_base_url", "")
        deploy_timeout_sec = int(staging_cfg.get("deploy_timeout_sec", 900))
        qa_timeout_sec = int(staging_cfg.get("qa_timeout_sec", 1200))

        snapshot = snapshot_staging_versions(deploy_wt, deploy_env, repo_root=repo_root)

        flags_label = " ".join(deploy_plan.extra_args) or "(full)"
        for t in merged_tickets:
            _set_activity(jobs_dir, t.job_id, "running:deploy")
            _log(
                t.job_id,
                f"staging_qa deploy=start sha={t.merged_sha[:7]} train_size={len(merged_tickets)} flags={flags_label}",
            )

        t_dep = time.monotonic()
        try:
            deploy_ok, deploy_log = _call_deploy_to_staging(
                deploy_wt,
                deploy_env,
                deploy_timeout_sec,
                deploy_plan.extra_args,
                repo_root=repo_root,
                staging_cfg=staging_cfg,
            )
        except Exception as exc:
            deploy_ok = False
            deploy_log = f"{type(exc).__name__}: {exc}"
        dep_dur = time.monotonic() - t_dep
        for t in merged_tickets:
            record_metrics(jobs_dir, t.job_id, deploy_seconds=dep_dur)

        redacted_deploy_log = redact_secrets(deploy_log, secrets)
        for t in merged_tickets:
            _, _, qa_dir = meta[t.job_id]
            _log(
                t.job_id,
                f"staging_qa deploy={'OK' if deploy_ok else 'FAIL'} (dur={_fmt_duration(dep_dur)})",
            )
            _write_text(os.path.join(qa_dir, "deploy.log"), redacted_deploy_log)

        passed_tickets: list[TrainTicket] = []
        failed_tickets: list[TrainTicket] = []
        qa_results: dict[str, tuple[dict | None, str]] = {}
        shared_infra_failure = False

        if not deploy_ok:
            shared_infra_failure = True
            dep_reason = (
                "Staging deploy or post-deploy E2E smoke failed:\n\n```\n"
                f"{redacted_deploy_log[-1500:]}\n```"
            )
            for t in merged_tickets:
                failed_tickets.append(t)
                qa_results[t.job_id] = (None, dep_reason)
        else:
            seed_err = ""
            try:
                ensure_staging_qa_accounts(deploy_wt, deploy_env, repo_root=repo_root)
            except Exception as exc:
                seed_err = (
                    f"Staging QA account seed failed ({type(exc).__name__}): "
                    f"{redact_secrets(str(exc), secrets)}"
                )

            if seed_err:
                shared_infra_failure = True
                for t in merged_tickets:
                    failed_tickets.append(t)
                    qa_results[t.job_id] = (None, seed_err)
            else:
                qa_model = cfg["models"]["qa"]

                def _run_ticket_qa(t: TrainTicket) -> tuple[str, bool, dict | None, str]:
                    qa_attempt, qa_limit, qa_dir = meta[t.job_id]
                    qa_out: dict | None = None
                    try:
                        _set_activity(jobs_dir, t.job_id, f"running:browser-qa-r{qa_attempt}")
                        _log(
                            t.job_id,
                            f"staging_qa browser_qa=start round={qa_attempt}/{qa_limit} ({_fmt_model_tag(qa_model)})",
                        )
                        spec_md = _read_text(os.path.join(t.jdir, "spec.md"))
                        t_job = _read_json(os.path.join(t.jdir, "job.json"))
                        surfaces = t_job.get("surfaces") or []
                        qa_mode = "browser" if "ui" in surfaces else "http_fast"
                        merged_stat = _run_git(
                            deploy_wt, "show", "--stat", t.merged_sha, check=False
                        ).stdout.strip()
                        qa_prompt = "\n".join(
                            [
                                f"JOB_ID: {t.job_id}",
                                f"ISSUE_NUMBER: {t.issue_number}",
                                f"QA_ROUND: {qa_attempt}/{qa_limit}",
                                f"REPO_ROOT: {repo_root}",
                                f"QA_ARTIFACTS_DIR: {qa_dir}",
                                f"MERGED_COMMIT_SHA: {t.merged_sha}",
                                f"STAGING_BASE_URL: {base_url}",
                                f"STAGING_OPS_BASE_URL: {ops_url}",
                                f"SURFACES: {', '.join(surfaces) or 'none'}",
                                f"QA_MODE: {qa_mode}",
                                "",
                                "## Merged Commit Stat",
                                merged_stat or "(none)",
                                "",
                                "## Specification",
                                spec_md,
                                "",
                                "## Issue",
                                t.issue_md,
                            ]
                        )
                        qa_out, usage, dur = run_agent(
                            factory_dir,
                            t.jdir,
                            deploy_wt,
                            "qa",
                            "qa",
                            qa_model,
                            qa_prompt,
                            SCHEMAS["qa"],
                            qa_timeout_sec,
                            sandbox=False,
                            clean_worktree=False,
                            log_name=f"qa-{qa_attempt}",
                            extra_env=qa_env,
                            protect_head=True,
                        )
                        _clear_quota_cooldown_if_needed(state_store, qa_model)
                        metrics_str = _record_and_format_step(
                            jobs_dir, t.job_id, qa_model, usage, dur, cfg
                        )
                        _log(
                            t.job_id,
                            f"staging_qa browser_qa=done verdict={qa_out['verdict']} ({metrics_str})",
                        )
                        _write_json(os.path.join(qa_dir, "qa.json"), qa_out)
                        _write_text(
                            os.path.join(qa_dir, "qa.md"),
                            redact_secrets(render_qa_md(qa_out), secrets),
                        )
                        if qa_out["verdict"] == "PASS":
                            return t.job_id, True, qa_out, ""
                        return (
                            t.job_id,
                            False,
                            qa_out,
                            "Staging QA found user-facing regressions or "
                            "broken acceptance criteria in staging.",
                        )
                    except Exception as exc:
                        return (
                            t.job_id,
                            False,
                            qa_out,
                            f"Staging QA step failed ({type(exc).__name__}): "
                            f"{redact_secrets(str(exc), secrets)}",
                        )
                    finally:
                        for suffix in ("", "-retry"):
                            log_p = os.path.join(t.jdir, "logs", f"qa-{qa_attempt}{suffix}.json")
                            if os.path.isfile(log_p):
                                _write_text(log_p, redact_secrets(_read_text(log_p), secrets))

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, len(merged_tickets))
                ) as qa_pool:
                    per_ticket = dict(
                        (jid, (ok, qout, reason))
                        for jid, ok, qout, reason in qa_pool.map(_run_ticket_qa, merged_tickets)
                    )

                if not is_worktree_clean(deploy_wt):
                    reset_worktree_clean(deploy_wt)

                for t in merged_tickets:
                    ok, qout, reason = per_ticket[t.job_id]
                    qa_results[t.job_id] = (qout, reason)
                    if ok:
                        passed_tickets.append(t)
                    else:
                        failed_tickets.append(t)

        if not failed_tickets:
            for t in passed_tickets:
                qa_attempt, qa_limit, _ = meta[t.job_id]
                qout, _ = qa_results[t.job_id]
                _mark_ticket_merged(
                    jobs_dir,
                    gh_client,
                    labels,
                    secrets,
                    t,
                    qa_attempt,
                    qa_limit,
                    qa_out=qout,
                )
            return

        if not passed_tickets:
            rev_shas = [t.merged_sha for t in reversed(merged_tickets)]
            ok_rev, revert_map, _ = try_revert_commits_on_base(deploy_wt, base_branch, rev_shas)
            if not ok_rev:
                revert_map = {
                    t.merged_sha: revert_commit_on_base(deploy_wt, base_branch, t.merged_sha)
                    for t in reversed(merged_tickets)
                }
            rb_healthy, rb_log = rollback_staging(
                deploy_wt,
                deploy_env,
                snapshot,
                repo_root=repo_root,
            )
            if shared_infra_failure and len(merged_tickets) > 1:
                for t in merged_tickets:
                    _safe_finalize_train_ticket(
                        jobs_dir,
                        gh_client,
                        cfg,
                        t,
                        lambda tk=t: _requeue_innocent_ticket(
                            repo_root,
                            jobs_dir,
                            base_branch,
                            cfg,
                            gh_client,
                            secrets,
                            tk,
                            revert_map[tk.merged_sha],
                            rb_healthy,
                            rb_log,
                            solo_staging_qa=True,
                        ),
                    )
                return

            for t in failed_tickets:
                qa_attempt, qa_limit, qa_dir = meta[t.job_id]
                qout, reason = qa_results[t.job_id]
                _safe_finalize_train_ticket(
                    jobs_dir,
                    gh_client,
                    cfg,
                    t,
                    lambda tk=t, qa_a=qa_attempt, qa_l=qa_limit, qd=qa_dir, qo=qout, rsn=reason: _handle_qa_failure_for_ticket(
                        repo_root,
                        jobs_dir,
                        base_branch,
                        cfg,
                        gh_client,
                        secrets,
                        tk,
                        qa_a,
                        qa_l,
                        qd,
                        rsn,
                        qo,
                        revert_map[tk.merged_sha],
                        rb_healthy,
                        rb_log,
                    ),
                )
            return

        culprit_shas = [t.merged_sha for t in reversed(failed_tickets)]
        surgical_ok, culprit_revert_map, conflict_detail = try_revert_commits_on_base(
            deploy_wt,
            base_branch,
            culprit_shas,
        )
        full_revert_map: dict[str, str] = {}

        if surgical_ok:
            rem_changed = _compute_train_changed_files(
                deploy_wt,
                train_base_sha,
                head_sha(deploy_wt),
                passed_tickets,
            )
            rem_plan = plan_staging_deploy(rem_changed, repo_root=repo_root)
            if rem_plan.skip_non_runtime:
                redeploy_ok = True
                redeploy_log = "Remaining passing PRs are non-runtime only."
            else:
                redeploy_ok, redeploy_log = _call_deploy_to_staging(
                    deploy_wt,
                    deploy_env,
                    deploy_timeout_sec,
                    rem_plan.extra_args,
                    repo_root=repo_root,
                    staging_cfg=staging_cfg,
                )
            if redeploy_ok:
                for t_p in passed_tickets:
                    qa_attempt, qa_limit, _ = meta[t_p.job_id]
                    qout, _ = qa_results[t_p.job_id]
                    _mark_ticket_merged(
                        jobs_dir,
                        gh_client,
                        labels,
                        secrets,
                        t_p,
                        qa_attempt,
                        qa_limit,
                        qa_out=qout,
                    )
                rb_note = (
                    "Surgically reverted culprit commit(s) and re-deployed passing PR(s).\n"
                    f"{redeploy_log}"
                )
                for t_f in failed_tickets:
                    qa_attempt, qa_limit, qa_dir = meta[t_f.job_id]
                    qout, reason = qa_results[t_f.job_id]
                    _safe_finalize_train_ticket(
                        jobs_dir,
                        gh_client,
                        cfg,
                        t_f,
                        lambda tk=t_f, qa_a=qa_attempt, qa_l=qa_limit, qd=qa_dir, qo=qout, rsn=reason: _handle_qa_failure_for_ticket(
                            repo_root,
                            jobs_dir,
                            base_branch,
                            cfg,
                            gh_client,
                            secrets,
                            tk,
                            qa_a,
                            qa_l,
                            qd,
                            rsn,
                            qo,
                            culprit_revert_map[tk.merged_sha],
                            True,
                            rb_note,
                        ),
                    )
                return

            ok_rem, innocent_revert_map, _ = try_revert_commits_on_base(
                deploy_wt,
                base_branch,
                [t.merged_sha for t in reversed(passed_tickets)],
            )
            if not ok_rem:
                innocent_revert_map = {
                    t.merged_sha: revert_commit_on_base(deploy_wt, base_branch, t.merged_sha)
                    for t in reversed(passed_tickets)
                }
            full_revert_map = {**culprit_revert_map, **innocent_revert_map}
        else:
            first_line = conflict_detail.splitlines()[0] if conflict_detail else "conflict"
            _log(
                failed_tickets[0].job_id,
                f"surgical revert conflicted ({first_line}); reverting full train",
            )
            ok_all, full_revert_map, _ = try_revert_commits_on_base(
                deploy_wt,
                base_branch,
                [t.merged_sha for t in reversed(merged_tickets)],
            )
            if not ok_all:
                full_revert_map = {
                    t.merged_sha: revert_commit_on_base(deploy_wt, base_branch, t.merged_sha)
                    for t in reversed(merged_tickets)
                }

        rb_healthy, rb_log = rollback_staging(
            deploy_wt,
            deploy_env,
            snapshot,
            repo_root=repo_root,
        )
        for t_f in failed_tickets:
            qa_attempt, qa_limit, qa_dir = meta[t_f.job_id]
            qout, reason = qa_results[t_f.job_id]
            _safe_finalize_train_ticket(
                jobs_dir,
                gh_client,
                cfg,
                t_f,
                lambda tk=t_f, qa_a=qa_attempt, qa_l=qa_limit, qd=qa_dir, qo=qout, rsn=reason: _handle_qa_failure_for_ticket(
                    repo_root,
                    jobs_dir,
                    base_branch,
                    cfg,
                    gh_client,
                    secrets,
                    tk,
                    qa_a,
                    qa_l,
                    qd,
                    rsn,
                    qo,
                    full_revert_map[tk.merged_sha],
                    rb_healthy,
                    rb_log,
                ),
            )
        for t_p in passed_tickets:
            _safe_finalize_train_ticket(
                jobs_dir,
                gh_client,
                cfg,
                t_p,
                lambda tk=t_p: _requeue_innocent_ticket(
                    repo_root,
                    jobs_dir,
                    base_branch,
                    cfg,
                    gh_client,
                    secrets,
                    tk,
                    full_revert_map[tk.merged_sha],
                    rb_healthy,
                    rb_log,
                ),
            )


class JobHaltedError(RuntimeError):
    """Raised when global or local halt signal is active."""


def _default_machine_id() -> str:
    try:
        from factory.lib.worker import load_host_config

        return load_host_config().machine_id
    except Exception:
        return socket.gethostname()


def _halt_signal_path() -> str:
    host_dir = os.path.expanduser(os.environ.get("FACTORY_HOST_DIR") or os.path.join("~", ".config", "factory"))
    return os.path.join(host_dir, "halt.signal")


def is_local_halted() -> bool:
    return os.path.isfile(_halt_signal_path())


def _check_remote_lease_and_control(
    jobs_dir: str,
    job_id: str,
    store: GitStateStore,
) -> BaseException | None:
    ctrl = store.get_control_state()
    if ctrl.get("halted"):
        return JobHaltedError(f"fleet halted: {ctrl.get('reason') or 'operator halt'}")
    commit_sha = store._fetch_ref(f"refs/factory/jobs/{job_id}")
    if commit_sha:
        remote_job = store._read_json_from_commit(commit_sha, "job.json")
        if isinstance(remote_job, dict):
            remote_lease = remote_job.get("lease")
            try:
                local_job = load_job(jobs_dir, job_id)
            except FileNotFoundError:
                local_job = {}
            local_lease = local_job.get("lease") or {}
            local_token = int(local_lease.get("fencing_token") or local_job.get("_last_fencing_token") or 0)
            if isinstance(remote_lease, dict):
                remote_owner = remote_lease.get("worker_id") or remote_lease.get("holder")
                remote_token = int(remote_lease.get("fencing_token") or 0)
                if remote_owner and remote_owner != store.machine_id:
                    return LeaseLostError(f"{job_id} lease owned by {remote_owner}")
                if local_token and remote_token > local_token:
                    return LeaseLostError(f"{job_id} fencing token advanced ({remote_token} > {local_token})")
    return None


def check_halt_or_lease(repo_root: str, job_id: str, store: GitStateStore | None) -> None:
    """Raise JobHaltedError or LeaseLostError if local/fleet halt is active or lease is lost."""
    if is_local_halted():
        raise JobHaltedError("local halt.signal active")
    if store is not None:
        jobs_dir = os.path.join(repo_root, "factory", "jobs")
        exc = _check_remote_lease_and_control(jobs_dir, job_id, store)
        if exc is not None:
            raise exc


def _has_origin_remote(repo_root: str) -> bool:
    """Return True if the repository has a configured `origin` git remote."""
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return res.returncode == 0 and bool((res.stdout or "").strip())
    except Exception:
        return False


def _build_initial_lease_stub(jobs_dir: str, job_id: str, issue_num: int, machine_id: str) -> dict:
    try:
        return load_job(jobs_dir, job_id)
    except FileNotFoundError:
        return {
            "id": job_id,
            "issue": issue_num,
            "issue_number": issue_num,
            "stage": "queued",
            "branch": f"factory/{job_id}",
            "worker_id": machine_id,
            "last_worker_id": machine_id,
        }


def _ensure_lease_released(
    state_store: GitStateStore | None,
    jobs_dir: str,
    jdir: str,
    job_id: str,
) -> None:
    """Release remote lease if `process_job` was mocked or exited without releasing it."""
    if state_store is None:
        return
    try:
        remote_sha = state_store._fetch_ref(f"refs/factory/jobs/{job_id}")
        if not remote_sha:
            return
        remote_job = state_store._read_json_from_commit(remote_sha, "job.json")
        if not isinstance(remote_job, dict):
            return
        lease = remote_job.get("lease")
        if not isinstance(lease, dict):
            return
        owner = lease.get("worker_id") or lease.get("holder")
        if owner != state_store.machine_id:
            return
        try:
            local_job = load_job(jobs_dir, job_id)
        except FileNotFoundError:
            local_job = remote_job
        state_store.checkpoint_job(job_id, jdir, local_job, release_lease=True)
        try:
            token = int(lease.get("fencing_token") or local_job.get("_last_fencing_token") or 0)
            updates: dict = {"lease": None}
            if token:
                updates["_last_fencing_token"] = token
            update_job(jobs_dir, job_id, **updates)
        except FileNotFoundError:
            pass
    except Exception:
        pass


def _make_abort_check(
    repo_root: str,
    job_id: str,
    jobs_dir: str,
    store: GitStateStore | None,
    *,
    remote_interval_sec: float = 15.0,
):
    """Build a lightweight `abort_check` hook for `run_agent()`.

    Checks local `halt.signal` on every watchdog tick (~0.2s) and throttles
    remote `refs/factory/control` and lease fencing-token checks to once every
    `remote_interval_sec` seconds so git remotes are not spammed.
    """
    last_remote_check = time.monotonic()

    def _check() -> BaseException | None:
        nonlocal last_remote_check
        if is_local_halted():
            return JobHaltedError("local halt.signal active")
        if store is not None:
            now = time.monotonic()
            if (now - last_remote_check) >= remote_interval_sec:
                last_remote_check = now
                try:
                    return _check_remote_lease_and_control(jobs_dir, job_id, store)
                except Exception:
                    pass
        return None

    return _check


def _call_run_agent(*args, abort_check=None, **kwargs):
    """Invoke `run_agent`, passing `abort_check` when accepted by the callable."""
    try:
        sig = inspect.signature(run_agent)
        if "abort_check" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            return run_agent(*args, abort_check=abort_check, **kwargs)
    except (ValueError, TypeError):
        pass
    return run_agent(*args, **kwargs)


def _checkpoint_if_store(
    store: GitStateStore | None,
    job_id: str,
    jdir: str,
    jobs_dir: str,
    *,
    worktree: str | None = None,
    release_lease: bool = False,
) -> None:
    if store is None:
        return
    if worktree and os.path.isdir(worktree):
        store.push_slice_checkpoint(job_id, worktree)
    job_data = load_job(jobs_dir, job_id)
    store.checkpoint_job(job_id, jdir, job_data, release_lease=release_lease)
    if release_lease:
        token = int((job_data.get("lease") or {}).get("fencing_token") or job_data.get("_last_fencing_token") or 0)
        updates: dict = {"lease": None}
        if token:
            updates["_last_fencing_token"] = token
        update_job(jobs_dir, job_id, **updates)


def select_builder_model(
    cfg: dict,
    job: dict,
    *,
    attempt: int,
    is_fix_round: bool = False,
    is_conflict: bool = False,
) -> str:
    """Select fast builder_low on clean low-risk attempt 0, otherwise escalate to models['builder']."""
    models = cfg.get("models") or {}
    low_model = models.get("builder_low")
    if (
        isinstance(low_model, str)
        and low_model.strip()
        and (job.get("risk") or "") == "low"
        and attempt == 0
        and not is_fix_round
        and not is_conflict
    ):
        return low_model.strip()
    return models["builder"]


def _check_cost_budget(
    jobs_dir: str,
    job_id: str,
    job: dict,
    cfg: dict,
    gh_client: GitHubClient,
    issue_number: int,
    labels: dict,
) -> dict | None:
    """Escalate to needs-human if max_job_cost_usd > 0 and job spend reached the cap."""
    max_cost = float(cfg.get("max_job_cost_usd") or 0.0)
    if max_cost <= 0.0:
        return None
    spent = float((job.get("usage") or {}).get("cost_usd") or 0.0)
    if spent >= max_cost:
        return _escalate_to_human(
            jobs_dir,
            job_id,
            issue_number,
            gh_client,
            cfg,
            f"Job cost cap exceeded: spent ${spent:.4f} >= max_job_cost_usd ${max_cost:.2f}",
        )
    return None


def _cleanup_terminal_job(repo_root: str, job_id: str, job: dict) -> None:
    """Remove worktree and superseded remote branches for terminal merged/rejected jobs."""
    if job.get("stage") not in ("merged", "rejected"):
        return
    wt_path = os.path.join(repo_root, ".worktrees", job_id)
    try:
        remove_worktree(repo_root, wt_path)
    except Exception:
        pass
    branches: list[str] = []
    for item in job.get("prior_prs") or []:
        if isinstance(item, dict) and item.get("branch"):
            branches.append(str(item["branch"]))
    if job.get("branch"):
        branches.append(str(job["branch"]))
    seen: set[str] = set()
    for br in branches:
        if br and br not in seen:
            seen.add(br)
            try:
                delete_remote_branch(repo_root, br)
            except Exception:
                pass


def _finalize_job_exit(
    state_store: GitStateStore | None,
    job_id: str,
    jdir: str,
    jobs_dir: str,
    job: dict,
    *,
    worktree: str | None = None,
    base_branch: str = "main",
) -> dict:
    """Checkpoint final/paused job state and release lease when exiting `process_job`."""
    _set_activity(jobs_dir, job_id, None)
    if state_store is not None:
        st = job.get("stage")
        wt_arg = None
        if (
            st not in TERMINAL_OR_READY_STAGES
            and worktree
            and os.path.isdir(worktree)
            and commits_ahead(worktree, base_branch) > 0
        ):
            wt_arg = worktree
        _checkpoint_if_store(state_store, job_id, jdir, jobs_dir, worktree=wt_arg, release_lease=True)
        if st in TERMINAL_OR_READY_STAGES:
            state_store.delete_slice_checkpoint(job_id)
    final_job = load_job(jobs_dir, job_id)
    herdr = HerdrBridge.from_env()
    if herdr.enabled:
        st_final = str(final_job.get("stage") or "")
        cost_val = float((final_job.get("usage") or {}).get("cost_usd") or 0.0)
        herdr.set_labels(
            {
                "issue": str(final_job.get("issue") or ""),
                "stage": st_final,
                "round": str(final_job.get("round") or 0),
                "slice": f"{int(final_job.get('completed_slices') or 0)}/{int(final_job.get('total_slices') or 0)}",
                "cost": f"${cost_val:.4f}",
            }
        )
        if st_final in {"needs-info", "needs-human"}:
            herdr.emit_status("blocked", kind="question")
        elif st_final in TERMINAL_OR_READY_STAGES:
            herdr.emit_status("idle")
    if final_job.get("stage") in ("merged", "rejected"):
        if worktree and os.path.basename(os.path.dirname(os.path.abspath(worktree))) == ".worktrees":
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(worktree)))
        else:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(jobs_dir)))
        _cleanup_terminal_job(repo_root, job_id, final_job)
    return final_job


def run_halt(repos: list[str], reason: str = "", force: bool = False) -> int:
    """Activate global halt signal locally and across all target repos' refs/factory/control."""
    sig_path = _halt_signal_path()
    os.makedirs(os.path.dirname(sig_path), exist_ok=True)
    with open(sig_path, "w", encoding="utf-8") as f:
        json.dump({"halted": True, "reason": reason, "force": bool(force)}, f, indent=2)

    mid = _default_machine_id()
    for repo in repos:
        try:
            store = GitStateStore(repo, mid)
            store.set_halted(True, reason=reason)
        except Exception as exc:
            print(f"[halt] warning: failed to update refs/factory/control in {repo}: {exc}", file=sys.stderr)
    return 0


def run_resume(repos: list[str]) -> int:
    """Clear local halt signal and set halted=false on all target repos' refs/factory/control."""
    sig_path = _halt_signal_path()
    if os.path.isfile(sig_path):
        os.remove(sig_path)

    mid = _default_machine_id()
    for repo in repos:
        try:
            store = GitStateStore(repo, mid)
            store.set_halted(False, reason="")
        except Exception as exc:
            print(f"[resume] warning: failed to clear refs/factory/control in {repo}: {exc}", file=sys.stderr)
    return 0


def process_job(
    repo_root: str,
    issue_number: int,
    config: dict | None = None,
    gh: GitHubClient | None = None,
    train_coordinator: StagingTrainCoordinator | None = None,
    board: ProjectBoardSync | None = None,
    state_store: GitStateStore | None = None,
) -> dict:
    """Drive one GitHub issue from its current/resumed stage until paused or completed."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    cfg = config or load_config(factory_dir)
    gh_client = gh or GitHubClient(repo_root)
    board_sync = board if board is not None else ProjectBoardSync.discover(repo_root)
    labels = cfg["labels"]
    base_branch = cfg["base_branch"]
    timeout_sec = int(cfg["agent_timeout_sec"])
    staging_cfg = cfg.get("staging_qa") or {}
    max_qa_rounds = int(staging_cfg.get("max_qa_rounds", 3))

    pre_job_id = f"job-{int(issue_number)}"
    pre_jdir = os.path.join(jobs_dir, pre_job_id)
    if state_store is not None:
        state_store.hydrate_job(pre_job_id, pre_jdir)

    issue_data, issue_md = gh_client.fetch_issue(issue_number)

    job, action = create_or_resume_job(
        jobs_dir,
        issue_data,
        max_rounds=cfg["max_rounds"],
        max_qa_rounds=max_qa_rounds,
    )
    job_id = job["id"]
    issue_url = job.get("issue_url") or issue_data.get("url", "")
    jdir = os.path.join(jobs_dir, job_id)
    _write_text(os.path.join(jdir, "issue.md"), issue_md)

    if action == "skip":
        if board_sync:
            board_sync.sync_stage(issue_url, job["stage"])
        try:
            gh_client.edit_labels(issue_number, remove=[labels["queue"]])
        except Exception as exc:
            print(f"[{job_id}] warning: failed to remove queue label on skipped job: {exc}", file=sys.stderr)
        return _finalize_job_exit(state_store, job_id, jdir, jobs_dir, job)

    worktree = ""
    halted_cleanly = False
    abort_check = _make_abort_check(repo_root, job_id, jobs_dir, state_store)
    try:
        worktree, branch = ensure_worktree(repo_root, job_id, base_branch, branch=job.get("branch"))
        if state_store is not None:
            if int(job.get("completed_slices") or 0) > 0 or int(job.get("round") or 0) > 0:
                fetched_ckpt = state_store.fetch_slice_checkpoint(job_id, worktree)
                if not fetched_ckpt:
                    res_br = subprocess.run(
                        ["git", "fetch", state_store.remote, f"refs/heads/{branch}"],
                        cwd=worktree,
                        capture_output=True,
                        text=True,
                    )
                    if res_br.returncode == 0:
                        subprocess.run(
                            ["git", "reset", "--hard", "FETCH_HEAD"],
                            cwd=worktree,
                            capture_output=True,
                            text=True,
                        )

        herdr = HerdrBridge.from_env()
        _STAGE_PROGRESS = {
            "intake": 10,
            "spec": 25,
            "build": 40,
            "gate": 75,
            "review": 85,
            "approve": 90,
            "merging": 95,
            "staging_qa": 95,
        }

        def _emit_herdr_stage(stage_name: str, job_data: dict, *, slice_num: int = 0) -> None:
            if not herdr.enabled:
                return
            total_sl = int(job_data.get("total_slices") or 0)
            pct = _STAGE_PROGRESS.get(stage_name, 50)
            if stage_name == "build" and total_sl > 0 and slice_num > 0:
                pct = 40 + int(30 * (slice_num / total_sl))
            cost_val = float((job_data.get("usage") or {}).get("cost_usd") or 0.0)
            lbls = {
                "issue": str(issue_number),
                "stage": stage_name,
                "round": str(job_data.get("round") or 0),
                "slice": f"{slice_num}/{total_sl}",
                "cost": f"${cost_val:.4f}",
            }
            if stage_name in {"needs-info", "needs-human"}:
                herdr.emit_status("blocked", kind="question")
            elif stage_name in TERMINAL_OR_READY_STAGES:
                herdr.emit_status("idle")
            else:
                herdr.emit_status("working", progress=pct)
            herdr.set_labels(lbls)

        last_checkpointed_stage = ""
        while True:
            check_halt_or_lease(repo_root, job_id, state_store)
            job = load_job(jobs_dir, job_id)
            branch = job.get("branch") or f"factory/{job_id}"
            stage = job["stage"]
            if state_store is not None and stage != last_checkpointed_stage:
                _checkpoint_if_store(state_store, job_id, jdir, jobs_dir)
                last_checkpointed_stage = stage
            if board_sync:
                board_sync.sync_stage(issue_url, stage)
            _emit_herdr_stage(stage, job, slice_num=int(job.get("completed_slices") or 0))
            if stage in TERMINAL_OR_READY_STAGES or stage in {"needs-info", "needs-human"}:
                return _finalize_job_exit(state_store, job_id, jdir, jobs_dir, job)

            if stage in {"intake", "spec", "build", "review", "approve"}:
                budget_esc = _check_cost_budget(jobs_dir, job_id, job, cfg, gh_client, issue_number, labels)
                if budget_esc is not None:
                    return _finalize_job_exit(
                        state_store, job_id, jdir, jobs_dir, budget_esc, worktree=worktree, base_branch=base_branch
                    )

            _log(job_id, f"stage={stage} round={job['round']}")

            if stage == "intake":
                intake_model = cfg["models"]["intake"]
                _set_activity(jobs_dir, job_id, "running:intake")
                _log(job_id, f"intake start ({_fmt_model_tag(intake_model)})")
                prompt = f"JOB_ID: {job_id}\nBASE_BRANCH: {base_branch}\n\n{issue_md}"
                intake_out, usage, dur = _call_run_agent(
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
                    abort_check=abort_check,
                )
                _clear_quota_cooldown_if_needed(state_store, intake_model)
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
                    return _finalize_job_exit(
                        state_store,
                        job_id,
                        jdir,
                        jobs_dir,
                        advance_stage(jobs_dir, job_id, "needs-info"),
                    )

                if decision == "REJECT":
                    gh_client.comment_issue(
                        issue_number,
                        f"### Closing Issue (`{job_id}`)\n\n{intake_out['reasoning']}",
                    )
                    gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                    gh_client.close_issue(issue_number)
                    return _finalize_job_exit(
                        state_store,
                        job_id,
                        jdir,
                        jobs_dir,
                        advance_stage(jobs_dir, job_id, "rejected"),
                    )

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
                spec_out, usage, dur = _call_run_agent(
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
                    abort_check=abort_check,
                )
                _clear_quota_cooldown_if_needed(state_store, spec_model)
                metrics_str = _record_and_format_step(jobs_dir, job_id, spec_model, usage, dur, cfg)
                _log(job_id, f"spec done slices={len(spec_out['slices'])} ({metrics_str})")

                allow_self_host = bool(cfg.get("allow_self_host", False))
                planned_files = [
                    str(f)
                    for sl in (spec_out.get("slices") or [])
                    for f in (sl.get("files") or [])
                ]
                violations = find_protected_violations(
                    planned_files,
                    cfg.get("protected_paths", []),
                    allow_self_host=allow_self_host,
                )
                if violations:
                    budget_esc = _check_cost_budget(
                        jobs_dir, job_id, load_job(jobs_dir, job_id), cfg, gh_client, issue_number, labels
                    )
                    if budget_esc is not None:
                        return _finalize_job_exit(
                            state_store, job_id, jdir, jobs_dir, budget_esc, worktree=worktree, base_branch=base_branch
                        )
                    _log(job_id, f"spec protected_paths violation={violations}; retrying spec-writer once")
                    retry_prompt = (
                        f"{prompt}\n\n## Protected Path Violation (Required Fix)\n"
                        f"Your previous spec planned edits to protected paths: {', '.join(violations)}.\n"
                        "Revise `slices[*].files` so NO slice touches any protected path."
                    )
                    spec_out, usage, dur = _call_run_agent(
                        factory_dir,
                        jdir,
                        worktree,
                        "spec",
                        "spec-writer",
                        spec_model,
                        retry_prompt,
                        SCHEMAS["spec"],
                        timeout_sec,
                        sandbox=True,
                        log_name="spec-retry",
                        abort_check=abort_check,
                    )
                    _clear_quota_cooldown_if_needed(state_store, spec_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, spec_model, usage, dur, cfg)
                    _log(job_id, f"spec retry done slices={len(spec_out['slices'])} ({metrics_str})")
                    planned_files = [
                        str(f)
                        for sl in (spec_out.get("slices") or [])
                        for f in (sl.get("files") or [])
                    ]
                    violations = find_protected_violations(
                        planned_files,
                        cfg.get("protected_paths", []),
                        allow_self_host=allow_self_host,
                    )
                    if violations:
                        esc_job = _escalate_to_human(
                            jobs_dir,
                            job_id,
                            issue_number,
                            gh_client,
                            cfg,
                            f"Spec planned protected paths that cannot be modified: {', '.join(violations)}",
                        )
                        return _finalize_job_exit(
                            state_store, job_id, jdir, jobs_dir, esc_job, worktree=worktree, base_branch=base_branch
                        )

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
                qa_failure_md = _read_text(os.path.join(jdir, "qa-failure.md"))
                plan_data = _read_json(os.path.join(jdir, "plan.json"), {"slices": []})
                slices = plan_data.get("slices", [])
                total_slices = len(slices)
                prior_escalation = job.get("last_error") or ""
                is_fix_round = round_num > 1 or bool(qa_failure_md.strip()) or bool(prior_escalation.strip())

                if job["completed_slices"] < total_slices:
                    for idx in range(job["completed_slices"], total_slices):
                        slice_num = idx + 1
                        sl = slices[idx]
                        retry_feedback = ""
                        slice_done = False
                        _, prior_slices_md = _aggregate_slice_builds(jdir, idx)

                        for attempt in range(cfg["max_slice_retries"] + 1):
                            budget_esc = _check_cost_budget(
                                jobs_dir, job_id, load_job(jobs_dir, job_id), cfg, gh_client, issue_number, labels
                            )
                            if budget_esc is not None:
                                return _finalize_job_exit(
                                    state_store, job_id, jdir, jobs_dir, budget_esc, worktree=worktree, base_branch=base_branch
                                )
                            builder_model = select_builder_model(
                                cfg,
                                job,
                                attempt=attempt,
                                is_fix_round=is_fix_round,
                            )
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
                            if qa_failure_md:
                                prompt_parts.extend(["", "## Staging QA Failure (Must Fix)", qa_failure_md])
                            if retry_feedback:
                                prompt_parts.extend(["", "## Previous Attempt Failure (Must Fix)", retry_feedback])

                            build_out, usage, dur = _call_run_agent(
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
                                abort_check=abort_check,
                            )
                            _clear_quota_cooldown_if_needed(state_store, builder_model)
                            metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
                            _log(
                                job_id,
                                f"build slice={slice_num}/{total_slices} attempt={attempt + 1} verdict={build_out['verdict']} ({metrics_str})",
                            )

                            if build_out["verdict"] == "BLOCKED":
                                esc_job = _escalate_to_human(
                                    jobs_dir,
                                    job_id,
                                    issue_number,
                                    gh_client,
                                    cfg,
                                    f"Builder blocked on slice {slice_num}/{total_slices}: {build_out['blocked_reason']}",
                                )
                                return _finalize_job_exit(
                                    state_store,
                                    job_id,
                                    jdir,
                                    jobs_dir,
                                    esc_job,
                                    worktree=worktree,
                                    base_branch=base_branch,
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

                            if cfg.get("auto_polish", True):
                                if auto_polish_worktree(
                worktree,
                base_branch,
                cfg.get("protected_paths", []),
                allow_self_host=bool(cfg.get("allow_self_host", False)),
            ):
                                    _log(job_id, f"build slice={slice_num}/{total_slices} auto-polished formatting/assets")

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
                            record_metrics(jobs_dir, job_id, gate_seconds=gate_dur)
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
                                check_halt_or_lease(repo_root, job_id, state_store)
                                if state_store is not None and slice_num < total_slices:
                                    draining = is_host_draining()
                                    admitted = True
                                    if not draining:
                                        check_fn = (
                                            check_host_admission
                                            if check_host_admission is not _ORIG_CHECK_HOST_ADMISSION
                                            else _governor.check_host_admission
                                        )
                                        admitted, _ = check_fn(load_host_config())
                                    if draining or not admitted:
                                        yield_activity = "paused: draining" if draining else "yielded: host busy"
                                        halted_cleanly = True
                                        _set_activity(jobs_dir, job_id, yield_activity)
                                        _checkpoint_if_store(
                                            state_store,
                                            job_id,
                                            jdir,
                                            jobs_dir,
                                            worktree=worktree,
                                            release_lease=True,
                                        )
                                        return load_job(jobs_dir, job_id)
                                _checkpoint_if_store(state_store, job_id, jdir, jobs_dir, worktree=worktree)
                                slice_done = True
                                break

                            retry_feedback = f"Deterministic gate failed after slice {slice_num}:\n\n{slice_checks_md}"

                        if not slice_done:
                            esc_job = _escalate_to_human(
                                jobs_dir,
                                job_id,
                                issue_number,
                                gh_client,
                                cfg,
                                f"Slice {slice_num}/{total_slices} (`{sl['title']}`) failed deterministic gate after "
                                f"{cfg['max_slice_retries'] + 1} attempts.\n\n{retry_feedback[:1500]}",
                            )
                            return _finalize_job_exit(
                                state_store,
                                job_id,
                                jdir,
                                jobs_dir,
                                esc_job,
                                worktree=worktree,
                                base_branch=base_branch,
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
                    builder_model = select_builder_model(cfg, job, attempt=0, is_fix_round=True)
                    _set_activity(jobs_dir, job_id, f"running:fix-round-{round_num}")
                    _log(job_id, f"build fix-round={round_num} start ({_fmt_model_tag(builder_model)})")
                    build_out, usage, dur = _call_run_agent(
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
                        abort_check=abort_check,
                    )
                    _clear_quota_cooldown_if_needed(state_store, builder_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
                    _log(job_id, f"build fix-round={round_num} verdict={build_out['verdict']} ({metrics_str})")
                    if build_out["verdict"] == "BLOCKED":
                        esc_job = _escalate_to_human(
                            jobs_dir,
                            job_id,
                            issue_number,
                            gh_client,
                            cfg,
                            f"Builder blocked in round {round_num}: {build_out['blocked_reason']}",
                        )
                        return _finalize_job_exit(
                            state_store,
                            job_id,
                            jdir,
                            jobs_dir,
                            esc_job,
                            worktree=worktree,
                            base_branch=base_branch,
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
                        if cfg.get("auto_polish", True):
                            if auto_polish_worktree(
                worktree,
                base_branch,
                cfg.get("protected_paths", []),
                allow_self_host=bool(cfg.get("allow_self_host", False)),
            ):
                                _log(job_id, "gate auto-polished formatting/assets before checks.sh")
                        _set_activity(jobs_dir, job_id, f"running:gate-{attempt + 1}")
                        _log(job_id, f"gate scope=full running checks.sh (attempt={attempt + 1})")
                        t_gate = time.monotonic()
                        passed, checks_md = run_gate(repo_root, job_id, worktree, round_num, cfg, scope="full")
                        gate_dur = time.monotonic() - t_gate
                        record_metrics(jobs_dir, job_id, gate_seconds=gate_dur)
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
                    fix_out, usage, dur = _call_run_agent(
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
                        abort_check=abort_check,
                    )
                    _clear_quota_cooldown_if_needed(state_store, builder_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, builder_model, usage, dur, cfg)
                    _log(job_id, f"gate-fix attempt={attempt + 1} verdict={fix_out['verdict']} ({metrics_str})")
                    if fix_out["verdict"] == "BLOCKED":
                        esc_job = _escalate_to_human(
                            jobs_dir,
                            job_id,
                            issue_number,
                            gh_client,
                            cfg,
                            f"Builder blocked while fixing gate in round {round_num}: {fix_out['blocked_reason']}",
                        )
                        return _finalize_job_exit(
                            state_store,
                            job_id,
                            jdir,
                            jobs_dir,
                            esc_job,
                            worktree=worktree,
                            base_branch=base_branch,
                        )

                if not passed:
                    if job["round"] < job["round_limit"]:
                        advance_stage(jobs_dir, job_id, "build", preserve_last_error=bool(job.get("last_error")))
                        continue
                    esc_job = _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Deterministic gate failed after {job['round']} round(s):\n\n{checks_md[:1500]}",
                    )
                    return _finalize_job_exit(
                        state_store,
                        job_id,
                        jdir,
                        jobs_dir,
                        esc_job,
                        worktree=worktree,
                        base_branch=base_branch,
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
                    out, u, d = _call_run_agent(
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
                        abort_check=_make_abort_check(repo_root, job_id, jobs_dir, state_store),
                    )
                    _clear_quota_cooldown_if_needed(state_store, rev_model)
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
                    esc_job = _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Reviewers still requested changes after {job['round']} round(s):\n\n{unresolved}",
                    )
                    return _finalize_job_exit(
                        state_store,
                        job_id,
                        jdir,
                        jobs_dir,
                        esc_job,
                        worktree=worktree,
                        base_branch=base_branch,
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

                qa_failure_md = _read_text(os.path.join(jdir, "qa-failure.md"))
                if should_auto_approve_clean(cfg, job, checks_md, reviews_dict, qa_failure_md):
                    approve_out = build_auto_approve_payload()
                    _log(job_id, "approve decision=APPROVE (auto-approved clean round-1 fast-path)")
                else:
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
                    approve_out, usage, dur = _call_run_agent(
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
                        abort_check=abort_check,
                    )
                    _clear_quota_cooldown_if_needed(state_store, approver_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, approver_model, usage, dur, cfg)
                    _log(job_id, f"approve decision={approve_out['decision']} ({metrics_str})")
                _write_json(os.path.join(round_dir, "approve.json"), approve_out)
                _write_text(os.path.join(round_dir, "approve.md"), render_approve_md(approve_out))

                if approve_out["decision"] == "ESCALATE":
                    esc_job = _escalate_to_human(
                        jobs_dir,
                        job_id,
                        issue_number,
                        gh_client,
                        cfg,
                        f"Approver requested human review:\n\n**Question:** {approve_out['question']}\n\n{approve_out['reasoning']}",
                    )
                    return _finalize_job_exit(
                        state_store,
                        job_id,
                        jdir,
                        jobs_dir,
                        esc_job,
                        worktree=worktree,
                        base_branch=base_branch,
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
                return _finalize_job_exit(state_store, job_id, jdir, jobs_dir, ready_job)

            if stage in {"merging", "staging_qa"}:
                sqa_enabled = bool(staging_cfg.get("enabled"))
                if stage == "merging" and not sqa_enabled:
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
                                state_store=state_store,
                            )
                            if not sync_ok:
                                esc_job = _escalate_to_human(
                                    jobs_dir,
                                    job_id,
                                    issue_number,
                                    gh_client,
                                    cfg,
                                    sync_err,
                                )
                                return _finalize_job_exit(
                                    state_store,
                                    job_id,
                                    jdir,
                                    jobs_dir,
                                    esc_job,
                                    worktree=worktree,
                                    base_branch=base_branch,
                                )

                            gh_client.mark_pr_ready(pr_num)
                            gh_client.merge_pr(pr_num, cfg["merge_method"])
                        merged_job = advance_stage(jobs_dir, job_id, "merged")
                        delete_remote_branch(repo_root, branch)

                    try:
                        gh_client.edit_labels(issue_number, remove=[labels["queue"]])
                    except Exception as exc:
                        print(f"[{job_id}] warning: post-merge label cleanup failed: {exc}", file=sys.stderr)
                    return _finalize_job_exit(state_store, job_id, jdir, jobs_dir, merged_job)

                if train_coordinator is None and state_store is not None:
                    outcome = _run_distributed_staging_train(
                        repo_root=repo_root,
                        factory_dir=factory_dir,
                        jobs_dir=jobs_dir,
                        cfg=cfg,
                        gh_client=gh_client,
                        state_store=state_store,
                        job_id=job_id,
                        issue_number=issue_number,
                        jdir=jdir,
                        worktree=worktree,
                        branch=branch,
                        issue_md=issue_md,
                        initial_stage=stage,
                        solo=bool(job.get("solo_staging_qa")),
                    )
                else:
                    active_coord = train_coordinator or StagingTrainCoordinator(
                        max_batch_size=1,
                        coalesce_window_sec=0.0,
                    )
                    ticket = TrainTicket(
                        job_id=job_id,
                        issue_number=issue_number,
                        jdir=jdir,
                        worktree=worktree,
                        branch=branch,
                        issue_md=issue_md,
                        initial_stage=stage,
                        solo=bool(job.get("solo_staging_qa")),
                    )
                    outcome = active_coord.run_ticket(
                        ticket,
                        lambda batch: _execute_staging_train_batch(
                            batch,
                            repo_root=repo_root,
                            factory_dir=factory_dir,
                            jobs_dir=jobs_dir,
                            cfg=cfg,
                            gh_client=gh_client,
                            state_store=state_store,
                        ),
                    )
                if outcome.action == "continue":
                    continue
                assert outcome.job is not None
                return _finalize_job_exit(
                    state_store,
                    job_id,
                    jdir,
                    jobs_dir,
                    outcome.job,
                    worktree=worktree,
                    base_branch=base_branch,
                )

            raise RuntimeError(f"Unhandled stage {stage!r} for {job_id}")

    except JobHaltedError:
        halted_cleanly = True
        if worktree and os.path.isdir(worktree):
            reset_worktree_clean(worktree)
        _set_activity(jobs_dir, job_id, "halted")
        if state_store is not None:
            wt_arg = (
                worktree
                if worktree and os.path.isdir(worktree) and commits_ahead(worktree, base_branch) > 0
                else None
            )
            _checkpoint_if_store(state_store, job_id, jdir, jobs_dir, worktree=wt_arg, release_lease=True)
        else:
            update_job(jobs_dir, job_id, lease=None)
        return load_job(jobs_dir, job_id)
    except QuotaExhaustedError as exc:
        halted_cleanly = True
        if worktree and os.path.isdir(worktree):
            reset_worktree_clean(worktree)
        _set_activity(jobs_dir, job_id, "cooldown: quota")
        if state_store is not None:
            base_model, _ = split_model_and_effort(exc.model_spec)
            base_model = base_model.split(":", 1)[0].split()[0]
            state_store.record_quota_cooldown(base_model, str(exc), exc.retry_after_sec)
            _checkpoint_if_store(state_store, job_id, jdir, jobs_dir, release_lease=True)
        return load_job(jobs_dir, job_id)
    except LeaseLostError:
        if worktree and os.path.isdir(worktree):
            reset_worktree_clean(worktree)
        return load_job(jobs_dir, job_id)
    except (AgentExecutionError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        cur = load_job(jobs_dir, job_id)
        if cur["stage"] not in TERMINAL_OR_READY_STAGES and cur["stage"] not in {"needs-info", "needs-human"}:
            esc_job = _escalate_to_human(
                jobs_dir,
                job_id,
                issue_number,
                gh_client,
                cfg,
                f"Pipeline error during `{cur['stage']}`: {exc}",
            )
            return _finalize_job_exit(
                state_store,
                job_id,
                jdir,
                jobs_dir,
                esc_job,
                worktree=worktree,
                base_branch=base_branch,
            )
        raise
    finally:
        if not halted_cleanly:
            _set_activity(jobs_dir, job_id, None)
        if board_sync:
            try:
                final_job = load_job(jobs_dir, job_id)
                board_sync.sync_stage(final_job.get("issue_url") or issue_url, final_job["stage"])
            except Exception:
                pass


def _plan_queue_batch(
    repo_root: str,
    factory_dir: str,
    jobs_dir: str,
    cfg: dict,
    issues: list[dict],
    state_store: GitStateStore | None = None,
) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """Build `(order_deps, hard_deps)` DAGs for a batch of issues using explicit refs + optional LLM planner."""
    issue_set_hash = ""
    if state_store is not None and len(issues) > 1:
        key = ",".join(str(int(item["number"])) for item in sorted(issues, key=lambda x: int(x["number"])))
        issue_set_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        cached = state_store.get_cached_dag(issue_set_hash)
        if cached:
            order_deps = {int(k): {int(v) for v in vs} for k, vs in (cached.get("order_deps") or {}).items()}
            hard_deps = {int(k): {int(v) for v in vs} for k, vs in (cached.get("hard_deps") or {}).items()}
            return order_deps, hard_deps

    planner_agent_path = resolve_agent_path(factory_dir, "planner")

    def _run_planner(batch: list[dict]) -> dict:
        if not planner_agent_path:
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
            _clear_quota_cooldown_if_needed(state_store, planner_model)
            return out
        except Exception as exc:
            print(f"[factory] warning: queue planner failed, using explicit dependencies only: {exc}", file=sys.stderr)
            return {}

    order_deps, hard_deps, _ = plan_dependency_dag(
        issues,
        planner_fn=_run_planner if len(issues) > 1 else None,
    )
    if state_store is not None and issue_set_hash:
        try:
            state_store.put_cached_dag(
                issue_set_hash,
                {str(k): sorted(v) for k, v in order_deps.items()},
                {str(k): sorted(v) for k, v in hard_deps.items()},
            )
        except Exception:
            pass
    return order_deps, hard_deps


_ORIG_PROCESS_JOB_REF = process_job


def reconcile_ready_jobs(
    repo_root: str,
    cfg: dict | None = None,
    gh: GitHubClient | None = None,
    *,
    state_store: GitStateStore | None = None,
    process_fn=None,
) -> list[dict]:
    """Poll open stage='ready' PRs for merge, CHANGES_REQUESTED fix rounds, or APPROVED/factory:ship."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    if not os.path.isdir(jobs_dir):
        return []
    config = cfg or load_config(factory_dir)
    gh_client = gh or GitHubClient(repo_root)
    runner_fn = process_fn or process_job
    labels_cfg = config.get("labels") or {}
    ship_label = str(labels_cfg.get("ship") or "factory:ship")

    jobs_by_id = {j.get("id"): j for j in list_jobs(jobs_dir) if j.get("id")}
    if state_store is not None:
        try:
            for rj in state_store.list_remote_jobs():
                rid = rj.get("id")
                if rid and rj.get("stage") == "ready" and rid not in jobs_by_id:
                    hydrated = state_store.hydrate_job(rid, os.path.join(jobs_dir, rid))
                    if hydrated:
                        jobs_by_id[rid] = hydrated
        except Exception:
            pass

    results: list[dict] = []
    for job_id in sorted(jobs_by_id):
        job = jobs_by_id[job_id]
        if job.get("stage") != "ready" or not job.get("pr_number"):
            continue
        pr_num = int(job["pr_number"])
        issue_num = int(job["issue"])
        try:
            pr_state = gh_client.fetch_pr_review_state(pr_num)
        except Exception as exc:
            _log(job_id, f"reconcile_ready_jobs warning pr=#{pr_num}: {exc}")
            continue

        if pr_state.get("merged"):
            merged_job = advance_stage(jobs_dir, job_id, "merged")
            _cleanup_terminal_job(repo_root, job_id, merged_job)
            results.append(merged_job)
            continue

        decision = str(pr_state.get("review_decision") or "")
        latest_id = str(pr_state.get("latest_review_id") or "")
        pr_labels = set(pr_state.get("labels") or [])

        if decision == "CHANGES_REQUESTED" and latest_id and latest_id != str(job.get("last_human_review_id") or ""):
            feedback = str(pr_state.get("feedback_md") or "Human reviewer requested changes on PR.")
            update_job(jobs_dir, job_id, last_human_review_id=latest_id)
            advance_stage(
                jobs_dir,
                job_id,
                "build",
                last_error=f"Human PR review requested changes:\n{feedback}",
                expand_round_by=1,
            )
            results.append(runner_fn(repo_root, issue_num, config=config, gh=gh_client))
            continue

        if decision == "APPROVED" or ship_label in pr_labels:
            advance_stage(jobs_dir, job_id, "merging")
            results.append(runner_fn(repo_root, issue_num, config=config, gh=gh_client))
            continue

    return results


def run_factory(
    repo_root: str,
    issue_number: int | None = None,
    issue_numbers: list[int] | None = None,
    *,
    batch_mode: bool | None = None,
    max_batch_issues: int | None = None,
) -> list[dict]:
    """Process specific issue(s) or drain all eligible queued issues using Smart Batch Mode (default) or legacy DAG."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(factory_dir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    pid_path = os.path.join(jobs_dir, ".factory.pid")
    cfg = load_config(factory_dir)
    if max_batch_issues is not None:
        cfg.setdefault("batch", {})["max_batch_issues"] = max(1, int(max_batch_issues))
    gh = GitHubClient(repo_root)
    board = ProjectBoardSync.discover(repo_root)

    explicit_nums: list[int] = []
    if issue_number is not None:
        explicit_nums = [int(issue_number)]
    elif issue_numbers:
        explicit_nums = [int(n) for n in issue_numbers]

    _write_text(pid_path, f"{os.getpid()}\n")
    state_store: GitStateStore | None = None
    if _has_origin_remote(repo_root):
        state_store = GitStateStore(repo_root, _default_machine_id())
    use_batch = bool((cfg.get("batch") or {}).get("enabled", True)) if batch_mode is None else bool(batch_mode)
    try:
        if use_batch:
            if not explicit_nums:
                reconcile_ready_jobs(repo_root, cfg, gh, state_store=state_store)
            issues_to_run = (
                [gh.fetch_issue(num)[0] for num in explicit_nums]
                if explicit_nums
                else gh.list_queue_issues(cfg)
            )
            midflight_nums: list[int] = []
            fresh_issues: list[dict] = []
            for iss in issues_to_run:
                inum = int(iss["number"])
                try:
                    existing = load_job(jobs_dir, f"job-{inum}")
                    if existing.get("stage") not in {"queued"}:
                        midflight_nums.append(inum)
                        continue
                except FileNotFoundError:
                    pass
                fresh_issues.append(iss)

            batch_results: list[dict] = []
            for mnum in midflight_nums:
                batch_results.append(process_job(repo_root, mnum, config=cfg, gh=gh, board=board))
            if fresh_issues:
                batch_results.extend(
                    run_batch_factory(
                        repo_root,
                        fresh_issues,
                        config=cfg,
                        gh=gh,
                        board=board,
                        state_store=state_store,
                    )
                )
            return batch_results

        if len(explicit_nums) == 1:
            num = explicit_nums[0]
            job_id = f"job-{num}"
            jdir = os.path.join(jobs_dir, job_id)
            if state_store is not None:
                initial = _build_initial_lease_stub(jobs_dir, job_id, num, state_store.machine_id)
                ok, _ = state_store.claim_job_lease(job_id, initial)
                if not ok:
                    return []
                try:
                    sig_params = inspect.signature(process_job).parameters
                    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values())
                    call_kwargs: dict = {"config": cfg, "gh": gh, "board": board}
                    if "state_store" in sig_params or accepts_var_kw:
                        call_kwargs["state_store"] = state_store
                    return [process_job(repo_root, num, **call_kwargs)]
                finally:
                    _ensure_lease_released(state_store, jobs_dir, jdir, job_id)
            return [process_job(repo_root, num, config=cfg, gh=gh, board=board)]

        if not explicit_nums:
            reconcile_ready_jobs(repo_root, cfg, gh, state_store=state_store)
        sqa_cfg = cfg.get("staging_qa") or {}
        train_coord: StagingTrainCoordinator | None = None
        if bool(sqa_cfg.get("enabled")):

            def _has_near_merge_peers(queued_ids: set[str]) -> bool:
                for existing in list_jobs(jobs_dir):
                    if existing.get("id") in queued_ids:
                        continue
                    if not _is_pid_alive(existing.get("pid")):
                        continue
                    if existing.get("stage") in {
                        "review",
                        "approve",
                        "merging",
                        "staging_qa",
                    }:
                        return True
                return False

            train_coord = StagingTrainCoordinator(
                max_batch_size=int(sqa_cfg.get("max_batch_size", 4)),
                coalesce_window_sec=float(sqa_cfg.get("coalesce_window_sec", 15)),
                has_near_merge_peers=_has_near_merge_peers,
            )

        _ORIG_PROCESS_JOB = globals().get("_ORIG_PROCESS_JOB_REF") or process_job
        herdr_bridge = HerdrBridge.from_env()
        factory_bin = os.path.join(repo_root, "factory", "bin", "factory")
        factory_cmd = [factory_bin] if os.path.isfile(factory_bin) and os.access(factory_bin, os.X_OK) else [sys.executable, "-m", "factory.cli"]

        def _safe_worker(num: int) -> dict | None:
            job_id = f"job-{num}"
            jdir = os.path.join(jobs_dir, job_id)
            if (
                herdr_bridge.enabled
                and len(explicit_nums) != 1
                and process_job is _ORIG_PROCESS_JOB
                and run_agent is _ORIG_RUN_AGENT
                and (not os.environ.get("PYTEST_CURRENT_TEST") or bool(os.environ.get("HC_BIN_PATH")))
            ):
                tid = herdr_bridge.spawn_job_terminal(
                    job_id,
                    repo_root,
                    [*factory_cmd, "_exec-job", "--repo", repo_root, str(num)],
                    labels={"issue": str(num), "stage": "queued"},
                )
                if tid:
                    herdr_bridge.wait_terminal(tid)
                    try:
                        return load_job(jobs_dir, job_id)
                    except FileNotFoundError:
                        return None
            if state_store is not None:
                initial = _build_initial_lease_stub(jobs_dir, job_id, num, state_store.machine_id)
                ok, _ = state_store.claim_job_lease(job_id, initial)
                if not ok:
                    try:
                        if load_job(jobs_dir, job_id).get("stage") == "queued":
                            shutil.rmtree(jdir, ignore_errors=True)
                    except FileNotFoundError:
                        pass
                    return None
            try:
                sig_params = inspect.signature(process_job).parameters
                accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values())
                call_kwargs: dict = {"config": cfg, "gh": gh, "board": board}
                if train_coord is not None and ("train_coordinator" in sig_params or accepts_var_kw):
                    call_kwargs["train_coordinator"] = train_coord
                if state_store is not None and ("state_store" in sig_params or accepts_var_kw):
                    call_kwargs["state_store"] = state_store
                return process_job(repo_root, num, **call_kwargs)
            except Exception as exc:
                print(f"[issue-{num}] error before job state could escalate: {exc}", file=sys.stderr)
                return None
            finally:
                _ensure_lease_released(state_store, jobs_dir, jdir, job_id)

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
                max_qa_rounds = int((cfg.get("staging_qa") or {}).get("max_qa_rounds", 3))
                runner_pid = os.getpid()
                for item in batch:
                    item_num = int(item["number"])
                    enqueue_job(
                        jobs_dir,
                        item,
                        max_rounds=max_rounds,
                        max_qa_rounds=max_qa_rounds,
                        waiting_on=order_deps.get(item_num),
                        pid=runner_pid,
                    )
                    if board:
                        board.sync_stage(item.get("url", ""), "queued")

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
    live_workers = sum(1 for j in active_jobs if _is_job_live(j))

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
    headers = ("JOB", "STAGE", "ACTIVITY", "MACHINE", "ROUND", "SLICES", "RISK", "TOKENS", "COST", "PR")
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
        lease = j.get("lease") or {}
        machine_str = str(lease.get("hostname") or lease.get("worker_id") or "-")
        rows.append(
            (
                str(j.get("id", "")),
                str(j.get("stage", "")),
                _format_activity(j),
                machine_str,
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

    res_name = subprocess.run(
        ["git", "-C", repo_root, "config", "user.name"],
        capture_output=True,
        text=True,
        check=False,
    )
    user_name = res_name.stdout.strip()
    if res_name.returncode != 0 or not user_name:
        ok = False
        messages.append("MISSING: git config user.name is not set")
    else:
        messages.append(f"OK: git user.name -> {user_name}")

    res_email = subprocess.run(
        ["git", "-C", repo_root, "config", "user.email"],
        capture_output=True,
        text=True,
        check=False,
    )
    user_email = res_email.stdout.strip()
    if res_email.returncode != 0 or not user_email:
        ok = False
        messages.append("MISSING: git config user.email is not set")
    else:
        messages.append(f"OK: git user.email -> {user_email}")

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

    if os.path.isfile(checks_sh):
        try:
            with open(checks_sh, encoding="utf-8", errors="replace") as f:
                checks_content = f.read()
        except Exception:
            checks_content = ""
        if "uv " in checks_content or "uv\n" in checks_content:
            if not shutil.which("uv"):
                ok = False
                messages.append("MISSING: `uv` not found on PATH (required by factory/checks.sh)")
            else:
                messages.append("OK: `uv` found")
        if "pytest" in checks_content:
            try:
                res_pytest = subprocess.run(
                    ["python3", "-m", "pytest", "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res_pytest.returncode != 0:
                    ok = False
                    messages.append(f"MISSING: python3 -m pytest (exit code {res_pytest.returncode})")
                else:
                    ver = res_pytest.stdout.strip() or res_pytest.stderr.strip()
                    detail = f" ({ver})" if ver else ""
                    messages.append(f"OK: python3 -m pytest{detail}")
            except Exception as exc:
                ok = False
                messages.append(f"MISSING: python3 -m pytest ({exc})")

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

    if cfg and (cfg.get("staging_qa") or {}).get("enabled"):
        sqa = cfg["staging_qa"]
        try:
            secrets = load_staging_secrets(sqa.get("env_files") or [], repo_root=repo_root)
            messages.append(
                f"OK: staging_qa enabled (base_url={sqa.get('base_url')}, secrets_loaded={len(secrets)})"
            )
        except Exception as exc:
            ok = False
            messages.append(f"FAIL: staging_qa secret loading failed: {exc}")

        pw_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from playwright.sync_api import sync_playwright; "
                "p = sync_playwright().start(); "
                "b = p.chromium.launch(headless=True); "
                "b.close(); p.stop()",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if pw_probe.returncode != 0:
            ok = False
            detail = (pw_probe.stderr or pw_probe.stdout or "").strip().splitlines()[-1:] or ["failed"]
            messages.append(f"FAIL: playwright headless chromium check failed ({detail[0]})")
        else:
            messages.append("OK: playwright headless chromium")

        wrangler_ok = bool(shutil.which("wrangler"))
        if not wrangler_ok and shutil.which("npx"):
            wr = subprocess.run(
                ["npx", "--no-install", "wrangler", "--version"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            wrangler_ok = wr.returncode == 0
        if wrangler_ok:
            messages.append("OK: wrangler available")
        else:
            ok = False
            messages.append("MISSING: wrangler (install globally or via npm/npx for staging_qa deploy)")

    return ok, messages
