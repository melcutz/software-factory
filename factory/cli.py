"""Central CLI entrypoint for the Autonomous Software Factory."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys

from factory.lib.config import load_config
from factory.lib.gate_runner import run_gate
from factory.lib.git_ops import GitHubClient
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-board":
        res = create_factory_board(owner=args.owner, title=args.title, cwd=os.getcwd())
        proj = res["project"]
        verb = "Created" if res["created"] else "Already exists"
        print(f"{verb}: {proj.get('title', args.title)} ({proj.get('url', '')})")
        return 0

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

    return 1


if __name__ == "__main__":
    sys.exit(main())
