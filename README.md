# Autonomous Software Factory for Google Antigravity (`agy`)

An installable, zero-dependency software factory that turns GitHub issues—from small bugfixes to **complex, multi-file features**—into tested, multi-axis-reviewed Pull Requests using **Google Antigravity (`agy`)**.

Label an issue `factory` and run `/factory run` (inside `agy`) or `./factory/bin/factory run` (headless from any terminal, SSH session, or cron job).

## How It Works

```text
GitHub Issue (`factory` label)
  │
  ▼
intake (sandboxed JSON) ──► ACCEPT / NEEDS_INFO / REJECT + risk + surfaces (ui, security)
  │
  ▼
spec-writer (sandboxed JSON) ──► spec.md + 1..6 ordered vertical TDD slices (plan.json)
  │
  ▼
builder + gate loop (in isolated .worktrees/job-<N> on branch factory/job-<N>)
  ├─ Builds each vertical slice with Red-Green-Refactor TDD at public seams
  ├─ Runs protected_paths diff check + main checkout's factory/checks.sh after every slice
  └─ Self-heals slice gate failures before moving to the next slice
  │
  ▼
parallel reviewers (sandboxed ThreadPoolExecutor)
  ├─ reviewer-code (Standards + Spec fidelity)
  ├─ reviewer-security (routed for security surface; applies ponytail-audit checks)
  └─ reviewer-ux + reviewer-ui (routed for UI surface)
  │  (nit-only reviews pass automatically; actionable findings loop back to builder up to max_rounds)
  ▼
approver (sandboxed JSON)
  ├─ Low risk (or auto_merge="all"): syncs base branch, re-runs gate, auto-merges PR
  └─ Medium/High risk (default auto_merge="low"): marks PR ready (`gh pr ready`) with full evidence for 1-click merge
```

## Key Design Highlights

- **Built for complex features:** Instead of trying to one-shot large features in a single prompt, `spec-writer` decomposes complex issues into 2–6 vertical TDD slices verified individually by `factory/checks.sh` before full-branch review.
- **Deterministic Python orchestrator (`factory/bin/factory`):** Zero third-party Python packages and **no `herdr` dependency**. State transitions, atomic `job.json` updates, resume-after-crash, git worktrees, and `gh` PR mutations live in tested Python code rather than fragile chat loops.
- **Hard safety & trust boundaries:**
  - Issue descriptions and comments are isolated inside escaped `<untrusted_issue_data>` blocks.
  - Non-builder stages (`intake`, `spec-writer`, `reviewers`, `approver`) run with `agy -p --sandbox --output-format json --json-schema`, verified clean against git `HEAD`.
  - `gate` enforces a hardcoded protected-path floor (`factory/`, `.agents/`, plus configured paths like `.github/`) using `--no-renames` diff checks so branches cannot weaken their own test gates.
  - Only the Python runner pushes branches or invokes `gh pr`.
- **Crash-safe & resumable:** Re-running `factory run` resumes interrupted jobs at their exact stage and last completed vertical slice, and automatically resumes `factory:needs-info` / `factory:needs-human` issues once you reply and re-apply the `factory` label.

## Model Routing (`factory/config.json`)

| Role | Default Model | Notes |
|---|---|---|
| `intake` | `gemini-3.8-flash-medium` | Fast triage, risk & surface classification |
| `spec_writer` | `gemini-4-argon-high` | Architecture, domain check & vertical slice planning |
| `builder` | `gemini-4-argon-high` | Slice-by-slice TDD & review-fix implementation |
| `code_low` | `gemini-3.6-flash-low` | Code review for low-risk changes |
| `code_medium` | `gemini-3.8-flash-low` | Code review for medium-risk changes |
| `code_high` | `gemini-3.8-flash-medium` | Code review for high-risk changes |
| `security` | `gemini-4-argon-high` | Security, data-loss & concurrency audit (`ponytail-audit`) |
| `ux` / `ui` | `gemini-3.8-flash-medium` / `gemini-3.6-flash-low` | UX flows, accessibility & visual fit |
| `approver` | `gemini-4-argon-high` | Final readiness check & merge-danger summary |

## CLI & `/factory` Skill Commands

| Command | CLI Equivalent | Purpose |
|---|---|---|
| `/factory run [n]` | `./factory/bin/factory run [n]` | Drain queue or run/resume issue `#n` |
| `/factory status [job-id]` | `./factory/bin/factory status [job-id]` | View live job stages, slice progress, tokens & PR URLs |
| `/factory queue` | `./factory/bin/factory queue` | List open eligible `factory` issues |
| `/factory doctor` | `./factory/bin/factory doctor` | Check git, `gh`, `agy`, config, `checks.sh` & `.gitignore` |
| `/factory research <topic>` | *(interactive in `agy`)* | Primary-source research saved to `docs/research/` |
| `/factory improve [area]` | *(interactive in `agy`)* | Visual HTML architecture review for module deepening |

## Install & Test

See [INSTALL.md](INSTALL.md) to install into any GitHub repository. To run this repository's unit and end-to-end test suite:

```bash
bash factory/checks.sh
```

## Credits & License

Adapted from [ZazenCodes' Software Factory V2](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2) with engineering workflows inspired by [mattpocock/skills](https://github.com/mattpocock/skills). Licensed under GPLv3.
