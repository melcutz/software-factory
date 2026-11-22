---
name: factory
description: Run the autonomous software factory on GitHub issues. Use when the user types /factory run, /factory run <n>, /factory status, /factory doctor, /factory research <topic>, or /factory improve.
---

# Autonomous Software Factory (`software-factory`)

You operate the repository's autonomous software factory (`factory/bin/factory` or `factory`). The deterministic Python runner manages Smart Batch-DAG triage, isolated git worktrees, pluggable headless agent invocations (`agy -p`, optional `opencode run`, and optional local `gpu:*` decision MCP), vertical TDD slice execution, parallel specialist reviews, and GitHub Pull Requests.

## Commands

### `/factory run` or `/factory run <issue-number ...>`
Drain all eligible open `factory`-labeled GitHub issues using Smart Batch-DAG mode by default (or pass `--no-batch` for the legacy sequential single-issue pipeline), or process specific issue number(s):

```bash
./factory/bin/factory plan         # dry-run Smart Batch-DAG triage
./factory/bin/factory run          # drain all eligible queued issues
./factory/bin/factory run 42       # run or resume only issue #42
./factory/bin/factory run 42 43    # run or resume a specific batch of issues
```

- Long multi-slice runs can take several minutes; run with an appropriate command wait or monitor progress via `./factory/bin/factory status`.
- When finished, summarize each processed job's final stage (`merged`, `ready`, `needs-info`, `needs-human`, `rejected`), PR link, slice count, and any question needing human input.

### `/factory status [job-id]`
Show the runner/fleet liveness banner (`Factory: RUNNING` / `Factory: NOT RUNNING`, plus `refs/factory/*` fleet workers and quota cooldowns) and summary table of all jobs, or inspect one job's `job.json`:

```bash
./factory/bin/factory status
./factory/bin/factory status job-42
```

### `/factory queue`
List open `factory`-labeled issues eligible for execution (automatically excludes `factory:needs-info` and `factory:needs-human`):

```bash
./factory/bin/factory queue
```

### `/factory doctor`
Verify prerequisites and layered configuration: `python3 >= 3.10`, `git` + `origin` + `user.name`/`user.email`, `gh auth status`, `agy` (required CLI prerequisite), optional `opencode` (when `opencode/*`, `opencode-go/*`, or `openrouter/*` models are configured), optional `gpu.mcp_url` reachability (when `gpu:*` models are configured), merged `factory/config.json` layers, executable `factory/checks.sh`, and `.gitignore`:

```bash
./factory/bin/factory doctor
```

### Other Operational CLI Subcommands
Run `./factory/bin/factory <command> --help` for full flags on:
- `worker` / `pause` / `unpause` / `halt` / `resume` / `fleet` — continuous multi-machine worker loop and fleet control
- `logs <job-id>` / `stats` / `patrol` — inspect gate/review/QA logs, tier/provider cost telemetry, and repo health sensors
- `init` / `map` / `learn` / `clean` / `gate` / `init-board` — scaffold repo config, refresh `factory/REPO_MAP.md`, distill `factory/PITFALLS.md`, prune finished worktrees/refs, re-run gates, and wire a ProjectV2 board

### `/factory research <topic>`
Read and follow `factory/agents/researcher.md` directly in an interactive `agy` session: investigate primary sources (using `agy`'s read-only `research` subagent for broad exploration if helpful) and save a cited Markdown report under `docs/research/`.

### `/factory improve [area]`
Read and follow `factory/agents/architect.md` interactively in an interactive `agy` session: survey high-churn modules for deepening opportunities, generate the visual HTML report in `${TMPDIR:-/tmp}/`, and walk through candidate trade-offs live with the user.
