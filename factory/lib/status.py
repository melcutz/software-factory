"""Status formatting and OS-level PID liveness helpers for factory jobs."""
from __future__ import annotations

import os

from factory.lib.state import TERMINAL_OR_READY_STAGES


def _read_text(path: str, default: str = "") -> str:
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return f.read()


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
