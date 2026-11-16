"""Central CLI entrypoint for the Autonomous Software Factory."""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import statistics
import subprocess
import sys

from factory.lib.batch import format_batch_plan, triage_and_plan_queue
from factory.lib.config import load_config
from factory.lib.fleet import fleet_pause, fleet_remove, fleet_status, fleet_unpause, fleet_update, fleet_wake
from factory.lib.gate_runner import run_gate
from factory.lib.git_ops import GitHubClient, _run_git, remove_worktree
from factory.lib.onboarding import generate_repo_map, init_repository, learn_from_jobs
from factory.lib.patrol import run_patrol
from factory.lib.git_state import GitStateStore
from factory.lib.pipeline import (
    factory_runner_status,
    format_status_table,
    run_doctor,
    run_factory,
    run_halt,
    run_resume,
)
from factory.lib.project_board import create_factory_board
from factory.lib.state import list_jobs, load_job
from factory.lib.worker import exec_job_command, run_worker, set_host_paused

DEFAULT_REPOS_CONFIG = os.path.join("~", ".config", "factory", "repos.json")


def _repos_config_path() -> str:
    return os.path.expanduser(os.environ.get("FACTORY_REPOS_CONFIG") or DEFAULT_REPOS_CONFIG)


def _detect_git_root(cwd: str) -> str:
    res = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        return os.path.abspath(res.stdout.strip())
    return os.path.abspath(cwd)


def load_registered_repos(config_path: str | None = None) -> list[str]:
    """Load target repository paths from `~/.config/factory/repos.json`."""
    path = os.path.expanduser(config_path or _repos_config_path())
    if not os.path.isfile(path):
        raise SystemExit(
            f"No repos registry found at {path}. Create it as a JSON list of repo paths "
            f"(e.g. [\"~/work/repo-a\", \"~/work/repo-b\"]) or pass --repo <path>."
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("repos", []) if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"Repos registry at {path} must be a non-empty JSON list of repository paths.")
    repos = [os.path.abspath(os.path.expanduser(str(item))) for item in raw if str(item).strip()]
    missing = [r for r in repos if not os.path.isdir(r)]
    if missing:
        raise SystemExit(f"Registered repo path(s) do not exist: {', '.join(missing)}")
    return repos


def resolve_target_repos(args: argparse.Namespace) -> list[str]:
    """Resolve target repository roots from `--all`, `--repo`, or current git working tree."""
    if getattr(args, "all", False):
        return load_registered_repos()
    repo_args = getattr(args, "repo", None) or []
    if repo_args:
        repos = [os.path.abspath(os.path.expanduser(p)) for p in repo_args]
        missing = [r for r in repos if not os.path.isdir(r)]
        if missing:
            raise SystemExit(f"Repository path(s) do not exist: {', '.join(missing)}")
        return repos
    return [_detect_git_root(os.getcwd())]


def _build_parser() -> argparse.ArgumentParser:
    repo_flags = argparse.ArgumentParser(add_help=False)
    repo_flags.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Target repository path (repeatable; defaults to current git repo root)",
    )
    repo_flags.add_argument(
        "--all",
        action="store_true",
        help="Run against all repositories listed in ~/.config/factory/repos.json",
    )

    parser = argparse.ArgumentParser(
        prog="factory",
        description="Autonomous Software Factory for Google Antigravity (agy)",
        parents=[repo_flags],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run",
        parents=[repo_flags],
        help="Drain queued factory issues or process specific issue number(s)",
    )
    p_run.add_argument("issues", nargs="*", type=int, help="Optional GitHub issue numbers (default: drain all queued)")

    sub.add_parser("queue", parents=[repo_flags], help="List open eligible factory-labeled GitHub issues")

    p_plan = sub.add_parser(
        "plan",
        parents=[repo_flags],
        help="Dry-run smart batch triage and print the planned parallel/sequential Batch-DAG",
    )
    p_plan.add_argument("issues", nargs="*", type=int, help="Optional GitHub issue numbers (default: all queued)")

    p_status = sub.add_parser(
        "status",
        parents=[repo_flags],
        help="Show job status summary or detailed JSON for one job",
    )
    p_status.add_argument("job_id", nargs="?", help="Optional job id (e.g. job-42)")

    p_gate = sub.add_parser(
        "gate",
        parents=[repo_flags],
        help="Run protected_paths + factory/checks.sh for a job worktree",
    )
    p_gate.add_argument("job_id", help="Job id (e.g. job-42)")
    p_gate.add_argument("worktree", nargs="?", help="Worktree path (defaults to .worktrees/<job_id>)")
    p_gate.add_argument("round", nargs="?", type=int, default=1, help="Round number (default: 1)")

    sub.add_parser("doctor", parents=[repo_flags], help="Verify git, gh, agy, config.json, checks.sh, and .gitignore")

    p_board = sub.add_parser("init-board", parents=[repo_flags], help="Create a generic 'Factory' GitHub Project board")
    p_board.add_argument("--owner", default="@me", help="GitHub owner login (default: @me)")
    p_board.add_argument("--title", default="Factory", help="Project title (default: Factory)")

    p_halt = sub.add_parser("halt", parents=[repo_flags], help="Globally halt the factory fleet and checkpoint active jobs")
    p_halt.add_argument("--reason", default="", help="Optional reason recorded in refs/factory/control")
    p_halt.add_argument("--force", action="store_true", help="Interrupt immediately even during staging deploy")

    sub.add_parser("resume", parents=[repo_flags], help="Clear global halt and allow workers to resume")

    p_worker = sub.add_parser("worker", parents=[repo_flags], help="Run continuous competing-consumer worker loop")
    p_worker.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")

    p_exec = sub.add_parser("_exec-job", parents=[repo_flags], help="Internal: execute a single claimed job")
    p_exec.add_argument("issue", type=int, help="GitHub issue number")

    p_pause = sub.add_parser("pause", parents=[repo_flags], help="Pause new job claims on this machine")
    p_pause.add_argument("--drain", action="store_true", help="Drain active jobs cleanly")

    sub.add_parser("unpause", parents=[repo_flags], help="Resume job claims on this machine")

    p_fleet = sub.add_parser("fleet", parents=[repo_flags], help="Manage peer machines via hc machine")
    p_fleet.add_argument("action", choices=["status", "wake", "pause", "unpause", "update", "remove"], help="Fleet action")
    p_fleet.add_argument("machine", nargs="?", help="Optional target machine name")
    p_fleet.add_argument("--drain", action="store_true", help="Drain active jobs when pausing")

    p_clean = sub.add_parser(
        "clean",
        parents=[repo_flags],
        help="Remove worktrees for merged/rejected jobs and prune stale git worktrees",
    )
    p_clean.add_argument("--dry-run", action="store_true", help="Show worktrees that would be removed without deleting them")

    p_map = sub.add_parser(
        "map",
        parents=[repo_flags],
        help="Generate factory/REPO_MAP.md from tracked repository layout",
    )
    p_map.add_argument("--stdout", action="store_true", help="Print generated map to stdout instead of writing file")

    p_init = sub.add_parser(
        "init",
        parents=[repo_flags],
        help="Scaffold factory/config.json, checks.sh, REPO_MAP.md, and .gitignore",
    )
    p_init.add_argument("--force", action="store_true", help="Overwrite existing factory files")

    p_learn = sub.add_parser(
        "learn",
        parents=[repo_flags],
        help="Distill guardrails from multi-round/QA jobs into factory/PITFALLS.md",
    )
    p_learn.add_argument("job_id", nargs="?", help="Optional specific job ID (e.g. job-12)")
    p_learn.add_argument("--deterministic", action="store_true", help="Use deterministic extraction without LLM")

    p_stats = sub.add_parser("stats", parents=[repo_flags], help="Show telemetry summary across factory jobs")
    p_stats.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_logs = sub.add_parser("logs", parents=[repo_flags], help="Show failure/gate/review summary for a job")
    p_logs.add_argument("job_id", help="Job ID (e.g. job-12)")
    p_logs.add_argument("--round", type=int, default=None, help="Specific round number")
    p_logs.add_argument("--tail", type=int, default=40, help="Number of trailing lines from checks.md")

    p_patrol = sub.add_parser("patrol", parents=[repo_flags], help="Run repo health sensors and file factory issues")
    p_patrol.add_argument("--dry-run", action="store_true", help="Print findings without creating GitHub issues")

    return parser


def _format_fleet_result(name: str, rc: int, out: str) -> str:
    text = (out or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if "error" in data and isinstance(data["error"], dict):
                    err = data["error"]
                    msg = str(err.get("message") or "command failed")
                    hint = str(err.get("hint") or "")
                    suffix = f" ({hint})" if hint else ""
                    return f"{name}: FAILED - {msg}{suffix}"
                if "address" in data:
                    state = str(data.get("state") or "live")
                    return f"{name}: started ({data['address']}, {state})"
        except ValueError:
            pass
    if rc == 0:
        return f"{name}: {text or 'ok'}"
    return f"{name}: FAILED (rc={rc}) {text}".strip()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-board":
        res = create_factory_board(owner=args.owner, title=args.title, cwd=os.getcwd())
        proj = res["project"]
        verb = "Created" if res["created"] else "Already exists"
        print(f"{verb}: {proj.get('title', args.title)} ({proj.get('url', '')})")
        return 0

    if args.command == "pause":
        set_host_paused(True, drain=args.drain)
        print("Paused new job claims on this machine.")
        return 0

    if args.command == "unpause":
        set_host_paused(False)
        print("Unpaused job claims on this machine.")
        return 0

    if args.command == "fleet":
        if args.action == "status":
            machines = fleet_status()
            headers = ("NAME", "HOST", "STATE", "VERSION", "VIA")
            rows = [headers]
            for m in machines:
                rows.append(
                    (
                        str(m.get("name") or "-"),
                        str(m.get("host") or m.get("target") or "-"),
                        str(m.get("state") or ("paused" if m.get("paused") else "-")),
                        str(m.get("version") or "-"),
                        str(m.get("via") or "-"),
                    )
                )
            widths = [max(len(r[c]) for r in rows) for c in range(len(headers))]
            for idx, r in enumerate(rows):
                print("  ".join(r[c].ljust(widths[c]) for c in range(len(headers))).rstrip())
                if idx == 0:
                    print("  ".join("-" * w for w in widths))
            return 0
        if args.action == "wake":
            for name, rc, out in fleet_wake():
                print(_format_fleet_result(name, rc, out))
            return 0
        if args.action == "pause":
            for name, rc, out in fleet_pause(args.machine, drain=args.drain):
                print(_format_fleet_result(name, rc, out))
            return 0
        if args.action == "unpause":
            for name, rc, out in fleet_unpause(args.machine):
                print(_format_fleet_result(name, rc, out))
            return 0
        if args.action == "update":
            for name, rc, out in fleet_update(args.machine):
                print(_format_fleet_result(name, rc, out))
            return 0
        if args.action == "remove":
            if not args.machine:
                print("Error: 'factory fleet remove <machine>' requires a machine name.", file=sys.stderr)
                return 1
            rc, out = fleet_remove(args.machine)
            print(_format_fleet_result(args.machine, rc, out))
            return rc
        return 1

    repos = resolve_target_repos(args)
    multi = len(repos) > 1

    if args.command == "run":
        if multi and args.issues:
            print("Error: explicit issue numbers cannot be combined with multiple repos.", file=sys.stderr)
            return 1
        if not multi:
            jobs = run_factory(repos[0], issue_numbers=args.issues or None)
            print(format_status_table(jobs))
            return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(repos)) as pool:
            futs = {pool.submit(run_factory, repo): repo for repo in repos}
            for fut, repo in futs.items():
                jobs = fut.result()
                print(f"\n=== {os.path.basename(repo)} ({repo}) ===")
                print(format_status_table(jobs))
        return 0

    if args.command == "queue":
        for repo in repos:
            cfg = load_config(os.path.join(repo, "factory"))
            gh = GitHubClient(repo)
            prefix = f"{os.path.basename(repo)}\t" if multi else ""
            for iss in gh.list_queue_issues(cfg):
                print(f"{prefix}{iss['number']}\t{iss['title']}\t{iss['url']}")
        return 0

    if args.command == "plan":
        if multi and args.issues:
            print("Error: explicit issue numbers cannot be combined with multiple repos.", file=sys.stderr)
            return 1
        for idx, repo in enumerate(repos):
            cfg = load_config(os.path.join(repo, "factory"))
            gh = GitHubClient(repo)
            if args.issues:
                issues = [gh.fetch_issue(int(n))[0] for n in args.issues]
            else:
                issues = gh.list_queue_issues(cfg)
            plan = triage_and_plan_queue(repo, issues, config=cfg, gh=gh, dry_run=True)
            if multi:
                if idx > 0:
                    print()
                print(f"=== {os.path.basename(repo)} ({repo}) ===")
            print(format_batch_plan(plan))
        return 0

    if args.command == "status":
        if args.job_id:
            if multi:
                print("Error: job_id lookup requires a single repository.", file=sys.stderr)
                return 1
            jobs_dir = os.path.join(repos[0], "factory", "jobs")
            try:
                job = load_job(jobs_dir, args.job_id)
            except FileNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(job, indent=2))
            return 0
        for idx, repo in enumerate(repos):
            jobs_dir = os.path.join(repo, "factory", "jobs")
            local_jobs = list_jobs(jobs_dir)
            merged_by_id = {j["id"]: j for j in local_jobs if j.get("id")}
            fleet_banner = ""
            try:
                store = GitStateStore(repo, "status-viewer")
                ctrl = store.get_control_state()
                workers = store.list_worker_heartbeats()
                quota = store.get_quota_state().get("models") or {}
                for rj in store.list_remote_jobs():
                    rid = rj.get("id")
                    if rid:
                        merged_by_id[rid] = rj
                if ctrl.get("halted") or workers or quota:
                    state_word = f"HALTED ({ctrl.get('reason') or 'operator'})" if ctrl.get("halted") else "RUNNING"
                    parts = [f"Fleet: {state_word}"]
                    if workers:
                        w_desc = ", ".join(
                            f"{wid}({w.get('active_jobs', 0)}/{w.get('max_workers', 0)})"
                            for wid, w in sorted(workers.items())
                        )
                        parts.append(f"workers=[{w_desc}]")
                    if quota:
                        parts.append(f"cooldowns={list(sorted(quota.keys()))}")
                    fleet_banner = " | ".join(parts)
            except Exception:
                pass
            jobs = [merged_by_id[k] for k in sorted(merged_by_id)]
            if multi:
                if idx > 0:
                    print()
                print(f"=== {os.path.basename(repo)} ({repo}) ===")
            if fleet_banner:
                print(fleet_banner)
            print(factory_runner_status(jobs_dir, jobs))
            print(format_status_table(jobs))
        return 0

    if args.command == "halt":
        return run_halt(repos, reason=args.reason, force=args.force)

    if args.command == "resume":
        return run_resume(repos)

    if args.command == "worker":
        return run_worker(repos, once=args.once)

    if args.command == "_exec-job":
        if multi:
            print("Error: _exec-job requires a single repository.", file=sys.stderr)
            return 1
        return exec_job_command(repos[0], args.issue)

    if args.command == "gate":
        if multi:
            print("Error: gate requires a single repository.", file=sys.stderr)
            return 1
        repo = repos[0]
        cfg = load_config(os.path.join(repo, "factory"))
        wt = args.worktree or os.path.join(repo, ".worktrees", args.job_id)
        passed, md = run_gate(repo, args.job_id, wt, args.round, cfg)
        print(md)
        return 0 if passed else 1

    if args.command == "doctor":
        all_ok = True
        for idx, repo in enumerate(repos):
            ok, messages = run_doctor(repo)
            all_ok = all_ok and ok
            if multi:
                if idx > 0:
                    print()
                print(f"=== {os.path.basename(repo)} ({repo}) ===")
            for msg in messages:
                print(msg)
        return 0 if all_ok else 1

    if args.command == "clean":
        for idx, repo in enumerate(repos):
            if multi:
                if idx > 0:
                    print()
                print(f"=== {os.path.basename(repo)} ({repo}) ===")
            jobs_dir = os.path.join(repo, "factory", "jobs")
            wt_root = os.path.join(repo, ".worktrees")
            reclaimed: list[str] = []
            if os.path.isdir(wt_root):
                for entry in sorted(os.listdir(wt_root)):
                    wt_path = os.path.join(wt_root, entry)
                    job_file = os.path.join(jobs_dir, entry, "job.json")
                    if not os.path.isfile(job_file):
                        continue
                    try:
                        with open(job_file, "r", encoding="utf-8") as f:
                            job_data = json.load(f)
                    except (OSError, ValueError):
                        continue
                    if job_data.get("stage") in ("merged", "rejected"):
                        reclaimed.append(wt_path)
                        if not args.dry_run:
                            remove_worktree(repo, wt_path)
            if not args.dry_run:
                _run_git(repo, "worktree", "prune", check=False)
            if reclaimed:
                verb = "[dry-run] would remove" if args.dry_run else "removed"
                for path in reclaimed:
                    print(f"{verb} {path}")
            else:
                print("Nothing to clean.")
        return 0

    if args.command == "map":
        for idx, repo in enumerate(repos):
            content = generate_repo_map(repo)
            if args.stdout:
                if multi:
                    print(f"=== {os.path.basename(repo)} ({repo}) ===")
                print(content, end="")
            else:
                out_path = os.path.join(repo, "factory", "REPO_MAP.md")
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"Wrote {out_path}")
        return 0

    if args.command == "init":
        for repo in repos:
            written = init_repository(repo, force=args.force)
            print(f"Initialized {repo} ({len(written)} files updated)")
        return 0

    if args.command == "learn":
        for repo in repos:
            added = learn_from_jobs(repo, job_id=args.job_id)
            if added:
                for b in added:
                    print(f"+ {b}")
            else:
                print("No new pitfalls distilled.")
        return 0

    if args.command == "stats":
        all_jobs: list[dict] = []
        for repo in repos:
            all_jobs.extend(list_jobs(os.path.join(repo, "factory", "jobs")))
        counts_by_stage: dict[str, int] = {}
        merged_costs: list[float] = []
        total_cost = 0.0
        total_in = 0
        total_cache = 0
        llm_dur = 0.0
        gate_dur = 0.0
        deploy_dur = 0.0
        for j in all_jobs:
            st = str(j.get("stage") or "unknown")
            counts_by_stage[st] = counts_by_stage.get(st, 0) + 1
            u = j.get("usage") or {}
            m = j.get("metrics") or {}
            c = float(u.get("cost_usd") or 0.0)
            total_cost += c
            if st == "merged":
                merged_costs.append(c)
            total_in += int(u.get("input_tokens") or 0)
            total_cache += int(u.get("cache_read_tokens") or 0)
            llm_dur += float(u.get("duration_seconds") or 0.0)
            gate_dur += float(m.get("gate_duration_seconds") or 0.0)
            deploy_dur += float(m.get("deploy_duration_seconds") or 0.0)
        total_jobs = len(all_jobs)
        merged_count = counts_by_stage.get("merged", 0)
        merge_rate = round(merged_count / total_jobs, 4) if total_jobs else 0.0
        median_merged_cost = round(statistics.median(merged_costs), 6) if merged_costs else 0.0
        cache_denom = total_in + total_cache
        cache_hit_pct = round((total_cache / cache_denom) * 100.0, 2) if cache_denom else 0.0
        summary = {
            "total_jobs": total_jobs,
            "counts_by_stage": counts_by_stage,
            "merge_rate": merge_rate,
            "total_cost_usd": round(total_cost, 6),
            "median_merged_cost_usd": median_merged_cost,
            "cache_hit_pct": cache_hit_pct,
            "durations_seconds": {
                "llm": round(llm_dur, 3),
                "gate": round(gate_dur, 3),
                "deploy": round(deploy_dur, 3),
            },
        }
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"Total jobs: {total_jobs} | Merge rate: {merge_rate * 100:.1f}%")
            print(f"Stages: {counts_by_stage}")
            print(f"Cost: total=${summary['total_cost_usd']:.4f} | median_merged=${median_merged_cost:.4f} | cache_hit={cache_hit_pct:.1f}%")
            print(f"Durations: llm={llm_dur:.1f}s | gate={gate_dur:.1f}s | deploy={deploy_dur:.1f}s")
        return 0

    if args.command == "logs":
        if multi:
            print("Error: logs requires a single repository.", file=sys.stderr)
            return 1
        jobs_dir = os.path.join(repos[0], "factory", "jobs")
        try:
            job = load_job(jobs_dir, args.job_id)
        except FileNotFoundError:
            print(f"Error: job {args.job_id!r} not found in {jobs_dir}", file=sys.stderr)
            return 1
        jdir = os.path.join(jobs_dir, args.job_id)
        print(f"Job: {job.get('id')} | Issue: #{job.get('issue')} | Stage: {job.get('stage')} | Round: {job.get('round')}")
        if job.get("last_error"):
            print(f"Last error: {job['last_error']}")
        rnum = args.round if args.round is not None else max(1, int(job.get("round") or 1))
        rdir = os.path.join(jdir, f"round-{rnum}")
        if args.round is None and not os.path.isdir(rdir):
            existing_rounds = sorted(
                glob.glob(os.path.join(jdir, "round-*")),
                key=lambda p: int(os.path.basename(p).split("-", 1)[1]) if os.path.basename(p).split("-", 1)[1].isdigit() else 0,
            )
            if existing_rounds:
                rdir = existing_rounds[-1]
                rnum = os.path.basename(rdir).split("-", 1)[1]
        checks_path = os.path.join(rdir, "checks.md")
        if os.path.isfile(checks_path):
            with open(checks_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            print(f"\n--- round-{rnum}/checks.md (tail {args.tail}) ---")
            print("\n".join(lines[-max(1, args.tail):]))
        for rev_path in sorted(glob.glob(os.path.join(rdir, "review-*.json"))):
            try:
                with open(rev_path, encoding="utf-8") as f:
                    rev = json.load(f)
                findings = rev.get("findings") or []
                if findings:
                    print(f"\n--- {os.path.basename(rev_path)} findings ---")
                    for item in findings:
                        msg = item.get("message") if isinstance(item, dict) else str(item)
                        print(f"- {msg}")
            except (OSError, ValueError):
                pass
        qa_fail = os.path.join(jdir, "qa-failure.md")
        if os.path.isfile(qa_fail):
            with open(qa_fail, encoding="utf-8") as f:
                print("\n--- qa-failure.md ---")
                print(f.read().strip())
        return 0

    if args.command == "patrol":
        for idx, repo in enumerate(repos):
            if multi:
                if idx > 0:
                    print()
                print(f"=== {os.path.basename(repo)} ({repo}) ===")
            actions = run_patrol(repo, dry_run=args.dry_run)
            if not actions:
                print("No new patrol findings.")
            for act in actions:
                verb = "[dry-run] would file" if args.dry_run else "filed"
                print(f"{verb} [patrol:{act['fingerprint']}] {act['title']}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
