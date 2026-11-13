# Autonomous Software Factory for Google Antigravity (`agy`) — Design Specification

**Date:** 2026-11-13  
**Status:** Draft for user review

---

## 1. Goal & Scope

Transform `software-factory-agy` from a broken Herdr/OpenCode port into a self-contained, zero-dependency autonomous software factory that turns GitHub issues—including **complex, multi-file features**—into tested, reviewed, production-quality Pull Requests with minimal human intervention.

### Design Objectives
1. **Zero terminal-multiplexer fragility:** Remove the `herdr` runtime dependency. Run anywhere `python3` (3.10+), `git`, `gh`, and `agy` are installed (interactive `agy`, plain terminal, SSH, background jobs, cron).
2. **Deterministic orchestration:** Move the pipeline loop, state machine, worktree management, and GitHub mutations out of LLM chat prompts into a unit-tested Python CLI (`factory/bin/factory`).
3. **Complex-feature reliability via vertical TDD slices:** Decompose features into 1–6 ordered vertical slices during spec generation. Build, commit, and pass `checks.sh` slice-by-slice before running parallel whole-branch reviews.
4. **Hard trust & safety boundaries:** Enforce structured outputs via `agy -p --output-format json --json-schema`, sandbox read-only stages (`--sandbox`), block edits to protected paths in `gate`, and restrict `git push` / `gh pr` mutations to deterministic Python code.
5. **Tiered merge autonomy:** Default to auto-merging passing `low`-risk PRs while promoting passing `medium`/`high`-risk complex features to green, ready-to-merge PRs (`gh pr ready`) with full verification summaries (configurable via `auto_merge: "low" | "all" | "none"`).

### Out of Scope
- Multi-machine distributed job queues or external databases (all state lives on disk in `factory/jobs/`).
- Automated production deployments after PR merge.
- Third-party Python dependencies (uses Python 3.10+ standard library exclusively).

---

## 2. Installed Architecture & File Layout

```text
.agents/
  skills/
    factory/
      SKILL.md                 # Thin agy skill: /factory run [n], status, doctor, improve, research
factory/
  bin/
    factory                    # Executable Python CLI: run | status | queue | gate | doctor
  lib/
    __init__.py
    config.py                  # Loads and validates factory/config.json
    schemas.py                 # JSON Schemas + markdown renderers for each agent stage
    state.py                   # Atomic job.json state machine, transitions, rounds, slice progress, usage
    git_ops.py                 # Git worktrees, branch sync, protected-path diff checks, gh CLI wrapper
    gate_runner.py             # Deterministic checks.sh runner + protected_paths enforcement
    agy_runner.py              # Invokes agy -p --output-format json --json-schema, logs & usage capture
    pipeline.py                # Single-job state machine + ThreadPoolExecutor queue & reviewer runners
  agents/
    intake.md                  # Triage: ACCEPT / NEEDS_INFO / REJECT + risk + surfaces
    spec-writer.md             # Spec + domain check + 1..6 ordered vertical TDD slices
    builder.md                 # Implements one vertical slice or review/gate fix round via TDD
    reviewer-code.md           # Two-axis review (repo standards + spec fidelity)
    reviewer-security.md       # Security/data-loss/race review (invokes ponytail-audit when installed)
    reviewer-ux.md             # User flow, accessibility, responsiveness, consistency review
    reviewer-ui.md             # Visual fit, layout, empty/loading/error states, dark mode review
    approver.md                # Final safety/spec check + merge-danger assessment
    researcher.md              # Interactive/on-demand primary-source research (/factory research)
    architect.md               # Interactive codebase deepening review (/factory improve)
  config.json                  # Single source of truth for models, limits, labels, and merge policy
  checks.sh                    # Project-specific deterministic test/typecheck/lint gate
tests/                         # Template repo unit & integration tests (stdlib unittest)
```

> **Cleanup from v0.1:** The four disconnected scripts (`factory/bin/job`, `factory/bin/route`, `factory/bin/gate`, `factory/bin/spawn-agent`) are replaced by `factory/bin/factory` and `factory/lib/*`.

---

## 3. Configuration (`factory/config.json`)

All routing, limits, labels, and model assignments live in one non-duplicated structure:

```json
{
  "concurrency": 3,
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

### Configuration Rules
- `merge_method`: `"merge" | "squash" | "rebase"` (passed as `--merge`, `--squash`, or `--rebase` to `gh pr merge`).
- `auto_merge`:
  - `"low"` *(default)*: Auto-merge `APPROVE` verdicts when `risk == "low"`; mark `medium`/`high`-risk `APPROVE` PRs ready (`gh pr ready`) with verification evidence and move job to `ready`.
  - `"all"`: Auto-merge all `APPROVE` verdicts regardless of risk level.
  - `"none"`: Never auto-merge; all `APPROVE` verdicts stop at `ready`.
- `protected_paths`: Prefix/glob patterns checked against `git diff --name-only origin/<base_branch>..HEAD`. Regardless of user config, `factory/checks.sh`, `factory/config.json`, `factory/bin/`, `factory/lib/`, and `.agents/` are always enforced as a hardcoded safety floor.

---

## 4. Job State Machine & On-Disk Artifacts

All runtime state for issue `<N>` lives in `factory/jobs/job-<N>/` (gitignored) and `.worktrees/job-<N>/` (gitignored):

```text
factory/jobs/job-42/
  job.json                     # Atomic state record (written via tempfile + os.replace)
  issue.md                     # Issue title, URL, body, and comments fetched via gh
  intake.md                    # Rendered from intake structured output
  spec.md                      # Rendered from spec-writer structured output
  plan.json                    # Ordered vertical slices produced by spec-writer
  logs/                        # Raw JSON envelopes & stderr per stage invocation
  round-1/
    slice-1-build.md           # Build summary per vertical slice (Round 1)
    build.md                   # Round-level build summary
    checks.log                 # Full deterministic output of checks.sh + protected_paths
    checks.md                  # Gate verdict + last 120 lines of checks.log
    review-code.md             # Code reviewer report
    review-security.md         # Security reviewer report (when routed)
    review-ux.md               # UX reviewer report (when routed)
    review-ui.md               # UI reviewer report (when routed)
    approve.md                 # Approver report
```

### Legal Stage Transitions
| Current Stage | Allowed Next Stages | Trigger |
|---|---|---|
| `intake` | `spec`, `needs-info`, `rejected`, `needs-human` | Intake verdict or unrecoverable error |
| `needs-info` | `intake`, `rejected` | Human removes `needs_info` label and re-queues `factory` |
| `spec` | `build`, `needs-human` | Spec + vertical slice plan written |
| `build` | `gate`, `needs-human` | All slices (Round 1) or review fixes (Round > 1) committed; or `BLOCKED` |
| `gate` | `review`, `build`, `needs-human` | Gate `PASS` → `review`; Gate `FAIL` → retry/fix or escalate |
| `review` | `approve`, `build`, `needs-human` | All reviewers `PASS` → `approve`; `CHANGES` & `round < max_rounds` → `build`; else `needs-human` |
| `approve` | `merging`, `ready`, `needs-human` | `APPROVE` + policy auto-merges → `merging`; `APPROVE` + manual merge → `ready`; `ESCALATE` → `needs-human` |
| `merging` | `merged`, `needs-human` | Base-branch sync + final gate + `gh pr merge` succeeds → `merged`; conflict/gate failure → `needs-human` |
| `needs-human` | `build`, `rejected` | Human removes `needs_human` label and re-queues `factory` |
| `ready` | `merged` | Terminal for factory run (green PR ready for human merge) |
| `merged` | *(none)* | Terminal |
| `rejected` | *(none)* | Terminal |

### `job.json` Fields
`id`, `issue`, `issue_url`, `issue_title`, `branch` (`factory/job-<N>`), `stage`, `round` (monotonic int starting at `0`, incremented only on stage transitions *into* `build` so `round-<R>/` directories are never overwritten; resuming an already-`build` job keeps `round` unchanged), `round_limit` (initialized to `config.max_rounds`; bumped to `max(round_limit, round + 1)` on `needs-human -> build`), `risk`, `surfaces` (`list[str]`), `total_slices` (int), `completed_slices` (int), `pr_number` (`int | null`), `pr_url` (`str | null`), `usage` (`input_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens`, `duration_seconds`), `last_error` (`str | null`), `created`, `updated`.

---

## 5. End-to-End Pipeline Flow

### 5.1 Queue Discovery & Resume (`factory run [issue]`)
1. `queue` queries `gh issue list --label <queue> --state open --json number,title,url,labels`.
2. Issues carrying `labels.needs_info` or `labels.needs_human` are skipped automatically.
3. For each eligible issue (up to `concurrency` workers in `ThreadPoolExecutor`):
   - `issue.md` is refreshed from `gh issue view <N> --comments --json number,title,body,url,comments` so human replies on previously paused issues are visible.
   - If `job.json` does **not** exist, initialize at `stage = "intake", round = 0`.
   - If `job.json` **does** exist:
     - If `stage` is `needs-info` (human answered and re-labeled), advance `needs-info -> intake`.
     - If `stage` is `needs-human` (human replied/unblocked and re-labeled), set `round_limit = max(round_limit, round + 1)` and advance `needs-human -> build`.
     - If `stage` is `merged`, `ready`, or `rejected`, remove the `factory` queue label if still present and skip.
     - Otherwise resume directly from the persisted `stage` and `completed_slices` without incrementing `round`.
4. Ensure git worktree exists: `git fetch origin <base_branch>` and, if `.worktrees/job-<N>` is absent, `git worktree add -B factory/job-<N> .worktrees/job-<N> origin/<base_branch>`.

### 5.2 Stage 1 — `intake` (Read-Only, Sandboxed)
- Runs `agy` in `.worktrees/job-<N>` with `--sandbox`. Issue text in `issue.md` is explicitly framed inside `<untrusted_issue_data>` tags.
- Unlike v0.1, `intake` **accepts complex features** as long as requirements are clear enough to specify and implement inside this repository; it only returns `REJECT` for out-of-project/duplicate/impossible requests, and `NEEDS_INFO` only when a product decision cannot be inferred from the codebase and issue.
- On `NEEDS_INFO`: posts numbered questions to GitHub, swaps labels (`--remove-label <queue> --add-label <needs_info>`), transitions to `needs-info`.
- On `REJECT`: posts reasoning comment, removes `<queue>` label, closes issue, transitions to `rejected`.
- On `ACCEPT`: persists `risk` (`low|medium|high`) and `surfaces` (`[]` subset of `["ui", "security"]`), writes `intake.md`, transitions to `spec`.

### 5.3 Stage 2 — `spec` (Read-Only, Sandboxed)
- Runs `spec-writer` in `.worktrees/job-<N>` with `--sandbox`.
- Inspects codebase, `AGENTS.md`/`GEMINI.md`, `GLOSSARY.md`, and `docs/adr/` without mutating files. Any needed glossary or ADR additions are recorded in `glossary_updates` inside the JSON output so `builder` commits them cleanly on the branch.
- Decomposes implementation into **1 to 6 vertical TDD slices** (`plan.json`):
  - Simple bugfix / small feature: **1 slice**.
  - Complex feature: **2–6 slices**, ordered by dependency so every slice has testable behavior at public seams and leaves the test suite passing.
- Writes `spec.md` and `plan.json`, sets `total_slices = len(slices)`, `completed_slices = 0`, transitions to `build`.

### 5.4 Stage 3 & 4 — `build` + `gate` (Vertical Slice Loop & Fix Rounds)
- **Round 1 (`completed_slices < total_slices`):**
  - Loops `idx` from `completed_slices` to `total_slices - 1`:
    1. Runs `builder` in `.worktrees/job-<N>` with `spec.md`, `slices[idx]`, and summaries of prior slices. Builder follows Red-Green-Refactor TDD and commits locally on `factory/job-<N>`.
    2. If `builder` returns `BLOCKED`, escalates immediately to `needs-human`.
    3. If uncommitted changes remain in `.worktrees/job-<N>` or `HEAD` did not advance on slice 1, treats as an incomplete build and triggers a slice fix retry.
    4. Runs `gate` (`protected_paths` check + `checks.sh` in `.worktrees/job-<N>`).
    5. If `gate` fails, retries `builder` on the same slice with `checks.md` up to `max_slice_retries` (default 2). If still failing, escalates to `needs-human`.
    6. Once `gate` passes for slice `idx`, writes `round-1/slice-<idx+1>-build.md` and persists `completed_slices = idx + 1` in `job.json`.
  - After the final slice passes `gate`, advances `build -> gate -> review` (recording final `round-1/checks.md`), pushes `origin factory/job-<N>`, and opens a draft PR (`gh pr create --draft`), saving `pr_number` and `pr_url` in `job.json`.
- **Round > 1 (Review-Fix Rounds):**
  - Runs `builder` in `.worktrees/job-<N>` with `spec.md` and actionable `CHANGES` findings (`must_fix` and `should_fix`) from Round `R - 1`.
  - Advances `build -> gate` and runs `gate` (allowing up to `max_slice_retries` gate-fix retries if `checks.sh` fails).
  - On `gate` `PASS`: pushes `origin factory/job-<N>`, updates PR body, advances `gate -> review`.
  - On `gate` `FAIL` after retries: if `round < round_limit`, advances `gate -> build`; otherwise transitions to `needs-human`.

### 5.5 Stage 5 — `review` (Parallel, Read-Only, Sandboxed)
- Computes active reviewers deterministically:
  - Always: `code` using `models["code_" + risk]`.
  - If `"security"` in `surfaces`: `security` using `models["security"]` (instructed to apply `ponytail-audit` checks on the diff and connected paths).
  - If `"ui"` in `surfaces`: `ux` (`models["ux"]`) and `ui` (`models["ui"]`).
- Runs all routed reviewers in parallel via `ThreadPoolExecutor` in `.worktrees/job-<N>` with `--sandbox`. After all finish, verifies `git status --porcelain` is empty (discarding any accidental untracked artifacts).
- **Anti-churn rule:** Each finding carries `severity` (`must_fix | should_fix | nit`). A reviewer verdict is treated as `CHANGES` **only** if at least one finding has `severity` in `{"must_fix", "should_fix"}`; nit-only reviews normalize to `PASS`.
- If all reviewers `PASS`: transitions `review -> approve`.
- If any reviewer has actionable `CHANGES`:
  - If `round < round_limit`: transitions `review -> build`.
  - Else: transitions `review -> needs-human`, comments unresolved findings on PR/issue, swaps labels `-<queue> +<needs_human>`.

### 5.6 Stage 6 — `approve` & Merge Delivery
- Runs `approver` (`--sandbox`) with `intake.md`, `spec.md`, `round-<R>/build.md`, `round-<R>/checks.md`, `round-<R>/review-*.md`, and `git diff --stat origin/<base_branch>..HEAD`.
- `approver` evaluates correctness, spec completeness, and safety (`APPROVE` vs `ESCALATE`) **independently of the intake risk tier**.
- If `ESCALATE`: transitions `approve -> needs-human`, posts `approver`'s specific question on the issue/PR, swaps labels `-<queue> +<needs_human>`.
- If `APPROVE`:
  - Updates PR title & body with Summary, Verification Evidence (`checks.sh` + reviewer verdicts), and Merge-Danger assessment.
  - **Auto-merge path** (`auto_merge == "all"` or `(auto_merge == "low" and risk == "low")`):
    1. Transitions `approve -> merging`.
    2. Runs `git fetch origin <base_branch>` and merges `origin/<base_branch>` into `factory/job-<N>` (if merge conflict occurs, aborts merge and escalates to `needs-human`).
    3. If `HEAD` changed during sync, re-runs `gate` and pushes `factory/job-<N>` (if `gate` fails after sync, escalates to `needs-human`).
    4. Marks PR ready (`gh pr ready <pr_number>`) and merges (`gh pr merge <pr_number> --<merge_method> --delete-branch`).
    5. Removes `<queue>` label from issue if still open, transitions `merging -> merged`.
  - **Ready-for-review path** (Option A for `medium`/`high` risk under `auto_merge == "low"`, or any risk under `auto_merge == "none"`):
    1. Marks PR ready (`gh pr ready <pr_number>`).
    2. Removes `<queue>` label from the issue and posts a concise completion comment linking the ready PR.
    3. Transitions `approve -> ready`.

---

## 6. Headless `agy` Invocation Contract (`agy_runner.py`)

Verified against the installed `agy` CLI:
- Command form:
  ```bash
  agy -p <full_prompt> \
    --model <model_id> \
    --output-format json \
    --json-schema <compact_schema_json> \
    --print-timeout <agent_timeout_sec>s \
    --dangerously-skip-permissions \
    [--sandbox]
  ```
- **Do NOT pass `--mode plan`:** In `agy`, `--mode plan` enters interactive Planning Mode (creating `implementation_plan.md` approval artifacts) rather than read-only execution. Read-only stages enforce isolation via `--sandbox` plus post-run worktree cleanliness verification (`git status --porcelain` & `HEAD` SHA check).
- **Subagent preamble:** Every headless prompt begins with a `<SUBAGENT-STOP>` headless worker preamble instructing the agent that it is a dispatched non-interactive subagent executing one specific stage and must never invoke interactive brainstorming or pause to ask a human questions.
- **Envelope parsing:** Parses stdout JSON envelope (`status == "SUCCESS"`), extracts `payload["structured_output"]` (validating required keys and enums in Python), accumulates `payload.get("usage", {})` + `payload.get("duration_seconds", 0)` into `job.json["usage"]`, and saves the full JSON response to `factory/jobs/job-<N>/logs/round-<R>-<stage>.json`. Retries once on transient CLI/JSON parse failure before surfacing an error.

---

## 7. CLI & Skill Surface

### `factory/bin/factory` Subcommands
- `factory run [issue_number]` — Drain all open queued issues (up to `concurrency`), or run/resume a single issue number. Prints concise timestamped stage transitions to stdout.
- `factory queue` — List eligible open `factory`-labeled issues (excluding `needs_info` / `needs_human`).
- `factory status [job_id]` — Print a summary table of all jobs (stage, round, slice progress, risk, PR URL, token usage), or detailed status for one job.
- `factory gate <job_id> [worktree_path] [round]` — Run `protected_paths` + `checks.sh` deterministically and write `checks.md`.
- `factory doctor` — Verify prerequisites (`python3 >= 3.10`, `git` repo + `origin`, `gh auth status`, `agy --version`, valid `factory/config.json`, executable `factory/checks.sh`, `.gitignore` entries for `/factory/jobs/` and `/.worktrees/`).

### `.agents/skills/factory/SKILL.md` Commands
- `/factory run` / `/factory run <n>` — Runs `factory/bin/factory run [n]` (supports foreground or background execution in `agy`) and summarizes resulting PRs / escalations.
- `/factory status` — Runs `factory/bin/factory status`.
- `/factory doctor` — Runs `factory/bin/factory doctor`.
- `/factory research <topic>` — Follows `factory/agents/researcher.md` directly inside the `agy` session (or via `agy`'s `research` subagent) and saves cited primary-source findings to `docs/research/`.
- `/factory improve [area]` — Follows `factory/agents/architect.md` interactively inside the current `agy` session so the user can review the generated HTML report and walk the decision tree live.

---

## 8. Testing & Verification Strategy

All tests live in `tests/` using Python's built-in `unittest` and `tempfile`:
1. **`test_config_and_schemas.py`** — Config defaults/validation, reviewer routing (`low`/`medium`/`high` × `[]`/`["ui"]`/`["security"]`/`["ui", "security"]`), nit-only review normalization, JSON schema completeness.
2. **`test_state.py`** — Legal/illegal stage transitions, atomic `job.json` persistence, resume rules (`needs-info -> intake`, `needs-human -> build`, idempotent re-entry mid-slice), usage accumulation.
3. **`test_git_and_gate.py`** — Real local bare `origin` + main checkout + worktree creation (`factory/job-42`), `checks.sh` pass/fail + 120-line log truncation, `protected_paths` failure when `factory/checks.sh` or `.github/workflows/ci.yml` is modified on the branch, clean base-branch merge sync + conflict detection.
4. **`test_pipeline_e2e.py`** — End-to-end `pipeline.py` runs with stubbed `agy` and `gh` executables on `PATH` against a real temporary git repo:
   - **Complex 3-slice medium-risk feature (`auto_merge="low"`):** Slices 1–3 commit & pass gate (including a simulated gate failure + self-heal retry on slice 2), parallel `code` + `security` + `ux` + `ui` reviews (1 review-fix round), `approver` `APPROVE` → PR marked ready, queue label removed, ends in `stage == "ready"`.
   - **Low-risk single-slice fix (`auto_merge="low"`):** Auto-syncs `main`, passes final gate, runs `gh pr merge`, ends in `stage == "merged"`.
   - **Crash & resume mid-slice:** Simulated failure after slice 1 of 2; second `factory run` resumes at slice 2 without re-running `intake`, `spec`, or slice 1.
   - **`NEEDS_INFO` & human reply resume:** Issue labeled `factory:needs-info`, skipped on next poll, then resumed cleanly once re-labeled `factory`.
   - **`max_rounds` exhaustion:** Escalates cleanly to `needs-human` with label swap and summary comment.
