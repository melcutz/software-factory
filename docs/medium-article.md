# Building an Autonomous Software Factory: Why 66% of Our Wait Time Wasn't LLMs (and How We Cut PR Cost 35x)

*Lessons, telemetry, and production war stories from shipping 29 multi-file PRs autonomously on a live SaaS codebase.*

Most conversations about AI coding agents obsess over prompts, context windows, and which frontier model writes slightly cleaner Python.

When we built an autonomous software factory ([`software-factory-agy`](https://github.com/melcutz/software-factory)) and pointed it at a real production SaaS codebase (`example-app` -- a multi-tenant FastAPI + Cloudflare Python Workers + D1 + Queues + Stripe + Workers AI app with 3,264 tests and 8 CI invariant gates), **the bottlenecks that actually mattered had almost nothing to do with prompt engineering.**

Across **36 queued GitHub issues**, the factory shipped **29 merged multi-file PRs (70 vertical slices)**, rejected 1 out-of-scope issue cleanly in 25 seconds for `$0.00`, and processed **217.2M tokens** across **10.45 hours** of cumulative model time.

Here is what the numbers taught us -- and the 10 hard-won engineering lessons that turned a fragile prototype into an unattended delivery pipeline.

---

## The Architecture in 60 Seconds

Every GitHub issue moves through a gated state machine inside an isolated `git worktree`:

1. **Intake & Spec:** Triage the issue (`rejected` / `needs-human` / `proceed`), classify risk (`low` / `medium` / `high`), and slice it into vertical deliverables (`spec.json`).
2. **Vertical-Slice Build:** Implement one slice at a time and pass the deterministic CI gate (`checks.sh`).
3. **Routed Multi-Persona Review:** Run specialized reviewers in parallel (`code`, `security`, `ui`, `ux`), routed by risk and changed surfaces. Clean low/medium Round-1 PRs auto-approve deterministically; high-risk or multi-round PRs go to an `approver` agent.
4. **Coalesced Staging Deploy + Live Browser QA:** Merge PRs onto `main` via a staging deploy train, deploy to live Cloudflare Workers staging, and run an autonomous Playwright QA agent across 4 user personas (`pro`, `smoke`, `fresh_signup`, `anonymous`).
5. **Auto-Rollback on Failure:** If live browser QA fails, `git revert` `main`, roll back the Cloudflare Workers, hand the failure screenshot + HTTP logs back to Builder on a `-qa2` branch, and retry automatically.

*[Insert Diagram: Factory Pipeline & Staging Train Architecture -- export from `docs/lessons-learned.md`]*

---

## By the Numbers: 36 Real Issues in Production

- **Total issues processed:** 36 jobs (`29` merged to `main`, `1` rejected at intake, `6` in-flight / queued in Wave 2)
- **Wave 1 completion rate:** `22 / 22` accepted issues merged (`100%`)
- **Vertical slices shipped:** `70` slices (`1` to `5` per issue; median `2`)
- **Total tokens processed:** `217.2M` (`26.5M` uncached input, **`188.2M` cache reads**, `2.5M` output, `1.2M` thinking)
- **Prompt-cache hit rate:** **`87.7%`**
- **Cost per PR (early all-frontier baseline):** `$7.03` - `$14.25` (`$21.28` for 2 PRs)
- **Cost per PR (after role-based model tiering, 19 Wave 1 PRs):** **`$0.27` median** (`$0.05` - `$0.51`; **`$4.97` total for all 19 PRs combined**)
- **Full 8-step CI gate (`3,264` tests):** **`~6m 30s` -> `1m 12.5s`** (**5.4x faster**)
- **Post-approve (`merging` + `staging_qa`) median wall-clock:** **`24.4 min` -> `3.0 - 4.9 min`** (**5x - 8x faster**)
- **End-to-end job median wall-clock:** **`52.2 min` -> `18.9 - 22.3 min`** (**~2.5x faster**)

---

## 10 Hard-Won Lessons from Running It Live

### 1. Most of Your Wall-Clock Time Isn't LLM Inference -- It's Tests, Deploys, and Locks
When Wave 1 felt slow (~50-65 minutes per ticket), our first instinct was to blame LLM latency. Then we measured timestamps across 14 uninterrupted Wave 1 jobs:

- **Median end-to-end wall-clock:** `52.2 minutes`
- **Median total LLM time across every stage combined:** **`16.3 minutes` (`33.4%`)**
- **Median non-LLM time (tests, deploys, locks):** **`35.9 minutes` (`66.6%`)**

On smaller 2-slice fixes, LLMs were just **`14.7% - 17.2%`** of wall-clock time. After a PR was approved, Playwright QA's LLM turn took only **`2.9 minutes`**, while **`21.5 minutes`** went to waiting on locks, running `pre-merge` gates, bundling two Cloudflare Python Workers, and running D1 migrations one PR at a time.

**What fixed it:**
- **Adaptive `pytest-xdist` (`--dist loadfile`):** Dynamically budgeting CPU workers across concurrent gates cut full `checks.sh` (`3,264` tests) from **`6m 30s` to `1m 12.5s` (5.4x faster)**.
- **Diff-aware `pre-merge` gates:** Disjoint upstream merges with no shared-infra changes run a slice-scoped gate in **`< 4 seconds`** instead of re-running all 3,264 tests.
- **Selective staging deploy flags:** Skip staging deploy + QA for docs/tests-only PRs, and pass `--web-only --skip-migrations` when ingest Worker code and D1 SQL are untouched.
- **Coalesced Staging Deploy Train:** Batch up to `4` ready PRs onto `main`, deploy to Cloudflare staging **once**, and fan out per-PR Playwright QA in parallel. If 1 PR fails QA, surgically `git revert` just that culprit commit and re-deploy the other 3.

Result: post-approve time dropped from `24.4 min` to **`3.0 - 4.9 min`**, cutting total end-to-end delivery for clean 2-slice PRs to **`18.9 - 22.3 min`**.

### 2. Right-Sizing Models + Prompt Caching Cut PR Cost by 35x (`$14.25` -> `$0.27`)
Our first three jobs ran a frontier reasoning model across every stage. One 2-slice security PR burned `82 minutes` of model time, `33.2M` cached tokens, and **`$14.25` on a single PR**.

Per-step token and dollar telemetry made the fix obvious:
- **Match model tier to cognitive load:** Keep frontier reasoning where architectural judgment matters (`spec` on complex tickets, `security` review, `approver` on `risk == "high"`), and route structured execution (`intake`, vertical-slice `builder`, `code`/`ux`/`ui` review, Playwright `qa`) to fast mid-tier models.
- **Prepend a static ~90-line `REPO_MAP.md`:** Eliminated 6-10 cold-start `find`/`grep` turns per stage and drove our **`87.7%` prompt-cache hit ratio** (`188.2M` cache reads).
- **Deterministic Round-1 auto-approval (`risk != "high"`):** If tests pass and every reviewer returns `PASS` with zero findings, don't pay an LLM `approver` to rubber-stamp `APPROVE`.

Across the next **19 consecutive merged PRs (49 slices)**, total LLM spend was **`$4.97` combined -- a `$0.27` median per PR** with a `100%` merge rate.

### 3. Local Unit Tests Lie About Edge Runtimes -- Live Staging QA Paid for Itself Twice
Why deploy to live staging and run headless Chromium if you already have 3,264 tests and multi-agent code review? Two real jobs answered that:

- **The `1 MiB` Multipart Parser Trap (`job-525`):** Enforced a `10 MB` `.html` bookmark import limit. All 3,200+ local ASGI `TestClient` tests passed. On live Cloudflare Workers staging, Playwright uploaded a `2 MB` file and hit `HTTP 400: There was an error parsing the body` -- Starlette's `MultiPartParser` defaulted `max_part_size` to `1 MiB`. The factory automatically `git revert`ed `main`, rolled back Workers, patched `max_part_size`, and shipped the fix on QA Round 2 (`$0.51` total).
- **The Missing Wrangler Bundle Glob (`job-568`):** Extracted ~900 KB of inline `<script>` tags into `/static/base.js`, `/static/library.js`, and `/static/tags.js`. All 3,264 local tests passed because local Python reads static files straight from disk. On live staging, Chromium got `404` (`window.showToast === "undefined"`) because `wrangler.staging.jsonc` only bundled `["**/*.html", "**/*.sql", "**/*.md", "**/*.css"]` and omitted `"**/*.js"`. The factory reverted PR `#597`, rolled back staging, patched the Wrangler glob, passed 4-persona Playwright QA on PR `#598`, and merged -- **just `11.5 minutes` from QA failure to self-healed merge**.

### 4. Parallel Agents Expose Hidden Repo Coupling
Running 4-5 concurrent worktrees acted like a fuzz tester for our build and test infrastructure:
- **Content-hashed CSS collisions:** Parallel UI PRs edited completely different HTML templates, regenerated `tailwind-<sha256>.css`, and collided on `main` 100% of the time. A deterministic asset-conflict resolver + pre-gate auto-polish (`ruff format` + `build_tailwind.py` *before* `checks.sh`) eliminated an entire class of mechanical LLM retries.
- **Latent test races:** Enabling `pytest -n auto` immediately exposed two pre-existing bugs that had hidden under serial execution (one test deleting a shared build directory during `--dry-run`, and an order-dependent fixture). Fixing them made 8-way parallel test runs 100% deterministic.

### 5. Specialized Reviewers Catch What Code Review Misses (`job-527`)
In `job-527` (tag merge UX), `checks.sh` passed and `reviewer-code` approved. `reviewer-ux` flagged a real user-facing bug: an HTMX validation `400` on tag self-merge returned no `HX-Trigger` error toast, leaving the user staring at a dead button. Splitting review personas (`code`, `security`, `ui`, `ux`) paid for itself immediately.

### 6. Your Prototype's Shell Scripts Will Betray You First
We started with a 19-file bash-and-markdown prototype. It worked for one happy-path run and broke on partial outputs, concurrent `job.json` writes, and crash recovery. Rewriting the orchestrator in Python with strict JSON schemas, file locks, and unit/e2e tests was the minimum bar for debugging failures at 2 a.m.

### 7. `gh` CLI and `git worktree` Have Sharp Edges
- Always pass `--body-file` (never inline `--body`) for PR descriptions, and never interpolate LLM text into `bash -c`.
- Never run `git pull --rebase` against a branch that shares worktree commits; use explicit `git fetch` + `git merge --no-edit`.
- Never allow an `origin/<base>` fast-forward fallback when restoring a branch after a revert -- if the reverted PR's base commit is already an ancestor of `origin/main`, fast-forwarding silently wipes out the PR's work.

### 8. Naive Retries Re-Build Merged PRs and Run Your Test Suite 5x
Persist stage checkpoints (`verified_gate_sha`, `needs_pre_merge_gate`, `prior_prs`). If a job dies mid-review or mid-merge, skip re-running `checks.sh` when `HEAD == verified_gate_sha` and never rebuild an already-merged PR.

### 9. Parallel Workers Without a DAG Turn Into `needs-human` Spam
Unordered workers racing on related issues cause cascading rebase conflicts. Modeling `depends_on` as a first-class DAG in the scheduler (and resetting dependent PRs cleanly when a parent lands) keeps workers building on settled `main`.

### 10. Watchdogs Cut Both Ways -- And Battle-Tested 15 Live Rollbacks
When we enabled a `45s` idle-background watchdog on headless agent subprocesses, we missed one detail: our agent CLI auto-backgrounds any shell command taking `>10s` (like `pytest` and Playwright) while printing an idle status line to `stderr`. The watchdog killed **32 active test/QA runs**, triggering **15 consecutive unattended `git revert` + Cloudflare Worker rollbacks**.

Fixing the watchdog was straightforward once diagnosed (track child PIDs via `/proc` before unlatching the timer and raise the grace window to `600s`). The unexpected win? Having a `45s` gremlin fire **15 live rollbacks in anger** proved our automated rollback safety net never once left `main` broken or `/health` red.

---

## If You're Building Your Own Factory, Start Here

1. **Instrument wall-clock vs. LLM time and cost per step from day one** -- your biggest speedup is probably your test suite or deploy pipeline, not your prompt.
2. **Tier models by cognitive load + inject a static `REPO_MAP.md`** -- don't pay frontier rates for cold-start `grep` or structured test fixes.
3. **Close the loop with live staging QA and automatic rollback** -- unit tests won't catch bundler globs or framework parser limits.
4. **Automate deterministic formatting, asset hashes, and conflict recovery** before spending tokens on them.
5. **Test your failure paths under real concurrency** -- locks, `git revert`, and process watchdogs are where autonomous systems live or die.
