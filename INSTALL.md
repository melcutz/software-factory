# Install the Software Factory (Google Antigravity / `agy` Edition)

## Prerequisites

- Git repository with `origin` on GitHub and `gh auth status` authenticated with push access.
- `agy --version` installed and authenticated on `PATH`.
- `python3 --version` 3.10+ (standard library only; zero `pip` packages required).

## 1. Copy Into Target Repository

Copy the skill and `factory/` directory into the root of your repository:

```text
.agents/skills/factory/SKILL.md
factory/bin/factory
factory/lib/__init__.py
factory/lib/config.py
factory/lib/schemas.py
factory/lib/state.py
factory/lib/git_ops.py
factory/lib/gate_runner.py
factory/lib/agy_runner.py
factory/lib/pipeline.py
factory/lib/planner.py
factory/lib/project_board.py
factory/agents/planner.md
factory/agents/intake.md
factory/agents/spec-writer.md
factory/agents/builder.md
factory/agents/reviewer-code.md
factory/agents/reviewer-security.md
factory/agents/reviewer-ux.md
factory/agents/reviewer-ui.md
factory/agents/approver.md
factory/agents/researcher.md
factory/agents/architect.md
factory/config.json
factory/checks.sh
factory/jobs/.gitkeep
```

Ensure executables have `+x`:

```bash
chmod +x factory/bin/factory factory/checks.sh
```

## 2. Configure for Your Stack

1. **`factory/checks.sh`**: Replace the template test command with your project's deterministic, non-interactive verification suite (e.g. `npm run typecheck && npm test`, `pytest`, `cargo test --all-targets`). It receives `FACTORY_GATE_SCOPE=slice|full` and `FACTORY_BASE_BRANCH=<branch>` so multi-slice builds can optionally run targeted tests on intermediate slices before running the full suite on the final slice. It must exit non-zero on any failure and must not rewrite tracked files.
2. **`factory/config.json`**:
   - Set `base_branch` (`"main"`, `"master"`, etc.) and `merge_method` (`"merge"`, `"squash"`, or `"rebase"`).
   - Set `auto_merge`:
     - `"low"` *(default)*: Auto-merge passing `low`-risk PRs; mark passing `medium`/`high`-risk PRs ready (`gh pr ready`) for 1-click human merge.
     - `"all"`: Auto-merge all approved PRs regardless of risk tier.
     - `"none"`: Never auto-merge; stop every approved PR at `ready`.
   - Adjust `concurrency` (default `4`), `models` (including `planner`), `auto_polish`, and optional `model_pricing` overrides if desired.
3. **`.gitignore`**: Append:
   ```gitignore
   /factory/jobs/*
   !/factory/jobs/.gitkeep
   /.worktrees/
   ```
4. **GitHub Labels**: Create `factory`, `factory:needs-info`, and `factory:needs-human` on your GitHub repository:
   ```bash
   gh label create "factory" --color "0E8A16" --description "Queued for autonomous software factory" --force
   gh label create "factory:needs-info" --color "FBCA04" --description "Factory waiting for clarification" --force
   gh label create "factory:needs-human" --color "D93F0B" --description "Factory escalated for human review" --force
   ```
5. **GitHub Project Board (Optional)**: If you want a Kanban board (`Inbox`, `Ready`, `Building`, `Review`, `Done`, `Deployed`, `Skipped`, `Needs Human`) that automatically tracks issues as the factory executes them:
   ```bash
   ./factory/bin/factory init-board
   ```
   *(Requires `gh auth refresh -s project` if your token does not yet include the `project` scope. Whenever a `"Factory"` project exists in your GitHub account, the runner syncs issue columns automatically.)*

## 3. Verify Installation

Run the built-in doctor check and your gate script:

```bash
./factory/bin/factory doctor
bash factory/checks.sh
```

Both commands must exit `0`.

## 4. Run

- Label any GitHub issue `factory` (from small bugfixes to complex multi-file features). Use `Depends on #N`, `Blocked by #N`, or `After #N` in issue bodies to sequence dependent work.
- Inside `agy`, run `/factory run` (or run `./factory/bin/factory run` / `./factory/bin/factory run 42 43` directly from any terminal, SSH session, or cron job).
- Check runner liveness, per-job activity/PID, slice progress, cost, and PR URLs anytime with `/factory status` or `./factory/bin/factory status`.
