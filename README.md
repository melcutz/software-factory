# software-factory-agy

[![Sponsor this project](https://img.shields.io/badge/Sponsor-this%20project-ea4aaa?logo=githubsponsors&logoColor=white)](https://buy.stripe.com/eVq6oI94c1SL5ne37i4Vy01)

`software-factory-agy` is a self-contained issue-to-PR pipeline built on Google Antigravity (`agy`) and the Python 3.10+ standard library. Given a labeled GitHub issue, the runner triages scope and risk, decomposes the work into ordered vertical slices, implements and gates each slice in an isolated git worktree, runs parallel specialist reviews, and either auto-merges or stages the pull request according to configured risk policy.

## Requirements

- **Python 3.10+** (standard library only; no third-party packages)
- **Git** with a GitHub `origin` remote
- **GitHub CLI (`gh`)** authenticated with repository push and PR permissions
- **Antigravity CLI (`agy`)** installed and authenticated on `PATH`

## Quickstart

1. Copy `factory/` and `.agents/skills/factory/` into the root of your repository and ensure the entrypoints are executable:

   ```bash
   chmod +x factory/bin/factory factory/checks.sh
   ```

2. Update `factory/checks.sh` to run your repository's non-interactive verification suite (tests, typechecks, linters), and add runtime directories to `.gitignore`:

   ```gitignore
   /factory/jobs/*
   !/factory/jobs/.gitkeep
   /.worktrees/
   ```

3. Create the workflow labels on GitHub and run preflight checks:

   ```bash
   gh label create "factory" --color "0E8A16" --description "Queued for autonomous software factory" --force
   gh label create "factory:needs-info" --color "FBCA04" --description "Waiting for issue clarification" --force
   gh label create "factory:needs-human" --color "D93F0B" --description "Escalated for human review" --force

   ./factory/bin/factory doctor
   ```

4. Apply the `factory` label to GitHub issues (and optionally create a `"Factory"` GitHub Project board for live Kanban column sync):

   ```bash
   ./factory/bin/factory init-board # optional: create a "Factory" GitHub ProjectV2 board
   ./factory/bin/factory run        # process queued issues up to configured concurrency (DAG-scheduled)
   ./factory/bin/factory run 42 43  # process or resume specific issue number(s)
   ./factory/bin/factory status     # inspect runner liveness, activity/PID, slices, cost, and PR URLs
   ```

See [INSTALL.md](INSTALL.md) for detailed setup instructions and [docs/lessons-learned.md](docs/lessons-learned.md) for architecture notes and engineering lessons.

## Pipeline Lifecycle

```text
GitHub Issue ("factory")
  -> plan          Build dependency DAG (Depends on #N + predicted surfaces) across queued issues
  -> intake        Triage scope, classify risk (low | medium | high) and surfaces (ui, security)
  -> spec          Produce technical specification and 1-6 ordered vertical slices
  -> build & gate  Implement each slice in .worktrees/job-<n> and verify via factory/checks.sh
  -> review        Run routed read-only reviewers in parallel (code, security, ux, ui)
  -> approve       Verify spec compliance, gate history, and merge readiness (fast-path or approver)
  -> merging       Sync with base (agent-assisted conflict resolution), pre-merge gate, and merge
```

| Stage | Behavior |
|---|---|
| `plan` | When running multiple issues, combines explicit markers (`Depends on #N`, `Blocked by #N`, `After #N`) with `planner` surface predictions into an acyclic `graphlib.TopologicalSorter` DAG. Dependent issues wait until parents merge and are skipped automatically if a parent halts. |
| `intake` | Evaluates whether the issue is actionable (`ACCEPT`), underspecified (`NEEDS_INFO`), or out of scope (`REJECT`). `NEEDS_INFO` posts clarifying questions on the issue and swaps the label to `factory:needs-info` until a human replies and re-applies `factory`. |
| `spec` | Inspects the codebase in read-only sandbox mode and produces `spec.md` alongside `plan.json` containing `1–6` testable vertical slices. |
| `build` & `gate` | Executes slices sequentially on branch `factory/job-<n>`. Runs `auto_polish` (if configured) and verifies protected-path diffs + `factory/checks.sh` (`FACTORY_GATE_SCOPE=slice` on intermediate slices, `FACTORY_GATE_SCOPE=full` on the final slice and stage gate, skipping duplicate full runs via `verified_gate_sha`). Opens or updates a draft PR once all slices pass. |
| `review` | Dispatches specialist reviewers concurrently based on risk tier and affected surfaces. Style-only (`nit`) findings do not block; `must_fix` and `should_fix` findings return to `build` for targeted remediation up to `max_rounds`. |
| `approve` & `merging` | Performs final verification (with fast-path approval for clean `low`-risk PRs, or full `approver` review for `medium`/`high`-risk PRs). Under `"auto_merge": "low"`, approved `low`-risk PRs sync with `origin/<base_branch>` (invoking `builder` to resolve merge conflicts if needed), run the `pre-merge` gate, and merge automatically; `medium`- and `high`-risk PRs are marked ready (`gh pr ready`). |

If a job exhausts its retry budget, fails conflict resolution, or triggers an `ESCALATE` decision, the runner transitions the job to `needs-human`, posts context on the issue, and swaps the label to `factory:needs-human`. Re-applying `factory` after replying resumes execution (from `merging` if escalation happened during merge, otherwise with one additional remediation round).

## Observability and GitHub Project Board Sync

- **Live runner & worker status (`./factory/bin/factory status`)**: Tracks `factory/jobs/.factory.pid` and per-job `activity` + `pid` (`waiting:repo_lock`, `running:slice-2`, `running:pre-merge-gate`, etc.), verifying OS PID liveness (`os.kill(pid, 0)`) to print a `Factory: RUNNING` / `Factory: NOT RUNNING` banner and flag crashed workers as `stale (<step>)`.
- **Token & cost telemetry**: Logs timestamps, `model`/`effort`, duration, token breakdown (`in`/`cache`/`out`/`think`), and step/cumulative `$` cost.
- **Idle watchdog & process-group reaping**: Runs headless `agy` turns in isolated `setsid` sessions with an idle watchdog that unlatches when backgrounded children exit and reaps child process groups cleanly.
- **GitHub ProjectV2 sync**: If a `"Factory"` GitHub Project exists in the runner's account (created via `./factory/bin/factory init-board`), the factory automatically adds processed issues and moves them across `Ready` → `Building` → `Review` → `Done` / `Needs Human` / `Skipped`.

## Safety and Isolation

- **Worktree isolation:** All code changes occur inside `.worktrees/job-<n>` on dedicated `factory/job-<n>` branches; the main working tree is never modified during job execution.
- **Sandboxed read-only stages:** `intake`, `spec-writer`, all reviewers, and `approver` execute with `agy --sandbox` and strict JSON Schema validation (`--output-format json --json-schema`). Any unexpected working-tree dirtying or `HEAD` movement is rolled back and treated as an execution failure.
- **Immutable verification gate:** Before executing tests, `gate` checks `git diff -z --name-only --no-renames origin/<base_branch>...HEAD` and rejects changes to `factory/`, `.agents/`, and any configured `protected_paths`. `factory/checks.sh` is always invoked from the main checkout so a job branch cannot alter its own test gate.
- **Untrusted issue containment:** Issue titles, bodies, and comments are escaped and wrapped in `<untrusted_issue_data>` tags prior to prompt construction. Agents never run `git push` or `gh pr`; all remote git and GitHub mutations are performed deterministically by the Python runner.
- **Atomic state and crash recovery:** Per-job state in `factory/jobs/job-<n>/job.json` is updated via atomic temp-file replacement under per-job locks. Re-running `factory run` after an interruption resumes from the current stage and last completed slice.

## CLI and Skill Reference

| CLI Command | `agy` Skill | Description |
|---|---|---|
| `./factory/bin/factory run [n ...]` | `/factory run [n ...]` | Drain open `factory`-labeled issues or process specific issue number(s) |
| `./factory/bin/factory queue` | `/factory queue` | List open eligible issues in the queue |
| `./factory/bin/factory status [job-id]` | `/factory status [job-id]` | Display runner liveness, job status table (activity, slices, cost, PR), or full JSON state for `<job-id>` |
| `./factory/bin/factory init-board` | — | Create a generic `"Factory"` GitHub ProjectV2 board (`Status` + `Priority` fields) |
| `./factory/bin/factory gate <job-id>` | — | Run protected-path and `factory/checks.sh` verification on a worktree |
| `./factory/bin/factory doctor` | `/factory doctor` | Validate Python, `git`, `gh`, `agy`, `config.json`, `checks.sh`, and `.gitignore` |
| — | `/factory research <topic>` | Run interactive primary-source research into `docs/research/` |
| — | `/factory improve [area]` | Generate an interactive architecture review for module boundaries |

## Configuration

`factory/config.json` controls concurrency, retry budgets, merge behavior, protected paths, and per-role model selection:

```json
{
  "concurrency": 4,
  "max_rounds": 3,
  "max_slice_retries": 2,
  "agent_timeout_sec": 900,
  "gate_timeout_sec": 600,
  "base_branch": "main",
  "merge_method": "merge",
  "auto_merge": "low",
  "labels": {
    "queue": "factory",
    "needs_info": "factory:needs-info",
    "needs_human": "factory:needs-human"
  },
  "protected_paths": [
    "factory/",
    ".agents/",
    ".github/"
  ],
  "models": {
    "planner": "gemini-3.8-flash-medium",
    "intake": "gemini-3.8-flash-medium",
    "spec_writer": "gemini-4-argon-high",
    "builder": "gemini-4-argon-high",
    "approver": "gemini-4-argon-high",
    "code_low": "gemini-3.6-flash-low",
    "code_medium": "gemini-3.8-flash-low",
    "code_high": "gemini-3.8-flash-medium",
    "security": "gemini-4-argon-high",
    "ux": "gemini-3.8-flash-medium",
    "ui": "gemini-3.6-flash-low"
  }
}
```

### Merge Policies (`auto_merge`)

- `"low"` *(default)*: Auto-merge approved `low`-risk PRs after base-branch sync and gate re-verification; mark `medium`- and `high`-risk PRs ready for human merge.
- `"all"`: Auto-merge all approved PRs regardless of risk classification.
- `"none"`: Never auto-merge; leave all approved PRs in ready-for-review state.

## Repository Layout

```text
.agents/skills/factory/SKILL.md   Antigravity slash-command skill definition
factory/
  bin/factory                     CLI entrypoint
  config.json                     Runtime and model routing configuration
  checks.sh                       Repository verification gate script
  agents/                         Role instructions for planner, intake, spec, build, review, and approval
  lib/
    agy_runner.py                 Headless agy invocation, idle watchdog, setsid reaping, and sandbox checks
    config.py                     Configuration loader, model pricing, and reviewer routing rules
    gate_runner.py                Protected-path diff verification, auto-polish, and checks.sh runner
    git_ops.py                    Worktree management, base-branch sync, conflict helpers, and gh CLI client
    pipeline.py                   End-to-end state orchestration, DAG pool, activity tracking, and doctor checks
    planner.py                    Queue dependency DAG builder and cycle-breaking fallback
    project_board.py              Generic "Factory" GitHub ProjectV2 creation and stage-to-column sync
    schemas.py                    Stage JSON schemas and Markdown artifact renderers
    state.py                      Thread-safe atomic job.json state machine and resume logic
tests/                            Unit and end-to-end pipeline test suite
```

## Development

Run the full unit and end-to-end test suite:

```bash
bash factory/checks.sh
```

## License

Released under the [MIT License](LICENSE).
