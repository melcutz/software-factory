"""End-to-end autonomous software factory pipeline orchestrator and doctor checks."""
from __future__ import annotations

import concurrent.futures
import contextlib
import graphlib
import hashlib
import inspect
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

import factory.lib.governor as _governor
from factory.lib.agy_runner import (
    AgentExecutionError,
    QuotaExhaustedError,
    classify_model_provider,
    resolve_agent_path,
    resolve_agy_bin,
    run_agent,
    run_agent_chain,
)
from factory.lib.batch import run_batch_factory

_ORIG_RUN_AGENT = run_agent
from factory.lib.config import (
    estimate_usage_cost_usd,
    expand_path_prepend,
    load_config,
    load_config_with_layers,
    model_chain,
    resolve_model_tier,
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
from factory.lib.doctor import run_doctor
from factory.lib.staging_train import (
    _call_deploy_to_staging,
    _compute_train_changed_files,
    _distributed_staging_lock,
    _execute_staging_train_batch,
    _handle_qa_failure_for_ticket,
    _mark_ticket_merged,
    _requeue_innocent_ticket,
    _run_distributed_staging_train,
    _safe_finalize_train_ticket,
)
from factory.lib.status import (
    _format_activity,
    _is_job_live,
    _is_pid_alive,
    factory_runner_status,
    format_status_table,
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


def _fmt_model_tag(model_spec: str | list[str]) -> str:
    if isinstance(model_spec, list):
        first = str(model_spec[0]).strip() if model_spec else ""
        base_model, effort = split_model_and_effort(first)
        if len(model_spec) > 1:
            return f"model={base_model} effort={effort} chain={len(model_spec)}"
        return f"model={base_model} effort={effort}"
    base_model, effort = split_model_and_effort(model_spec)
    return f"model={base_model} effort={effort}"


def _clear_quota_cooldown_if_needed(
    state_store: GitStateStore | None,
    model_spec: str | list[str],
    config: dict | None = None,
) -> None:
    if state_store is None:
        return
    if isinstance(model_spec, list):
        raw_spec = str(model_spec[0]).strip() if model_spec else ""
    else:
        raw_spec = (model_spec or "").strip()
    if not raw_spec:
        return
    bucket = _governor.quota_bucket_for_model(raw_spec, config=config)
    if not bucket:
        return
    models = state_store.get_quota_state().get("models")
    if isinstance(models, dict) and (bucket in models or raw_spec in models):
        state_store.clear_quota_cooldown(bucket, config=config)


def _record_and_format_step(
    jobs_dir: str,
    job_id: str,
    model_spec: str | list[str],
    usage: dict,
    duration_sec: float,
    config: dict,
) -> str:
    u = dict(usage) if isinstance(usage, dict) else {}
    if isinstance(model_spec, list):
        default_spec = str(model_spec[0]).strip() if model_spec else ""
    else:
        default_spec = (model_spec or "").strip()
    effective_spec = str(u.get("model") or default_spec).strip()
    if effective_spec:
        prov, _ = classify_model_provider(effective_spec)
        tier = resolve_model_tier(effective_spec, config)
        u.setdefault("provider", prov)
        u.setdefault("tier", tier)
        u.setdefault("model", effective_spec)
    step_cost = estimate_usage_cost_usd(effective_spec, u, config)
    updated_job = record_usage(jobs_dir, job_id, u, duration_sec, cost_usd=step_cost)
    in_tok = int(u.get("input_tokens", 0) or 0)
    cache_tok = int(u.get("cache_read_tokens", 0) or 0)
    out_tok = int(u.get("output_tokens", 0) or 0)
    think_tok = int(u.get("thinking_tokens", 0) or 0)
    tot_tok = int(u.get("total_tokens", 0) or (in_tok + out_tok))
    parts = [
        _fmt_model_tag(effective_spec),
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
                resolve_out, usage, dur = _call_run_agent(
                    factory_dir,
                    jdir,
                    worktree,
                    "build",
                    "builder",
                    builder_model,
                    conflict_prompt,
                    SCHEMAS["build"],
                    timeout_sec,
                    config=cfg,
                    state_store=state_store,
                    job_id=job_id,
                    sandbox=False,
                    log_name=f"r{round_num}-merge-conflict",
                )
            except Exception as exc:
                abort_merge(worktree)
                return (
                    False,
                    f"Merge conflict resolution failed while syncing `{branch}` with `origin/{base_branch}`: {exc}",
                )

            used_model = (usage or {}).get("model") or builder_model
            _clear_quota_cooldown_if_needed(state_store, used_model)
            metrics_str = _record_and_format_step(jobs_dir, job_id, used_model, usage, dur, cfg)
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


def _call_single_run_agent(*args, abort_check=None, gpu_context=None, **kwargs):
    """Invoke `run_agent`, passing `abort_check` and `gpu_context` when accepted by the callable."""
    try:
        sig = inspect.signature(run_agent)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if "abort_check" in sig.parameters or has_varkw:
            kwargs["abort_check"] = abort_check
        if gpu_context is not None and ("gpu_context" in sig.parameters or has_varkw):
            kwargs["gpu_context"] = gpu_context
    except (ValueError, TypeError):
        pass
    return run_agent(*args, **kwargs)


def _call_run_agent(
    factory_dir: str,
    job_dir: str,
    worktree: str,
    stage: str,
    agent_name: str,
    models: str | list[str],
    task_prompt: str,
    schema: dict,
    timeout_sec: int,
    *,
    config: dict | None = None,
    state_store: GitStateStore | None = None,
    job_id: str = "",
    abort_check=None,
    gpu_context: dict | None = None,
    **kwargs,
):
    """Invoke `run_agent_chain` across one or more models in `models`, using `pipeline.run_agent` as the runner."""
    if gpu_context is not None:
        kwargs["gpu_context"] = gpu_context
    return run_agent_chain(
        factory_dir,
        job_dir,
        worktree,
        stage,
        agent_name,
        models,
        task_prompt,
        schema,
        timeout_sec,
        config=config,
        state_store=state_store,
        runner_fn=_call_single_run_agent,
        log_fn=(lambda msg: _log(job_id, msg)) if job_id else None,
        abort_check=abort_check,
        **kwargs,
    )


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
) -> str | list[str]:
    """Select fast builder_low on clean low-risk attempt 0, otherwise escalate to models['builder']."""
    models = cfg.get("models") or {}
    low_model = models.get("builder_low")
    has_low = (isinstance(low_model, str) and bool(low_model.strip())) or (
        isinstance(low_model, list) and any(isinstance(s, str) and s.strip() for s in low_model)
    )
    if (
        has_low
        and (job.get("risk") or "") == "low"
        and attempt == 0
        and not is_fix_round
        and not is_conflict
    ):
        return low_model.strip() if isinstance(low_model, str) else [str(s).strip() for s in low_model if str(s).strip()]
    b_val = models["builder"]
    return b_val.strip() if isinstance(b_val, str) else [str(s).strip() for s in b_val if str(s).strip()]


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
                    config=cfg,
                    state_store=state_store,
                    job_id=job_id,
                    sandbox=True,
                    log_name="intake",
                    abort_check=abort_check,
                )
                used_intake_model = (usage or {}).get("model") or intake_model
                _clear_quota_cooldown_if_needed(state_store, used_intake_model)
                metrics_str = _record_and_format_step(jobs_dir, job_id, used_intake_model, usage, dur, cfg)
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
                    config=cfg,
                    state_store=state_store,
                    job_id=job_id,
                    sandbox=True,
                    log_name="spec",
                    abort_check=abort_check,
                )
                used_spec_model = (usage or {}).get("model") or spec_model
                _clear_quota_cooldown_if_needed(state_store, used_spec_model)
                metrics_str = _record_and_format_step(jobs_dir, job_id, used_spec_model, usage, dur, cfg)
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
                        config=cfg,
                        state_store=state_store,
                        job_id=job_id,
                        sandbox=True,
                        log_name="spec-retry",
                        abort_check=abort_check,
                    )
                    used_retry_spec_model = (usage or {}).get("model") or spec_model
                    _clear_quota_cooldown_if_needed(state_store, used_retry_spec_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, used_retry_spec_model, usage, dur, cfg)
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
                                config=cfg,
                                state_store=state_store,
                                job_id=job_id,
                                sandbox=False,
                                log_name=f"r{round_num}-slice-{slice_num}-a{attempt + 1}",
                                abort_check=abort_check,
                            )
                            used_builder_model = (usage or {}).get("model") or builder_model
                            _clear_quota_cooldown_if_needed(state_store, used_builder_model)
                            metrics_str = _record_and_format_step(jobs_dir, job_id, used_builder_model, usage, dur, cfg)
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
                    if qa_failure_md.strip():
                        prompt_parts.extend(
                            [
                                "",
                                "## Staging QA Failure (Must Fix)",
                                qa_failure_md,
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
                        config=cfg,
                        state_store=state_store,
                        job_id=job_id,
                        sandbox=False,
                        log_name=f"r{round_num}-fix-build",
                        abort_check=abort_check,
                    )
                    used_fix_model = (usage or {}).get("model") or builder_model
                    _clear_quota_cooldown_if_needed(state_store, used_fix_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, used_fix_model, usage, dur, cfg)
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
                        config=cfg,
                        state_store=state_store,
                        job_id=job_id,
                        sandbox=False,
                        log_name=f"r{round_num}-gate-fix-{attempt + 1}",
                        abort_check=abort_check,
                    )
                    used_gate_fix_model = (usage or {}).get("model") or builder_model
                    _clear_quota_cooldown_if_needed(state_store, used_gate_fix_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, used_gate_fix_model, usage, dur, cfg)
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

                def _run_single_reviewer(item: tuple[str, str | list[str]]) -> tuple[str, dict]:
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
                        config=cfg,
                        state_store=state_store,
                        job_id=job_id,
                        sandbox=True,
                        clean_worktree=False,
                        log_name=f"r{round_num}-review-{rev_name}",
                        abort_check=_make_abort_check(repo_root, job_id, jobs_dir, state_store),
                    )
                    used_rev_model = (u or {}).get("model") or rev_model
                    _clear_quota_cooldown_if_needed(state_store, used_rev_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, used_rev_model, u, d, cfg)
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
                    expected_reviewers = resolve_reviewers(cfg, job.get("risk", "low"), job.get("surfaces") or [])
                    all_reviews_passed = bool(expected_reviewers) and all(
                        isinstance(reviews_dict.get(r), dict)
                        and reviews_dict[r].get("verdict") == "PASS"
                        and not any(
                            isinstance(f, dict) and f.get("severity") in {"must_fix", "should_fix"}
                            for f in (reviews_dict[r].get("findings") or ())
                        )
                        for r in expected_reviewers
                    )
                    approve_gpu_context = {
                        "risk": job.get("risk") or (intake_data.get("risk") if isinstance(intake_data, dict) else None) or "",
                        "gate_passed": (checks_md or "").startswith("VERDICT: PASS") and not (qa_failure_md or "").strip(),
                        "all_reviews_passed": all_reviews_passed,
                        "prior_escalation": bool(job.get("last_error")),
                        "round": round_num,
                    }
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
                        config=cfg,
                        state_store=state_store,
                        job_id=job_id,
                        sandbox=True,
                        log_name=f"r{round_num}-approve",
                        abort_check=abort_check,
                        gpu_context=approve_gpu_context,
                    )
                    used_approve_model = (usage or {}).get("model") or approver_model
                    _clear_quota_cooldown_if_needed(state_store, used_approve_model)
                    metrics_str = _record_and_format_step(jobs_dir, job_id, used_approve_model, usage, dur, cfg)
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
            bucket = exc.bucket or _governor.quota_bucket_for_model(exc.model_spec, config=cfg)
            state_store.record_quota_cooldown(
                bucket or exc.model_spec,
                str(exc),
                exc.retry_after_sec,
                config=cfg,
            )
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
            out, u, _ = _call_run_agent(
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
                config=cfg,
                state_store=state_store,
                job_id="_planner",
            )
            used_planner_model = (u or {}).get("model") or planner_model
            _clear_quota_cooldown_if_needed(state_store, used_planner_model)
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
    spawn_fn=None,
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

    def _invoke_runner(issue_num: int, job_id: str, jdir: str) -> dict | None:
        if spawn_fn is not None:
            spawn_fn(repo_root, issue_num)
            return load_job(jobs_dir, job_id)
        try:
            sig_params = inspect.signature(runner_fn).parameters
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values())
            call_kwargs: dict = {"config": config, "gh": gh_client}
            if state_store is not None and ("state_store" in sig_params or accepts_var_kw):
                call_kwargs["state_store"] = state_store
            return runner_fn(repo_root, issue_num, **call_kwargs)
        finally:
            if state_store is not None:
                _ensure_lease_released(state_store, jobs_dir, jdir, job_id)

    results: list[dict] = []
    for job_id in sorted(jobs_by_id):
        job = jobs_by_id[job_id]
        if job.get("stage") != "ready" or not job.get("pr_number"):
            continue
        pr_num = int(job["pr_number"])
        issue_num = int(job["issue"])
        jdir = os.path.join(jobs_dir, job_id)
        try:
            pr_state = gh_client.fetch_pr_review_state(pr_num)
        except Exception as exc:
            _log(job_id, f"reconcile_ready_jobs warning pr=#{pr_num}: {exc}")
            continue

        if pr_state.get("merged"):
            merged_job = advance_stage(jobs_dir, job_id, "merged")
            _cleanup_terminal_job(repo_root, job_id, merged_job)
            if state_store is not None:
                try:
                    _checkpoint_if_store(state_store, job_id, jdir, jobs_dir, release_lease=True)
                except Exception:
                    pass
            results.append(merged_job)
            continue

        decision = str(pr_state.get("review_decision") or "")
        latest_id = str(pr_state.get("latest_review_id") or "")
        pr_labels = set(pr_state.get("labels") or [])

        if decision == "CHANGES_REQUESTED" and latest_id and latest_id != str(job.get("last_human_review_id") or ""):
            if state_store is not None:
                ok_claim, _ = state_store.claim_job_lease(job_id, job)
                if not ok_claim:
                    continue
            feedback = str(pr_state.get("feedback_md") or "Human reviewer requested changes on PR.")
            update_job(jobs_dir, job_id, last_human_review_id=latest_id)
            advance_stage(
                jobs_dir,
                job_id,
                "build",
                last_error=f"Human PR review requested changes:\n{feedback}",
                expand_round_by=1,
            )
            if state_store is not None:
                _checkpoint_if_store(state_store, job_id, jdir, jobs_dir)
            res = _invoke_runner(issue_num, job_id, jdir)
            if res is not None:
                results.append(res)
            continue

        if decision == "APPROVED" or ship_label in pr_labels:
            if state_store is not None:
                ok_claim, _ = state_store.claim_job_lease(job_id, job)
                if not ok_claim:
                    continue
            advance_stage(jobs_dir, job_id, "merging")
            if state_store is not None:
                _checkpoint_if_store(state_store, job_id, jdir, jobs_dir)
            res = _invoke_runner(issue_num, job_id, jdir)
            if res is not None:
                results.append(res)
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
    sqa_enabled = bool((cfg.get("staging_qa") or {}).get("enabled"))
    use_batch = (
        (bool((cfg.get("batch") or {}).get("enabled", True)) if batch_mode is None else bool(batch_mode))
        and not sqa_enabled
    )
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

