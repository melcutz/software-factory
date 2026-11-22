"""Distributed and local coalesced staging deploy train execution and rollback."""
from __future__ import annotations

import concurrent.futures
import contextlib
import inspect
import os
import subprocess
import sys
import time

from factory.lib.git_ops import (
    GitHubClient,
    _run_git,
    abort_merge,
    commits_ahead,
    head_sha,
    is_worktree_clean,
    repo_lock,
    reset_worktree_clean,
)
from factory.lib.git_state import GitStateStore
from factory.lib.schemas import SCHEMAS, render_pr_body, render_qa_failure_md, render_qa_md
from factory.lib.staging_qa import TrainOutcome, TrainTicket
from factory.lib.state import TERMINAL_OR_READY_STAGES, advance_stage, load_job, record_metrics, update_job

_TRAIN_POLL_INTERVAL_SEC = 2.0


def _call_deploy_to_staging(
    deploy_wt: str,
    deploy_env: dict[str, str],
    timeout_sec: int,
    extra_args: list[str],
    *,
    repo_root: str | None = None,
    staging_cfg: dict | None = None,
) -> tuple[bool, str]:
    from factory.lib import pipeline as _p

    sig = inspect.signature(_p.deploy_to_staging)
    kwargs: dict = {}
    if "extra_args" in sig.parameters:
        kwargs["extra_args"] = extra_args
    if "repo_root" in sig.parameters:
        kwargs["repo_root"] = repo_root
    if "staging_cfg" in sig.parameters:
        kwargs["staging_cfg"] = staging_cfg
    if kwargs:
        return _p.deploy_to_staging(deploy_wt, deploy_env, timeout_sec, **kwargs)
    return _p.deploy_to_staging(deploy_wt, deploy_env, timeout_sec)


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
    from factory.lib import pipeline as _p

    qa_fail_file = os.path.join(ticket.jdir, "qa-failure.md")
    if os.path.isfile(qa_fail_file):
        os.remove(qa_fail_file)
    merged_job = advance_stage(jobs_dir, ticket.job_id, "merged")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(jobs_dir)))
    _p._cleanup_terminal_job(repo_root, ticket.job_id, merged_job)
    try:
        gh_client.edit_labels(ticket.issue_number, remove=[labels["queue"]])
        if qa_out is not None:
            gh_client.comment_issue(
                ticket.issue_number,
                f"### Staging QA Passed (`{ticket.job_id}`, QA round {qa_attempt}/{qa_limit})\n\n"
                f"{_p.redact_secrets(render_qa_md(qa_out), secrets)}",
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
    from factory.lib import pipeline as _p

    t_job = load_job(jobs_dir, ticket.job_id)
    _p._write_text(
        os.path.join(qa_dir, "rollback.log"),
        _p.redact_secrets(rb_log, secrets),
    )
    try:
        gh_client.reopen_issue(ticket.issue_number)
    except Exception as exc:
        print(
            f"[{ticket.job_id}] warning: reopen_issue failed: {exc}",
            file=sys.stderr,
        )

    failure_md = _p.redact_secrets(
        render_qa_failure_md(qa_attempt, qa_limit, failure_reason, qa_out),
        secrets,
    )
    _p._write_text(os.path.join(qa_dir, "failure.md"), failure_md)
    _p._write_text(os.path.join(ticket.jdir, "qa-failure.md"), failure_md)

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
        esc_job = _p._escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            f"CRITICAL: Staging QA failed AND post-rollback health check failed on QA round "
            f"{qa_attempt}/{qa_limit}.\n\n{failure_md}\n\n"
            f"Rollback log:\n```\n{_p.redact_secrets(rb_log, secrets)}\n```",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()
        return

    ext_blocker = str((qa_out or {}).get("external_blocker") or "").strip()
    if qa_attempt >= qa_limit or ext_blocker:
        restored_branch: str | None = None
        if os.path.isdir(ticket.worktree):
            try:
                restored_branch = _p.prepare_qa_fix_branch(
                    repo_root,
                    ticket.worktree,
                    ticket.job_id,
                    qa_attempt + 1,
                    base_branch,
                    revert_sha,
                    pre_train_head_sha=ticket.pre_train_head_sha,
                )
            except Exception as exc:
                print(
                    f"[{ticket.job_id}] warning: failed to prepare QA resume branch on escalation: {exc}",
                    file=sys.stderr,
                )
        esc_updates: dict = {
            "qa_round": qa_attempt,
            "pr_number": None,
            "pr_url": None,
            "pre_merge_main_sha": "",
            "merged_commit_sha": "",
            "solo_staging_qa": False,
            "prior_prs": prior_prs,
        }
        if restored_branch:
            esc_updates["branch"] = restored_branch
        update_job(
            jobs_dir,
            ticket.job_id,
            **esc_updates,
        )
        prefix = (
            f"External/infra blocker detected on QA round {qa_attempt}/{qa_limit}: {ext_blocker}."
            if ext_blocker
            else f"Staging QA failed {qa_attempt}/{qa_limit} rounds."
        )
        branch_note = f" Restored changes on `{restored_branch}` for resume." if restored_branch else ""
        esc_job = _p._escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            f"{prefix} "
            f"Reverted `{ticket.merged_sha[:7]}` on `{base_branch}` (`{revert_sha[:7]}`) and rolled back "
            f"staging cleanly.{branch_note}\n\n{failure_md}",
            resume_stage="staging_qa",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()
        return

    new_branch = _p.prepare_qa_fix_branch(
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
    from factory.lib import pipeline as _p

    try:
        gh_client.reopen_issue(ticket.issue_number)
    except Exception as exc:
        print(
            f"[{ticket.job_id}] warning: reopen_issue on innocent requeue failed: {exc}",
            file=sys.stderr,
        )

    if not rb_healthy:
        esc_job = _p._escalate_to_human(
            jobs_dir,
            ticket.job_id,
            ticket.issue_number,
            gh_client,
            cfg,
            "Staging QA passed for this PR, but a sibling PR in the coalesced staging train "
            "failed and post-rollback health check failed.\n\n"
            f"Rollback log:\n```\n{_p.redact_secrets(rb_log, secrets)}\n```",
        )
        ticket.outcome = TrainOutcome(action="return", job=esc_job)
        ticket.done_event.set()
        return

    p_job = load_job(jobs_dir, ticket.job_id)
    prior_prs = list(p_job.get("prior_prs") or [])
    requeue_num = sum(1 for item in prior_prs if item.get("requeued")) + 1
    new_branch = _p.prepare_requeue_branch(
        repo_root,
        ticket.worktree,
        ticket.job_id,
        requeue_num,
        base_branch,
        revert_sha,
        pre_train_head_sha=ticket.pre_train_head_sha,
    )
    _p.push_branch(ticket.worktree, new_branch)
    intake_data = _p._read_json(os.path.join(ticket.jdir, "intake.json"))
    spec_data = _p._read_json(os.path.join(ticket.jdir, "spec.json"))
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
    _p._log(
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
    from factory.lib import pipeline as _p

    try:
        fn()
    except Exception as exc:
        if os.path.isdir(ticket.worktree):
            abort_merge(ticket.worktree)
        esc_job = _p._escalate_to_human(
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
    from factory.lib import pipeline as _p

    if state_store is None:
        yield
        return
    while not state_store.acquire_staging_lock([t.job_id for t in batch]):
        _p.check_halt_or_lease(repo_root, batch[0].job_id, state_store)
        time.sleep(_TRAIN_POLL_INTERVAL_SEC)
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
    from factory.lib import pipeline as _p
    from factory.lib.worker import _is_lease_expired

    staging_cfg = cfg.get("staging_qa") or {}
    base_branch = cfg["base_branch"]
    max_batch_size = max(1, int(staging_cfg.get("max_batch_size", 4)))
    coalesce_window_sec = max(0.0, float(staging_cfg.get("coalesce_window_sec", 15)))

    existing_remote = state_store.get_remote_job(job_id)
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
    _p._checkpoint_if_store(state_store, job_id, jdir, jobs_dir)

    while True:
        _p.check_halt_or_lease(repo_root, job_id, state_store)
        if state_store.acquire_staging_lock([job_id]):
            try:
                if not solo and coalesce_window_sec > 0.0 and max_batch_size > 1:
                    deadline = time.monotonic() + coalesce_window_sec
                    while time.monotonic() < deadline:
                        _p.check_halt_or_lease(repo_root, job_id, state_store)
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
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(_TRAIN_POLL_INTERVAL_SEC, max(0.2, remaining)))

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
                        t_wt, t_branch = _p.ensure_worktree(repo_root, tid, base_branch, branch=t_branch)
                        try:
                            _, t_issue_md = gh_client.fetch_issue(t_issue)
                        except Exception:
                            t_issue_md = _p._read_text(os.path.join(t_jdir, "issue.md"))
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
                    t_saved = load_job(jobs_dir, t.job_id)
                    t_wt_ckpt = (
                        t.worktree
                        if t_saved.get("stage") not in TERMINAL_OR_READY_STAGES
                        and os.path.isdir(t.worktree)
                        and commits_ahead(t.worktree, base_branch) > 0
                        else None
                    )
                    _p._checkpoint_if_store(
                        state_store,
                        t.job_id,
                        t.jdir,
                        jobs_dir,
                        worktree=t_wt_ckpt,
                        release_lease=(t.job_id != job_id),
                    )
                    if t_saved.get("stage") in TERMINAL_OR_READY_STAGES:
                        state_store.delete_slice_checkpoint(t.job_id)
                    state_store.delete_train_ticket(t.job_id)

                if any(t.job_id == job_id for t in batch):
                    updated = load_job(jobs_dir, job_id)
                    if updated["stage"] in {"build", "merging"} or (
                        updated["stage"] == "staging_qa" and updated.get("solo_staging_qa")
                    ):
                        return TrainOutcome(action="continue")
                    return TrainOutcome(action="exit", job=updated)
            finally:
                state_store.release_staging_lock()
        else:
            # Non-leader: wait until leader processes and deletes our ticket, or lock frees up.
            while True:
                _p.check_halt_or_lease(repo_root, job_id, state_store)
                if not state_store._ls_remote_one(f"refs/factory/train/{job_id}"):
                    hydrated = state_store.hydrate_job(job_id, jdir) or load_job(jobs_dir, job_id)
                    if hydrated["stage"] in {"build", "merging"} or (
                        hydrated["stage"] == "staging_qa" and hydrated.get("solo_staging_qa")
                    ):
                        new_br = str(hydrated.get("branch") or f"factory/{job_id}")
                        if os.path.isdir(worktree):
                            _p.ensure_worktree(repo_root, job_id, base_branch, branch=new_br)
                            if not state_store.fetch_slice_checkpoint(job_id, worktree):
                                res_br = subprocess.run(
                                    ["git", "fetch", state_store.remote, f"refs/heads/{new_br}"],
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
                        return TrainOutcome(action="continue")
                    return TrainOutcome(action="exit", job=hydrated)
                if not state_store._ls_remote_one("refs/factory/locks/staging"):
                    break
                time.sleep(_TRAIN_POLL_INTERVAL_SEC)


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
    from factory.lib import pipeline as _p

    staging_cfg = cfg.get("staging_qa") or {}
    labels = cfg["labels"]
    base_branch = cfg["base_branch"]
    max_qa_rounds = int(staging_cfg.get("max_qa_rounds", 3))

    for t in batch:
        _p._set_activity(jobs_dir, t.job_id, "waiting:staging_lock")

    dist_ctx = (
        _distributed_staging_lock(state_store, repo_root, batch)
        if manage_distributed_lock
        else contextlib.nullcontext()
    )
    with dist_ctx, _p.staging_lock(jobs_dir):
        secrets = _p.load_staging_secrets(
            staging_cfg.get("env_files") or [],
            repo_root=repo_root,
            test_mode_secret_keys=staging_cfg.get("test_mode_secret_keys"),
        )
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
                    _p._log(
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
            _p._set_activity(jobs_dir, t.job_id, "waiting:repo_lock")
            with repo_lock(repo_root):
                _p._set_activity(jobs_dir, t.job_id, "running:merging")
                sync_ok, sync_err = _p._sync_pr_with_base(
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
                    esc_job = _p._escalate_to_human(
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
                _p._log(
                    t.job_id,
                    f"stage=staging_qa qa_round={t_job.get('qa_round', 0) + 1}/{t_job.get('qa_round_limit', max_qa_rounds)}",
                )
                t.pre_merge_sha = pre_merge_sha
                t.merged_sha = merged_sha
                merged_tickets.append(t)

        if not merged_tickets:
            return

        deploy_wt, train_head_sha = _p.ensure_staging_deploy_worktree(repo_root, base_branch)
        for t in merged_tickets:
            if not t.merged_sha:
                t.merged_sha = train_head_sha
                update_job(jobs_dir, t.job_id, merged_commit_sha=t.merged_sha)

        changed_files = _compute_train_changed_files(
            deploy_wt, train_base_sha, train_head_sha, merged_tickets
        )
        deploy_plan = _p.plan_staging_deploy(changed_files, repo_root=repo_root)

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
                _p._write_text(
                    os.path.join(qa_dir, "deploy.log"),
                    "Skipped staging deploy and browser QA: non-runtime changes only.\n",
                )
                _p._log(t.job_id, "staging_qa skipped (non-runtime changes only)")
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

        deploy_env = _p.build_deploy_env(secrets, staging_cfg, repo_root=repo_root)
        qa_env = _p.build_qa_agent_env(secrets, staging_cfg, repo_root=repo_root)
        base_url = staging_cfg.get("base_url", "")
        ops_url = staging_cfg.get("ops_base_url", "")
        deploy_timeout_sec = int(staging_cfg.get("deploy_timeout_sec", 900))
        qa_timeout_sec = int(staging_cfg.get("qa_timeout_sec", 1200))

        snapshot = _p.snapshot_staging_versions(deploy_wt, deploy_env, repo_root=repo_root)

        flags_label = " ".join(deploy_plan.extra_args) or "(full)"
        for t in merged_tickets:
            _p._set_activity(jobs_dir, t.job_id, "running:deploy")
            _p._log(
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

        redacted_deploy_log = _p.redact_secrets(deploy_log, secrets)
        for t in merged_tickets:
            _, _, qa_dir = meta[t.job_id]
            _p._log(
                t.job_id,
                f"staging_qa deploy={'OK' if deploy_ok else 'FAIL'} (dur={_p._fmt_duration(dep_dur)})",
            )
            _p._write_text(os.path.join(qa_dir, "deploy.log"), redacted_deploy_log)

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
                _p.ensure_staging_qa_accounts(deploy_wt, deploy_env, repo_root=repo_root)
            except Exception as exc:
                seed_err = (
                    f"Staging QA account seed failed ({type(exc).__name__}): "
                    f"{_p.redact_secrets(str(exc), secrets)}"
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
                        _p._set_activity(jobs_dir, t.job_id, f"running:browser-qa-r{qa_attempt}")
                        _p._log(
                            t.job_id,
                            f"staging_qa browser_qa=start round={qa_attempt}/{qa_limit} ({_p._fmt_model_tag(qa_model)})",
                        )
                        spec_md = _p._read_text(os.path.join(t.jdir, "spec.md"))
                        t_job = _p._read_json(os.path.join(t.jdir, "job.json"))
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
                        qa_out, usage, dur = _p._call_run_agent(
                            factory_dir,
                            t.jdir,
                            deploy_wt,
                            "qa",
                            "qa",
                            qa_model,
                            qa_prompt,
                            SCHEMAS["qa"],
                            qa_timeout_sec,
                            config=cfg,
                            state_store=state_store,
                            job_id=t.job_id,
                            sandbox=False,
                            clean_worktree=False,
                            log_name=f"qa-{qa_attempt}",
                            extra_env=qa_env,
                            protect_head=True,
                        )
                        used_qa_model = (usage or {}).get("model") or qa_model
                        _p._clear_quota_cooldown_if_needed(state_store, used_qa_model)
                        metrics_str = _p._record_and_format_step(
                            jobs_dir, t.job_id, used_qa_model, usage, dur, cfg
                        )
                        _p._log(
                            t.job_id,
                            f"staging_qa browser_qa=done verdict={qa_out['verdict']} ({metrics_str})",
                        )
                        _p._write_json(os.path.join(qa_dir, "qa.json"), qa_out)
                        _p._write_text(
                            os.path.join(qa_dir, "qa.md"),
                            _p.redact_secrets(render_qa_md(qa_out), secrets),
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
                            f"{_p.redact_secrets(str(exc), secrets)}",
                        )
                    finally:
                        for suffix in ("", "-retry"):
                            log_p = os.path.join(t.jdir, "logs", f"qa-{qa_attempt}{suffix}.json")
                            if os.path.isfile(log_p):
                                _p._write_text(log_p, _p.redact_secrets(_p._read_text(log_p), secrets))

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
            ok_rev, revert_map, _ = _p.try_revert_commits_on_base(deploy_wt, base_branch, rev_shas)
            if not ok_rev:
                revert_map = {
                    t.merged_sha: _p.revert_commit_on_base(deploy_wt, base_branch, t.merged_sha)
                    for t in reversed(merged_tickets)
                }
            rb_healthy, rb_log = _p.rollback_staging(
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
        surgical_ok, culprit_revert_map, conflict_detail = _p.try_revert_commits_on_base(
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
            rem_plan = _p.plan_staging_deploy(rem_changed, repo_root=repo_root)
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

            ok_rem, innocent_revert_map, _ = _p.try_revert_commits_on_base(
                deploy_wt,
                base_branch,
                [t.merged_sha for t in reversed(passed_tickets)],
            )
            if not ok_rem:
                innocent_revert_map = {
                    t.merged_sha: _p.revert_commit_on_base(deploy_wt, base_branch, t.merged_sha)
                    for t in reversed(passed_tickets)
                }
            full_revert_map = {**culprit_revert_map, **innocent_revert_map}
        else:
            first_line = conflict_detail.splitlines()[0] if conflict_detail else "conflict"
            _p._log(
                failed_tickets[0].job_id,
                f"surgical revert conflicted ({first_line}); reverting full train",
            )
            ok_all, full_revert_map, _ = _p.try_revert_commits_on_base(
                deploy_wt,
                base_branch,
                [t.merged_sha for t in reversed(merged_tickets)],
            )
            if not ok_all:
                full_revert_map = {
                    t.merged_sha: _p.revert_commit_on_base(deploy_wt, base_branch, t.merged_sha)
                    for t in reversed(merged_tickets)
                }

        rb_healthy, rb_log = _p.rollback_staging(
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
