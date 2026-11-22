# Setup & Installation Guide (`software-factory` v0.3.0)

This guide walks you through installing `software-factory`, configuring a target repository, verifying prerequisites, and running your first issue—on one machine or across a multi-machine fleet.

For all configuration keys and layer precedence rules, see the [Configuration Reference](README.md#5-configuration-reference).

---

## 1. Prerequisites

- **Python 3.10+** (standard library only; zero third-party runtime dependencies)
- **Git** with `user.name` and `user.email` configured
- **GitHub CLI (`gh >= 2.67`)** authenticated (`gh auth status`) with push and PR permissions *(older `gh` versions can hit deprecated Projects classic GraphQL errors on `gh pr edit`)*
- **Coding agent CLIs**:
  - **Antigravity CLI (`agy`)** (required prerequisite) installed and authenticated on `PATH` (or configured via `providers.agy.bin` / `path_prepend`)
  - **Optional**: **OpenCode CLI (`opencode`)** if any role uses `opencode/*`, `openrouter/*`, or `opencode-go/*`
- **Tooling used by your target repo's `factory/checks.sh`** (e.g., `pytest`, `npm`, `cargo`, `go`)
- **Optional (for multi-machine fleet visibility)**: [`herdr-core` (`hc`) / `herdr-app`](https://herdr.dev/)

---

## 2. Install the `factory` CLI

Clone this repository and install it in editable mode on each machine that will run factory jobs:

```bash
git clone https://github.com/melcutz/software-factory.git
cd software-factory
pip install -e .
# On Ubuntu 24.04+ / externally managed Python environments:
# python3 -m pip install --user --break-system-packages --no-deps -e .
```

Verify `factory` is on `PATH`:

```bash
factory --help
```

*(If you also want `/factory` slash commands inside `agy`, copy `.agents/skills/factory/` into your target repository or global skills directory.)*

---

## 3. Configure a Target Repository

Initialize the repository you want the factory to manage (for example, `~/src/my-app`):

```bash
cd ~/src/my-app
factory init
```

`factory init` scaffolds:
- `factory/config.json` — repo-committed settings (`base_branch`, `auto_merge`, `protected_paths`, `models`)
- `factory/checks.sh` — deterministic verification gate (`chmod +x factory/checks.sh`)
- `factory/REPO_MAP.md` — concise repository map for agents
- `factory/jobs/.gitkeep`
- `.gitignore` entries (`/factory/jobs/*`, `!/factory/jobs/.gitkeep`, `/.worktrees/`, `factory/config.local.json`)

Next:

1. **Customize `factory/checks.sh`**:
   Ensure it runs your non-interactive test/lint/typecheck suite, exits non-zero on failure, and does not rewrite tracked files. It receives `FACTORY_GATE_SCOPE=slice|full` and `FACTORY_BASE_BRANCH=<branch>`.
2. **Customize `factory/config.json`** (and optional machine-local overrides in `~/.config/factory/config.json` or `factory/config.local.json`):
   - See [factory/config.example.json](factory/config.example.json) and the [README Configuration Reference](README.md#5-configuration-reference) for fallback chains, `provider_timeouts`, `gpu` (optional local-LLM decision MCP), `quota.buckets`, `model_pricing`, `path_prepend`, and `staging_qa`.
3. **Create GitHub Labels** on the target repo:
   ```bash
   gh label create "factory" --color "0E8A16" --description "Queued for autonomous software factory" --force
   gh label create "factory:needs-info" --color "FBCA04" --description "Waiting for issue clarification" --force
   gh label create "factory:needs-human" --color "D93F0B" --description "Escalated for human review" --force
   # Optional label for human-approved ready PRs (labels.ship):
   gh label create "factory:ship" --color "1D76DB" --description "Approved by human for factory merge" --force
   ```
4. *(Optional)* **Create a GitHub ProjectV2 Board**:
   ```bash
   factory init-board --owner example-owner --title "Factory"
   ```
   *(Requires `gh auth refresh -s project` if your token lacks `project` scope.)*

---

## 4. Verify with `factory doctor`

Run preflight checks and your repo's verification gate:

```bash
factory doctor --repo ~/src/my-app
bash ~/src/my-app/factory/checks.sh
```

`factory doctor` verifies Python, `git`, `gh`, `agy`, `opencode` (if configured), `gpu.mcp_url` (if configured), merged config layers, `factory/checks.sh`, and `.gitignore`. Both commands should exit `0`.

---

## 5. Multi-Repo & Multi-Machine Setup (Optional)

### Register Repositories (`~/.config/factory/repos.json`)

List all repositories on the machine that `--all` commands should manage:

```json
[
  "~/src/my-app",
  "~/src/my-api"
]
```

### Configure Host Limits (`~/.config/factory/host.json`)

Control per-machine concurrency and resource-governor thresholds:

```json
{
  "machine_id": "host-a",
  "max_workers": 2,
  "min_free_ram_gb": 2.0,
  "max_cpu_load_pct": 85.0,
  "ramp_up_delay_sec": 20.0,
  "poll_interval_sec": 45.0
}
```

### Pair Peer Machines (`hc` or `fleet.json`)

With `herdr-core` (`hc`):

```bash
hc machine add user@host-b --name host-b --install
hc machine ls
```

Or without `hc`, list SSH targets in `~/.config/factory/fleet.json`:

```json
["user@host-a", "user@host-b"]
```

---

## 6. Run Your First Issue

1. Open or pick a GitHub issue in `example-owner/my-app` and add the `factory` label (optionally add `Depends on #N` in issue bodies for ordering).
2. Preview the Batch-DAG:
   ```bash
   factory plan --repo ~/src/my-app
   ```
3. Run one-shot or start a continuous worker:
   ```bash
   # One-shot drain:
   factory run --repo ~/src/my-app

   # Or continuous worker across all registered repos:
   factory worker --all

   # Or background worker via hc:
   hc run --bg --name factory-worker -- factory worker --all
   ```
4. Monitor progress:
   ```bash
   factory status --all
   ```

---

## 7. Day-to-Day Operations

| Task | Command |
|---|---|
| Inspect all repos, active machines, & PRs | `factory status --all` |
| View cost/throughput telemetry by tier & provider | `factory stats --all` |
| Inspect failure/gate/review logs for a job | `factory logs <job-id>` |
| Emergency stop across the entire fleet | `factory halt --all --reason "maintenance"` |
| Resume fleet after a halt | `factory resume --all` |
| Pause new claims on this machine | `factory pause` (`factory unpause` to resume) |
| Clean up merged/rejected worktrees | `factory clean --repo ~/src/my-app` |
| Check / wake / pause / update peer machines | `factory fleet status` / `wake` / `pause` / `update --restart` |
