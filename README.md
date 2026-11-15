# software-factory-agy

[![Sponsor this project](https://img.shields.io/badge/Sponsor-this%20project-ea4aaa?logo=githubsponsors&logoColor=white)](https://buy.stripe.com/eVq6oI94c1SL5ne37i4Vy01)

`software-factory-agy` (`v0.2.0`) is a self-contained, zero-central-server autonomous issue-to-PR software factory built on Google Antigravity (`agy`) and the Python 3.10+ standard library.

Given labeled GitHub issues, it triages scope and risk, builds a dependency DAG, writes a technical spec with ordered vertical slices, implements and gates each slice in an isolated git worktree, runs parallel specialist reviews, and auto-merges or stages PRs—either on a single machine (`factory run`) or across a **durable multi-machine fleet** (`factory worker --all`) coordinated through Git refs (`refs/factory/*`) and [`herdr-core` (`hc`) + `herdr-app`](https://herdr.dev/).

*(Looking for the simpler `v0.1.0` single-machine baseline? Check out the [`v0.1.0` release](https://github.com/melcutz/software-factory-agy/releases/tag/v0.1.0).)*

---

## Key Features

- **Central Multi-Repo CLI (`factory`):** Install once (`pip install -e .`) and drive any repository with `--repo <path>` or all registered repositories via `--all` (`~/.config/factory/repos.json`).
- **End-to-End Autonomous Pipeline:** `plan` (DAG) → `intake` → `spec` (1–6 vertical slices) → `build & gate` → `review` (parallel code, security, UX, UI) → `approve` → `merging` → optional `staging_qa` deploy train.
- **Zero-Central-Server Multi-Machine Coordination (`refs/factory/*`):** Atomic `--force-with-lease` Compare-And-Swap job leases, worker heartbeats (`360s` TTL + `60s` skew buffer), slice-level crash checkpoints, distributed staging locks, and shared DAG caching—no coordinator daemon or database required.
- **`herdr-core` (`hc`) & `herdr-app` Visibility:** Workers emit native **OSC 7501** status sequences (`working`, `idle`, `done`, `blocked`) and spawn job workers via `hc run --bg`, surfacing all local and remote jobs live in `herdr-app`'s Agents sidebar with one-click **peek** and `needs-human` rescue shells. Works cleanly in plain terminals too when `$HC_TERMINAL_ID` is unset.
- **Global Kill Switch & Polite Pause:** `factory halt` / `factory resume` stops the entire fleet cleanly at the last completed slice without burning retries or escalating to `needs-human`; `factory pause` / `factory unpause` controls claims on a single machine.
- **Homelab-Polite Resource & Quota Governor:** Probes host CPU load and free RAM (`~/.config/factory/host.json`) before claiming or between slices, spreads batches evenly across idle machines, and turns LLM `429` / `RESOURCE_EXHAUSTED` errors into shared fleet-wide cooldowns (`refs/factory/quota`).

---

## Requirements

- **Python 3.10+** (standard library only; zero third-party runtime packages)
- **Git** with a GitHub `origin` remote
- **GitHub CLI (`gh`)** authenticated (`gh auth status`) with push and PR permissions
- **Antigravity CLI (`agy`)** installed and authenticated on `PATH`
- **Optional (for multi-machine desktop/SSH visibility):** [`herdr-app` / `herdr-core` (`hc`)](https://herdr.dev/) on Linux, macOS, or Windows/WSL2

---

## Quickstart (5 Minutes)

### 1. Install the `factory` CLI

```bash
git clone https://github.com/melcutz/software-factory-agy.git
cd software-factory-agy
pip install -e .
```

### 2. Prepare a Target Repository

In each repository you want the factory to work on:

1. Create `factory/config.json` (see [Configuration](#configuration)) and `factory/checks.sh` (your non-interactive test/lint/typecheck suite; `chmod +x factory/checks.sh`).
2. Add runtime paths to `.gitignore`:
   ```gitignore
   /factory/jobs/*
   !/factory/jobs/.gitkeep
   /.worktrees/
   ```
3. Create GitHub labels and verify prerequisites:
   ```bash
   gh label create "factory" --color "0E8A16" --description "Queued for autonomous software factory" --force
   gh label create "factory:needs-info" --color "FBCA04" --description "Waiting for issue clarification" --force
   gh label create "factory:needs-human" --color "D93F0B" --description "Escalated for human review" --force

   factory doctor --repo /path/to/your-repo
   ```

### 3. Run Single-Machine or Multi-Machine

**Single-machine (one-shot drain):**
```bash
factory run --repo /path/to/your-repo        # drain queued issues
factory run --repo /path/to/your-repo 42 43  # run specific issues
factory status --repo /path/to/your-repo     # check progress, machine, cost, PRs
```

**Multi-machine durable fleet (with `herdr-core` / `herdr-app`):**
```bash
# Register your target repos once in ~/.config/factory/repos.json:
# ["~/work/repo-a", "~/work/repo-b"]

# Start a durable worker inside hc / herdr-app on each machine:
hc run --bg --name factory-worker -- factory worker --all

# Or wake all machines registered in `hc machine ls` from one laptop:
factory fleet wake

# Inspect fleet status or halt/resume everything at once:
factory status --all
factory halt --reason "maintenance"
factory resume
```

See [INSTALL.md](INSTALL.md) for the complete step-by-step setup guide (including `~/.config/factory/host.json`, `hc machine` setup, and staging QA hooks).

---

## Pipeline Lifecycle

```text
GitHub Issue ("factory")
  -> plan          Build dependency DAG (Depends on #N + predicted surfaces) across queued issues
  -> intake        Triage scope, classify risk (low | medium | high) and surfaces (ui, security)
  -> spec          Produce technical specification and 1-6 ordered vertical slices
  -> build & gate  Implement each slice in .worktrees/job-<n>, verify via factory/checks.sh, checkpoint to Git
  -> review        Run routed read-only reviewers in parallel (code, security, ux, ui)
  -> approve       Verify spec compliance, gate history, and merge readiness (fast-path or approver)
  -> merging       Sync with base (agent-assisted conflict resolution), pre-merge gate, and merge
  -> staging_qa    (Optional) Batched deploy train + staging verification with automatic rollback
```

If a job exhausts its retry budget or hits `BLOCKED`, it transitions to `needs-human`, comments on the issue, swaps the label to `factory:needs-human`, emits OSC 7501 `state=blocked:kind=question` (surfacing in `herdr-app`), and leaves an interactive rescue shell in `.worktrees/job-<n>`. Re-applying `factory` after replying resumes from the saved checkpoint.

---

## CLI Reference

| Command | Description |
|---|---|
| `factory run [n ...]` | Drain open `factory`-labeled issues or process specific issue number(s) |
| `factory worker [--once]` | Run continuous competing-consumer worker loop (`--repo` or `--all`) |
| `factory queue` | List open eligible issues in the queue |
| `factory status [job-id]` | Show fleet banner, active machines, job table (`MACHINE`, slices, cost, PR), or JSON for `<job-id>` |
| `factory halt [--reason TEXT] [--force]` | Globally halt all fleet workers and checkpoint active jobs cleanly |
| `factory resume` | Clear global halt and allow workers to resume |
| `factory pause [--drain]` | Pause new job claims on the current machine |
| `factory unpause` | Resume job claims on the current machine |
| `factory fleet {status,wake,pause,unpause,update} [machine]` | Discover, update, and control peer machines via `hc machine ls` / `hc run --on` |
| `factory init-board` | Create a generic `"Factory"` GitHub ProjectV2 board (`Status` + `Priority`) |
| `factory gate <job-id>` | Run protected-path and `factory/checks.sh` verification on a worktree |
| `factory doctor` | Validate Python, `git`, `gh`, `agy`, `config.json`, `checks.sh`, and `.gitignore` |

---

## Configuration

`factory/config.json` in each target repository controls concurrency, retry budgets, merge behavior (`"auto_merge": "low" | "all" | "none"`), protected paths, and per-role model selection:

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
  "protected_paths": ["factory/", ".agents/", ".github/"],
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

---

## Repository Layout

```text
.agents/skills/factory/SKILL.md   Antigravity slash-command skill definition
factory/
  cli.py                          Central CLI entrypoint (`factory`)
  bin/factory                     Repo-local wrapper script
  config.json                     Default runtime and model routing configuration
  checks.sh                       Verification gate script
  agents/                         Role prompts (planner, intake, spec-writer, builder, reviewers, approver)
  lib/
    agy_runner.py                 Headless agy runner, idle watchdog, setsid reaping, 429 QuotaExhaustedError
    config.py                     Configuration loader, model pricing, reviewer routing
    fleet.py                      Fleet discovery & control via `hc machine ls` / `hc run --on`
    gate_runner.py                Protected-path diff verification, auto-polish, checks.sh runner
    git_ops.py                    Worktree management, base-branch sync, gh CLI client
    git_state.py                  Git-native refs/factory/* CAS leases, checkpoints, locks, control, quota
    governor.py                   Cross-platform CPU/RAM admission checks & shared quota cooldown helpers
    herdr_bridge.py               herdr-core (hc) background child terminals & OSC 7501 status protocol
    pipeline.py                   End-to-end stage orchestration, staging train, halt/resume
    planner.py                    Dependency DAG builder and cycle-breaking fallback
    project_board.py              GitHub ProjectV2 board creation and stage-to-column sync
    schemas.py                    Stage JSON schemas and Markdown renderers
    staging_qa.py                 Staging QA deploy train coordinator
    state.py                      Thread-safe atomic job.json state machine
    worker.py                     Continuous spread-first worker loop & _exec-job runner
tests/                            Unit and end-to-end pytest suite
```

## Development

Run the full test suite:

```bash
pytest
```

## License

Released under the [MIT License](LICENSE).
