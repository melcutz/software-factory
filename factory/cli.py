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

    _FMT = argparse.RawDescriptionHelpFormatter

    parser = argparse.ArgumentParser(
        prog="factory",
        description="Autonomous Software Factory for Google Antigravity (agy)",
        formatter_class=_FMT,
        epilog=(
            "How it works:\n"
            "  1. Queue & triage: `queue` lists eligible `factory`-labeled GitHub issues;\n"
            "     `plan` dry-runs Smart Batch triage into a parallel/sequential Batch-DAG.\n"
            "  2. Execute: `run` (single/batch) or `worker` (daemon) claims issues, creates\n"
            "     isolated git worktrees under `.worktrees/`, runs build/gate/review/merge,\n"
            "     and records state in `factory/jobs/` and `refs/factory/*`.\n"
            "  3. Inspect & maintain: `status`, `logs`, `stats`, `clean`, `patrol`, `doctor`.\n\n"
            "Examples:\n"
            "  factory queue\n"
            "  factory plan\n"
            "  factory run 42\n"
            "  factory patrol --dry-run\n"
            "  factory clean && factory status\n\n"
            "Run `factory <command> --help` (e.g. `factory patrol --help`) for details."
        ),
        parents=[repo_flags],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Drain queued factory issues or process specific issue number(s)",
        description=(
            "Drain queued `factory`-labeled GitHub issues or run specific issue number(s).\n\n"
            "How it works:\n"
            "  By default, Smart Batch Mode triages open issues into a dependency-aware\n"
            "  Batch-DAG (parallelizing non-conflicting work), creates isolated git\n"
            "  worktrees under `.worktrees/`, runs implementation, `gate` checks, code\n"
            "  review, and merge/deploy, and writes per-job state under `factory/jobs/`.\n"
            "  Use `--no-batch` to force the legacy sequential single-issue pipeline."
        ),
        epilog=(
            "Examples:\n"
            "  factory run\n"
            "  factory run 42 43\n"
            "  factory run --no-batch 42\n"
            "  factory run --max-batch 3"
        ),
    )
    p_run.add_argument("issues", nargs="*", type=int, help="Optional GitHub issue numbers (default: drain all queued)")
    p_run.add_argument("--no-batch", action="store_true", help="Disable Smart Batch Mode and use legacy single-issue pipeline")
    p_run.add_argument("--max-batch", type=int, default=None, help="Override batch.max_batch_issues for this run")

    sub.add_parser(
        "queue",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="List open eligible factory-labeled GitHub issues",
        description=(
            "List open GitHub issues eligible for the factory queue.\n\n"
            "How it works:\n"
            "  Queries GitHub via `gh` for open issues carrying the configured `factory`\n"
            "  label and filters out issues already blocked or claimed, printing issue\n"
            "  numbers and titles in queue order."
        ),
        epilog="Examples:\n  factory queue\n  factory queue --all",
    )

    p_plan = sub.add_parser(
        "plan",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Dry-run smart batch triage and print the planned parallel/sequential Batch-DAG",
        description=(
            "Dry-run Smart Batch triage without creating worktrees or modifying issues.\n\n"
            "How it works:\n"
            "  Inspects queued (or specified) GitHub issues, estimates touched files and\n"
            "  dependencies, and prints the planned Batch-DAG stages (which issues can\n"
            "  run in parallel vs. sequentially) before you commit to `factory run`."
        ),
        epilog="Examples:\n  factory plan\n  factory plan 10 11 12",
    )
    p_plan.add_argument("issues", nargs="*", type=int, help="Optional GitHub issue numbers (default: all queued)")

    p_status = sub.add_parser(
        "status",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Show job status summary or detailed JSON for one job",
        description=(
            "Show factory runner/fleet status and a summary table of jobs (or JSON for one job).\n\n"
            "How it works:\n"
            "  Reads local `factory/jobs/*/job.json` files and, when `origin` is configured,\n"
            "  merges remote `refs/factory/*` fleet control state, worker heartbeats,\n"
            "  model quota cooldowns, and remote job refs. Pass a `job_id` for full JSON."
        ),
        epilog="Examples:\n  factory status\n  factory status job-42\n  factory status --all",
    )
    p_status.add_argument("job_id", nargs="?", help="Optional job id (e.g. job-42)")

    p_gate = sub.add_parser(
        "gate",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Run protected_paths + factory/checks.sh for a job worktree",
        description=(
            "Run protected-path validation and `factory/checks.sh` for a job worktree.\n\n"
            "How it works:\n"
            "  Verifies that modified files in the target worktree do not violate\n"
            "  `protected_paths` in `factory/config.json`, executes `factory/checks.sh`,\n"
            "  and writes the round report to `factory/jobs/<job_id>/round-<N>/checks.md`."
        ),
        epilog="Examples:\n  factory gate job-42\n  factory gate job-42 .worktrees/job-42 2",
    )
    p_gate.add_argument("job_id", help="Job id (e.g. job-42)")
    p_gate.add_argument("worktree", nargs="?", help="Worktree path (defaults to .worktrees/<job_id>)")
    p_gate.add_argument("round", nargs="?", type=int, default=1, help="Round number (default: 1)")

    sub.add_parser(
        "doctor",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Verify git, gh, agy, config.json, checks.sh, and .gitignore",
        description=(
            "Verify repository and host prerequisites for running the factory.\n\n"
            "How it works:\n"
            "  Checks that `git`, `gh`, and `agy` are installed, `git config user.name`\n"
            "  and `user.email` are set, `factory/config.json` and executable\n"
            "  `factory/checks.sh` exist, `python3 -m pytest` works (if referenced by\n"
            "  `checks.sh`), and `.gitignore` ignores `.worktrees/` and `factory/jobs/`."
        ),
        epilog="Examples:\n  factory doctor\n  factory doctor --all",
    )

    p_board = sub.add_parser(
        "init-board",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Create a generic 'Factory' GitHub Project board",
        description=(
            "Create and wire a GitHub Project board for tracking factory issues.\n\n"
            "How it works:\n"
            "  Uses `gh` to create a GitHub Project (default title `Factory` under\n"
            "  `@me`) with standard workflow columns and stores board metadata."
        ),
        epilog="Examples:\n  factory init-board\n  factory init-board --owner my-org --title 'Factory'",
    )
    p_board.add_argument("--owner", default="@me", help="GitHub owner login (default: @me)")
    p_board.add_argument("--title", default="Factory", help="Project title (default: Factory)")

    p_halt = sub.add_parser(
        "halt",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Globally halt the factory fleet and checkpoint active jobs",
        description=(
            "Globally halt all factory workers across the fleet.\n\n"
            "How it works:\n"
            "  Writes a halted state (with optional `--reason`) into `refs/factory/control`\n"
            "  on `origin`. Workers stop claiming new issues and checkpoint active jobs;\n"
            "  `--force` interrupts immediately even during staging deploy."
        ),
        epilog="Examples:\n  factory halt --reason 'maintenance'\n  factory halt --force",
    )
    p_halt.add_argument("--reason", default="", help="Optional reason recorded in refs/factory/control")
    p_halt.add_argument("--force", action="store_true", help="Interrupt immediately even during staging deploy")

    sub.add_parser(
        "resume",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Clear global halt and allow workers to resume",
        description=(
            "Clear a global fleet halt so workers can resume claiming jobs.\n\n"
            "How it works:\n"
            "  Updates `refs/factory/control` on `origin` to clear the halted flag\n"
            "  set by `factory halt`."
        ),
        epilog="Examples:\n  factory resume\n  factory resume --all",
    )

    p_worker = sub.add_parser(
        "worker",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Run continuous competing-consumer worker loop",
        description=(
            "Run the competing-consumer factory worker loop on this machine.\n\n"
            "How it works:\n"
            "  Polls eligible `factory`-labeled GitHub issues, acquires atomic leases\n"
            "  via git refs (`refs/factory/*`), publishes worker heartbeats, respects\n"
            "  global `halt` and local `pause` state, and runs `_exec-job` in parallel\n"
            "  slots. Pass `--once` to execute a single poll cycle and exit."
        ),
        epilog="Examples:\n  factory worker\n  factory worker --once\n  factory worker --all",
    )
    p_worker.add_argument("--once", action="store_true", help="Run a single poll cycle and exit")

    p_exec = sub.add_parser(
        "_exec-job",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Internal: execute a single claimed job",
        description=(
            "Internal entrypoint used by `factory worker` to execute one claimed issue.\n\n"
            "How it works:\n"
            "  Runs the single-issue job pipeline for the given GitHub issue number\n"
            "  inside the calling worker's lease."
        ),
    )
    p_exec.add_argument("issue", type=int, help="GitHub issue number")

    p_pause = sub.add_parser(
        "pause",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Pause new job claims on this machine",
        description=(
            "Pause new job claims on the local machine without halting the whole fleet.\n\n"
            "How it works:\n"
            "  Sets the local worker pause marker so this host stops claiming new\n"
            "  issues. With `--drain`, waits for currently running local jobs to finish."
        ),
        epilog="Examples:\n  factory pause\n  factory pause --drain",
    )
    p_pause.add_argument("--drain", action="store_true", help="Drain active jobs cleanly")

    sub.add_parser(
        "unpause",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Resume job claims on this machine",
        description=(
            "Clear the local pause marker so this machine resumes claiming jobs.\n\n"
            "How it works:\n"
            "  Removes the local pause state created by `factory pause`."
        ),
        epilog="Examples:\n  factory unpause",
    )

    p_fleet = sub.add_parser(
        "fleet",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Manage peer machines via hc machine",
        description=(
            "Manage peer worker machines in the fleet via `hc machine`.\n\n"
            "How it works:\n"
            "  Dispatches `status`, `wake`, `pause`, `unpause`, `update`, or `remove`\n"
            "  to one named machine or all configured fleet peers."
        ),
        epilog="Examples:\n  factory fleet status\n  factory fleet pause Base --drain\n  factory fleet wake Rocket",
    )
    p_fleet.add_argument("action", choices=["status", "wake", "pause", "unpause", "update", "remove"], help="Fleet action")
    p_fleet.add_argument("machine", nargs="?", help="Optional target machine name")
    p_fleet.add_argument("--drain", action="store_true", help="Drain active jobs when pausing")

    p_clean = sub.add_parser(
        "clean",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Remove worktrees for merged/rejected jobs and prune stale git worktrees",
        description=(
            "Clean up finished worktrees and reconcile stale local/remote job status.\n\n"
            "How it works:\n"
            "  1. Removes `.worktrees/<job_id>` for `merged` or `rejected` jobs.\n"
            "  2. Checks non-terminal jobs against GitHub: removes empty `queued` stubs\n"
            "     (no `logs/` or `round-*`) for CLOSED issues, and marks CLOSED jobs\n"
            "     that have logs as `rejected` while preserving their logs.\n"
            "  3. Removes `_planner` scratch worktrees and runs `git worktree prune`.\n"
            "  4. Reconciles stale `refs/factory/*` job/worker refs on `origin` so\n"
            "     `factory status` reflects clean real state."
        ),
        epilog="Examples:\n  factory clean --dry-run\n  factory clean\n  factory clean --all",
    )
    p_clean.add_argument("--dry-run", action="store_true", help="Show worktrees that would be removed without deleting them")

    p_map = sub.add_parser(
        "map",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Generate factory/REPO_MAP.md from tracked repository layout",
        description=(
            "Generate a concise repository map (`factory/REPO_MAP.md`) for agents.\n\n"
            "How it works:\n"
            "  Scans git-tracked files in the repository, groups key modules and entry\n"
            "  points, and writes `factory/REPO_MAP.md` (or prints with `--stdout`)."
        ),
        epilog="Examples:\n  factory map\n  factory map --stdout",
    )
    p_map.add_argument("--stdout", action="store_true", help="Print generated map to stdout instead of writing file")

    p_init = sub.add_parser(
        "init",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Scaffold factory/config.json, checks.sh, REPO_MAP.md, and .gitignore",
        description=(
            "Initialize a repository for autonomous factory runs.\n\n"
            "How it works:\n"
            "  Detects project tooling and scaffolds `factory/config.json`,\n"
            "  `factory/checks.sh`, `factory/REPO_MAP.md`, and `.gitignore` entries\n"
            "  (`.worktrees/`, `factory/jobs/`). Pass `--force` to overwrite existing files."
        ),
        epilog="Examples:\n  factory init\n  factory init --force",
    )
    p_init.add_argument("--force", action="store_true", help="Overwrite existing factory files")

    p_learn = sub.add_parser(
        "learn",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Distill guardrails from multi-round/QA jobs into factory/PITFALLS.md",
        description=(
            "Distill recurring failure patterns into `factory/PITFALLS.md` guardrails.\n\n"
            "How it works:\n"
            "  Inspects multi-round or QA-failed jobs under `factory/jobs/` (or one\n"
            "  specific `job_id`), extracts root-cause lessons, and appends new\n"
            "  guardrails to `factory/PITFALLS.md` for future agent runs."
        ),
        epilog="Examples:\n  factory learn\n  factory learn job-12 --deterministic",
    )
    p_learn.add_argument("job_id", nargs="?", help="Optional specific job ID (e.g. job-12)")
    p_learn.add_argument("--deterministic", action="store_true", help="Use deterministic extraction without LLM")

    p_stats = sub.add_parser(
        "stats",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Show telemetry summary across factory jobs",
        description=(
            "Aggregate cost, cache, stage, and duration telemetry across factory jobs.\n\n"
            "How it works:\n"
            "  Reads all `factory/jobs/*/job.json` records across the target repo(s)\n"
            "  and computes total jobs, stage counts, merge rate, total and median\n"
            "  merged USD cost, prompt cache hit %, and LLM/gate/deploy durations."
        ),
        epilog="Examples:\n  factory stats\n  factory stats --json\n  factory stats --all --json",
    )
    p_stats.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    p_logs = sub.add_parser(
        "logs",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Show failure/gate/review summary for a job",
        description=(
            "Inspect gate output, review findings, and QA failures for a single job.\n\n"
            "How it works:\n"
            "  Loads `factory/jobs/<job_id>/job.json`, prints stage, round, and\n"
            "  `last_error`, then tails `round-<N>/checks.md` and prints any findings\n"
            "  from `round-<N>/review-*.json` and `qa-failure.md`."
        ),
        epilog="Examples:\n  factory logs job-12\n  factory logs job-12 --round 2 --tail 80",
    )
    p_logs.add_argument("job_id", help="Job ID (e.g. job-12)")
    p_logs.add_argument("--round", type=int, default=None, help="Specific round number")
    p_logs.add_argument("--tail", type=int, default=40, help="Number of trailing lines from checks.md")

    p_patrol = sub.add_parser(
        "patrol",
        parents=[repo_flags],
        formatter_class=_FMT,
        help="Run repo health sensors and file factory issues",
        description=(
            "Scan repository health and file deduplicated `factory`-labeled GitHub issues.\n\n"
            "How it works:\n"
            "  1. Runs built-in sensors:\n"
            "     - `stale-repo-map`: triggers if `factory/REPO_MAP.md` is > 14 days old\n"
            "       (recommends `factory map`).\n"
            "     - `repeated-gate-retries`: triggers if >= 3 jobs in `factory/jobs/`\n"
            "       required >= 3 rounds (recommends `factory learn` / `factory logs`).\n"
            "  2. Loads optional `factory/patrol_hooks.py` and calls\n"
            "     `collect_patrol_findings(repo_root)` for custom findings with\n"
            "     `fingerprint`, `title`, and optional `body`.\n"
            "  3. Checks open GitHub issues for `[patrol:<fingerprint>]`; skips any\n"
            "     already-open finding and files new issues labeled `factory`\n"
            "     (unless `--dry-run` is passed)."
        ),
        epilog=(
            "Examples:\n"
            "  factory patrol --dry-run\n"
            "  factory patrol\n"
            "  factory patrol --all --dry-run"
        ),
    )
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
        extra_kwargs: dict = {}
        if args.no_batch:
            extra_kwargs["batch_mode"] = False
        if args.max_batch is not None:
            extra_kwargs["max_batch_issues"] = args.max_batch
        if not multi:
            jobs = run_factory(
                repos[0],
                issue_numbers=args.issues or None,
                **extra_kwargs,
            )
            print(format_status_table(jobs))
            return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(repos)) as pool:
            futs = {
                pool.submit(
                    run_factory,
                    repo,
                    **extra_kwargs,
                ): repo
                for repo in repos
            }
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
        import shutil
        from factory.lib.state import _atomic_write_job

        for idx, repo in enumerate(repos):
            if multi:
                if idx > 0:
                    print()
                print(f"=== {os.path.basename(repo)} ({repo}) ===")
            jobs_dir = os.path.join(repo, "factory", "jobs")
            wt_root = os.path.join(repo, ".worktrees")
            reclaimed: list[str] = []
            terminal_ids: set[str] = set()
            closed_stub_ids: set[str] = set()

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

            if os.path.isdir(jobs_dir):
                gh = GitHubClient(repo)
                for entry in sorted(os.listdir(jobs_dir)):
                    if entry == "_planner":
                        planner_dir = os.path.join(jobs_dir, entry)
                        reclaimed.append(planner_dir)
                        if not args.dry_run:
                            shutil.rmtree(planner_dir, ignore_errors=True)
                        continue
                    jdir = os.path.join(jobs_dir, entry)
                    job_file = os.path.join(jdir, "job.json")
                    if not os.path.isfile(job_file):
                        continue
                    try:
                        with open(job_file, "r", encoding="utf-8") as f:
                            job_data = json.load(f)
                    except (OSError, ValueError):
                        continue
                    stage = job_data.get("stage")
                    if stage in ("merged", "rejected"):
                        terminal_ids.add(entry)
                        continue
                    inum = job_data.get("issue")
                    if not isinstance(inum, int):
                        continue
                    try:
                        iss, _ = gh.fetch_issue(inum)
                    except Exception:
                        continue
                    if str(iss.get("state") or "").upper() != "CLOSED":
                        continue
                    has_logs = os.path.isdir(os.path.join(jdir, "logs")) or any(
                        d.startswith("round-") for d in os.listdir(jdir)
                    )
                    if stage == "queued" and not has_logs:
                        reclaimed.append(jdir)
                        closed_stub_ids.add(entry)
                        if not args.dry_run:
                            shutil.rmtree(jdir, ignore_errors=True)
                    else:
                        reclaimed.append(f"{entry} (mark rejected, logs preserved)")
                        terminal_ids.add(entry)
                        if not args.dry_run:
                            job_data["stage"] = "rejected"
                            _atomic_write_job(jobs_dir, entry, job_data)

            if not args.dry_run:
                _run_git(repo, "worktree", "prune", check=False)

            remote_check = _run_git(repo, "remote", "get-url", "origin", check=False)
            if remote_check.returncode == 0:
                try:
                    store = GitStateStore(repo, "cleaner")
                    pruned_refs = store.reconcile_clean_refs(
                        terminal_job_ids=terminal_ids,
                        closed_job_ids=closed_stub_ids,
                        dry_run=args.dry_run,
                    )
                    reclaimed.extend(pruned_refs)
                except Exception:
                    pass

            if reclaimed:
                verb = "[dry-run] would clean" if args.dry_run else "cleaned"
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
