# Setup & Installation Guide (`software-factory-agy` v0.2.0)

This guide walks you through installing `software-factory-agy` on one machine or across a multi-machine homelab/workstation fleet.

---

## Prerequisites

- **Python 3.10+** (standard library only; zero third-party runtime dependencies)
- **Git** and **GitHub CLI (`gh`)** authenticated (`gh auth status`) with repo push & PR permissions
- **Antigravity CLI (`agy`)** installed and authenticated on `PATH`
- *(Optional, recommended for multi-machine fleets)* **[`herdr-app` + `herdr-core` (`hc`)](https://herdr.dev/)**:
  - macOS / Linux: `curl -fsSL https://releases.herdr.dev/app/install.sh | sh`
  - Windows: `irm https://releases.herdr.dev/app/install.ps1 | iex`
  - Headless Linux worker nodes only need `hc` (automatically installed by `hc machine add user@host --name box --install`).

---

## 1. Install the Central `factory` CLI

Clone this repository and install it in editable mode on each machine that will run factory jobs:

```bash
git clone https://github.com/melcutz/software-factory-agy.git
cd software-factory-agy
pip install -e .
```

Verify `factory` is on `PATH`:

```bash
factory --help
```

*(If you also want `/factory` slash commands inside `agy`, copy `.agents/skills/factory/` into your target repository or global skills directory.)*

---

## 2. Configure Each Target Repository

In each repository you want the factory to manage:

1. **Create `factory/checks.sh`** with your deterministic, non-interactive test/lint/typecheck suite and make it executable:
   ```bash
   chmod +x factory/checks.sh
   ```
   `checks.sh` receives `FACTORY_GATE_SCOPE=slice|full` and `FACTORY_BASE_BRANCH=<branch>`, must exit non-zero on failure, and must not rewrite tracked files.

2. **Create `factory/config.json`** (you can copy [factory/config.json](factory/config.json) as a starting point):
   - Set `base_branch` (`"main"`, `"master"`, etc.) and `merge_method` (`"merge"`, `"squash"`, or `"rebase"`).
   - Set `auto_merge`:
     - `"low"` *(default)*: Auto-merge approved `low`-risk PRs; mark `medium`/`high`-risk PRs ready (`gh pr ready`).
     - `"all"`: Auto-merge all approved PRs.
     - `"none"`: Stop all approved PRs at `ready` for human merge.
   - *(Optional)* Enable `staging_qa` if your repo uses a staging deploy train + `factory/staging_hooks.py`.

3. **Update `.gitignore`** in the target repo:
   ```gitignore
   /factory/jobs/*
   !/factory/jobs/.gitkeep
   /.worktrees/
   ```

4. **Create GitHub Labels** on the target repo:
   ```bash
   gh label create "factory" --color "0E8A16" --description "Queued for autonomous software factory" --force
   gh label create "factory:needs-info" --color "FBCA04" --description "Factory waiting for clarification" --force
   gh label create "factory:needs-human" --color "D93F0B" --description "Factory escalated for human review" --force
   ```

5. **Run Preflight Checks:**
   ```bash
   factory doctor --repo /path/to/your-repo
   bash /path/to/your-repo/factory/checks.sh
   ```
   Both must exit `0`.

6. *(Optional)* **Create a GitHub ProjectV2 Board:**
   ```bash
   factory init-board --repo /path/to/your-repo
   ```
   *(Requires `gh auth refresh -s project` if your token lacks `project` scope.)*

---

## 3. Configure Host & Multi-Repo Settings (Optional, for `--all` / Multi-Machine)

### Register Repositories (`~/.config/factory/repos.json`)
List all repositories on this machine that `factory worker --all` / `factory status --all` / `factory halt --all` should manage:

```json
[
  "~/work/repo-a",
  "~/work/repo-b"
]
```

### Tune Machine Resource Limits (`~/.config/factory/host.json`)
Prevent the factory from hogging shared desktops/laptops:

```json
{
  "machine_id": "ryzen-box",
  "max_workers": 2,
  "min_free_ram_gb": 2.0,
  "max_cpu_load_pct": 85.0,
  "ramp_up_delay_sec": 20.0,
  "poll_interval_sec": 45.0
}
```
If free RAM drops below `min_free_ram_gb` or 1-minute CPU load exceeds `max_cpu_load_pct`, the machine stops claiming new jobs until resources recover.

### Pair Remote Machines via `hc` (Optional)
On your primary workstation (e.g., over Tailscale SSH or WSL):

```bash
hc machine add user@100.64.0.2 --name ryzen-box --install
# Or for Windows WSL2 without an SSH server:
hc machine add --via 'wsl.exe -d Ubuntu -e sh -c' --name wsl
hc machine ls
```

*(If you don't use `hc`, you can list SSH targets in `~/.config/factory/fleet.json` as `["user@host-a", "user@host-b"]`.)*

---

## 4. Run It!

### Option A: Single-Machine One-Shot Run
Label one or more GitHub issues `factory` (optionally adding `Depends on #N` in issue bodies), then run:

```bash
factory run --repo /path/to/your-repo
# Or run specific issues:
factory run --repo /path/to/your-repo 42 43
```

### Option B: Durable Multi-Machine Fleet (with `herdr-core` / `herdr-app`)
Start a persistent worker on each machine (or wake them all remotely):

```bash
# On one machine inside hc:
hc run --bg --name factory-worker -- factory worker --all

# Or wake every machine in `hc machine ls` from your laptop:
factory fleet wake
```

Open **`herdr-app`** (or run `hc ls` / `hc attach <name>` in a terminal) to see every local and remote worker in the **Agents sidebar** (**Working / Blocked / Done / Idle**), click any job to **peek** into its live logs, or intervene directly in `.worktrees/job-<N>` if a job hits `needs-human`.

---

## 5. Day-to-Day Operations

| Task | Command |
|---|---|
| Inspect all repos, active machines, & PRs | `factory status --all` |
| Emergency stop across the entire fleet | `factory halt --all --reason "lunch break"` |
| Resume fleet after a halt | `factory resume --all` |
| Pause new claims on just this machine | `factory pause` (`factory unpause` to resume) |
| Check / wake / pause peer machines | `factory fleet status` / `factory fleet wake` / `factory fleet pause <name>` |
