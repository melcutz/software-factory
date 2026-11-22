# Building an Autonomous Software Factory, Part 2: Going Multi-Machine Without a Server (and Every Way It Broke)

*War stories, 30-job fleet telemetry, and 10 distributed-systems lessons from scaling an autonomous coding factory across a homelab -- including letting it build itself.*

In Part 1, we showed how optimizing test gates, deploys, and model tiering on a single workstation cut our autonomous PR cost 35x (`$14.25` -> `$0.27`) and dropped delivery time from `52.2` minutes to ~`20`.

The obvious next question was: **"Great -- now run it continuously across multiple machines and multiple repos without running a Kubernetes cluster or babysitting terminals."**

So we did. Over four intense days, we separated the orchestrator engine from target repositories, built a **zero-infrastructure multi-machine coordinator on native Git refs (`refs/factory/*`)**, wired it into **`herdr-core` (`hc`) + OSC 7501** for live fleet visibility, **pointed the factory at itself**, shipped **Smart Batch Mode (`v0.3.0`)**, ran **30 instrumented fleet jobs** across two 8-core machines (plus tried to recruit a third), and added **pluggable multi-provider coder chains** (`agy`, `opencode`, and an optional local GPU decision gate).

What we discovered the hard way: **going from 1 machine to 2 machines doesn't double your complexity -- it turns every single-process assumption you ever made into a distributed systems bug.** At one point, our shiny 2-machine, 16-core fleet took 4 hours to stumble through 2 issues while a single interactive session finished 7 issues in 20 minutes -- and we came within an inch of rolling back to `v0.1.0`.

Here is the architecture that survived, the 30-job fleet telemetry that surprised us *again*, and the 10 hard-won lessons from going multi-machine.

---

## The Multi-Machine Architecture in 60 Seconds

Personal desktops and homelab boxes are not dedicated cloud servers -- humans still use them, laptops sleep, Wi-Fi drops, and nobody wants to maintain Redis or Postgres just to coordinate coding agents. So we built a **peer-to-peer competing-consumer fleet** with three core layers:

1. **Git Refs *Are* the Database (`refs/factory/*`):** Every target repo coordinates workers via zero-parent orphan commits pushed with `git push --force-with-lease` Compare-And-Swap -- atomic job leases (`refs/factory/jobs/job-*`), mid-build slice checkpoints (`refs/factory/checkpoints/job-*`), worker heartbeats (`refs/factory/workers/<machine>`), a cross-machine staging mutex (`refs/factory/locks/staging`), a global kill switch (`refs/factory/control`), and shared `429` cooldowns (`refs/factory/quota`). Because custom `refs/factory/*` sit outside `refs/heads/*` and `refs/tags/*`, they trigger **zero GitHub Actions runs**, burn **zero private-repo CI minutes**, and never touch a checked-out worktree.
2. **Homelab-Polite Workers Inside Herdr (`hc`) + OSC 7501:** Each machine runs `factory worker --all` inside `herdr-core`, checking local CPU/RAM admission limits before claiming work and spawning each job as a background child terminal that streams OSC 7501 status (`working`, `progress`, `blocked`, `done`). Spread-first pacing + hashed tie-breaking distributes batches evenly across idle hosts, and `factory halt` / `factory resume` stops the entire fleet cleanly within seconds without burning retry rounds.
3. **Smart Batch Mode + Pluggable Coder Chains:** A single-call triage pass packs related/overlapping issues into a Batch-DAG executed inside one hot builder session with atomic per-issue commits and cherry-pick salvage if one issue fails. Per-role fallback chains route across `agy`, `opencode`, and an optional local GPU MCP decision gate with shared fleet-wide quota cooldown buckets.

*[Insert Diagram: Multi-Machine Git-Ref Fleet Architecture -- export from `README.md`]*

---

## By the Numbers: 30 Instrumented Fleet Jobs Across Two Machines

After stabilizing `worker-a` and `worker-b`, we pulled full stage-by-stage telemetry across **30 consecutive multi-machine jobs** (`example-app`: 24, `software-factory`: 5, `example-site`: 1) -- plus overall multi-repo fleet stats (`69` tracked jobs):

- **30-job fleet outcomes:** **`25 / 30` merged (`83.3%`)** -- **`18` merged in Round 1 (`72%`)**, `4` in R2, `2` in R3, `1` in R4; `4` rejected/superseded cleanly; `1` `needs-human` (an external Cloudflare setting, not a code bug)
- **Overall multi-repo fleet stats (`69` jobs):** **`91.3%` merge rate** (`63` merged), `$81.55` total spend (**`$0.57` median** per merged job), **`88.49%` prompt-cache hit rate**
- **30-job cohort spend & tokens:** **`$27.83` total** (**`$0.62` median** per merged job) across `17.64M` non-cache + `113.39M` cached tokens (`86.5%` cache hit rate)
- **Where active execution time actually went (`20.1m - 20.8m` median active):**
  - **Writing code (`builder` Round 1):** just **`3.1 min` (`12% - 16%` of active time)**
  - **Single-threaded staging lock, merge sync & failover wait:** **`29.5% - 38.2%`**
  - **Playwright browser QA (running inside the global staging lock):** **`17.3% - 21.8%`**
  - **Repeated `checks.sh` test gates:** **`12.6% - 14.0%`**
  - **Cloudflare staging deploys:** **`5.9% - 6.2%`**
- **Unplanned human hiatus:** **`54.7%` of raw calendar time (`1,067.5 min`)** was just two jobs (`#637`, `#639`) sitting overnight in `needs-human` because `worker-b` was missing Playwright/Chromium
- **Reviewer signal vs. ceremony (`88` reviewer LLM calls):** **`98.9%` pass rate (`87 / 88`)** -- while `review-security` on a heavy reasoning model took **`71.6s` median** (the bottleneck in `10 / 11` rounds with **`0` catches**) and `approver` rubber-stamped **`10 / 10`** clean PRs that only had non-blocking `low` nits

---

## 10 Hard-Won Lessons from Going Multi-Machine

### 1. Going Multi-Process Broke Every Single-Process Assumption Overnight
In `v0.1.0`, one Python process coordinated threads inside one repo. The moment `factory worker` spawned separate `_exec-job` processes via `hc` across two machines, four "impossible" bugs detonated back-to-back:

- **In-process `RLock` stopped being a lock:** Concurrent `_exec-job` processes on the same host raced on `git fetch` and clobbered `.git/FETCH_HEAD` until we upgraded `repo_lock()` to combine a thread lock with OS-level `fcntl.flock` ([`51069c7`](https://github.com/melcutz/software-factory/commit/51069c7a5)).
- **In-process `pytest-xdist` budgeting caused `154%` CPU starvation:** Our Part 1 `_ACTIVE_FULL_GATES` counter only knew about threads in its *own* process! Two sibling `_exec-job` processes each spawned `8` pytest workers (`16+` Python processes on an 8-core box), spiking load to **`154.1%`**, tripping our own Host Admission Governor (`>= 85%`), blocking new claims, and causing wall-clock latency assertions (`time.perf_counter()`) to fail under CPU starvation. Moving gate slot counting to OS-level PID files (`/tmp/factory-gate-slots-<uid>/`) fixed it immediately ([`0a822fa`](https://github.com/melcutz/software-factory/commit/0a822fab5)).
- **Silent lease expiry triggered cross-host double-runs:** Any stage taking `> 6` minutes expired its `360s` job lease and got stolen mid-flight by the other machine. Worse, our first `renew_job_lease()` patch had a subtle bug that failed quietly -- burning **`37.1 minutes` of duplicate cross-host reruns** before we caught and fixed it ([`1486a05`](https://github.com/melcutz/software-factory/commit/1486a05eb), [`51069c7`](https://github.com/melcutz/software-factory/commit/51069c7a5)).
- **Orphaned staging train tickets caused standalone + batched double-processing:** When `worker-b` restarted mid-merge, its leftover `refs/factory/train/job-628` ticket was scooped up into `worker-a`'s `train_size=2` staging batch *while* another worker simultaneously ran `#628` standalone ([`73a3fe0`](https://github.com/melcutz/software-factory/commit/73a3fe031)).

### 2. Homelab Machines Are Snowflakes: Non-Login `PATH`, Missing Browsers, and `SIGILL`
Our design spec started with an optimistic sentence: *"Every machine is provisioned to pass `factory doctor`."* Reality tested that immediately:

- **The `SIGILL` (`-4`) CPU trap:** Our third homelab machine (a 1st-gen Intel Core i7 laptop with `sse4_2` only, no `AVX`/`AVX2`) passed Python, `git`, `gh`, and all 8 staging secrets -- then died instantly on `agy --version` with **`exit -4` (`SIGILL` — Illegal Instruction)**. That single check birthed `factory fleet remove` ([`084c735`](https://github.com/melcutz/software-factory/commit/084c735ea)).
- **Environment drift cost us 14.6 overnight hours:** `worker-b` lacked `git config user.name`, lacked `pytest` on `/usr/bin/python3`, had an old `/usr/bin/gh` (`2.46.0`, hitting deprecated `projectCards` GraphQL errors), missed `uv` and `node` in non-login `hc` shells (where `~/.local/bin` isn't on `PATH`), and lacked Playwright + Chromium system libraries -- stalling `#637` and `#639` overnight for **`880` combined minutes**.
- **Fix:** Treat `factory doctor` as a strict admission gate that actually tests `git` identity, `pytest`, Playwright Chromium launch, and the repo's exact `WRANGLER` command before a worker is allowed to claim a single issue ([`7b64824`](https://github.com/melcutz/software-factory/commit/7b648243d), [`21b62c2`](https://github.com/melcutz/software-factory/commit/21b62c253), [`0cb8f42`](https://github.com/melcutz/software-factory/commit/0cb8f423f)).

### 3. Adding Machines Can't Outrun a Single Global Lock (Amdahl's Law Hits Hard)
In Part 1, we found that 66% of time wasn't LLMs. After scaling to two 8-core machines, the 30-job telemetry showed an even starker split: **writing code (`builder` Round 1) took a median of just `3.1 minutes` (`12% - 16%` of active time).**

Over **70% of active wall-clock time** happened *after* the code was written -- and **`29.5% - 38.2%`** was spent waiting single-file behind **`refs/factory/locks/staging`**, because there is only one live `staging.example-app.com` environment! Worse:

- `qa.md` launched full headless Chromium login + screenshots (`5 - 10 min`) even on backend-only OAuth/MCP/header PRs -- *while holding the global staging lock*.
- `coalesce_window_sec: 15` was narrower than issue arrival stagger, so **`train_size` was `1` on 100% of a 9-job batch**. Every waiting PR saw `origin/main` advance ahead of it, invalidating `verified_gate_sha` and forcing another `~2 min` full pre-merge test gate.
- Math is unforgiving: `1.4m deploy + 5.1m browser QA = ~6.5m` inside a global mutex capped fleet merge throughput at **~9 PRs/hour whether you ran 2 machines or 10**.

**What broke the logjam ([`52a469f`](https://github.com/melcutz/software-factory/commit/52a469f4b)):** Widening `coalesce_window_sec` from `15s -> 60s`, adding an HTTP/API fast-mode to Staging QA for non-UI PRs (`~40s` instead of `5 - 10m`), conditionalizing Tailwind CSS rebuilds in `checks.sh`, and waking the claim loop on `ramp_up_delay_sec` instead of sleeping a full `poll_interval_sec` after every single claim.

### 4. Letting the Factory Build Itself Was Brutally Humbling -- and Birthed Smart Batch Mode
Right after shipping `v0.2.0`, we enabled `allow_self_host: true` and filed Issues `#1`–`#9` so the factory could build its own upgrades. It *did* ship `#1` (`PR #10`, `$3.26`), `#2` (`PR #11`, `$7.90`), and later `#12` (`PR #13`, `$0.42`).

But watching our 2-machine, 16-core fleet spend **4 hours stumbling through 2 issues** -- while `reviewer-code` tried to edit `pipeline.py` and run `git commit` inside a read-only review turn, a background daemon collided with `factory run`, and 6–10 cold subprocesses re-read the same ~5k-LOC repo on every stage -- pushed us to the brink of reverting to `v0.1.0`. Meanwhile, finishing `#3`–`#9` in one continuous hot session took **~20 minutes**.

That stark contrast sparked **Smart Batch Mode (`v0.3.0`)**:
- **AST-enriched `REPO_MAP.md` (`<= 150` lines):** Stdlib `ast.parse()` indexes top-level classes, functions, line numbers, and `src <-> test` pairs so builders stop cold-grepping ([`ca37770`](https://github.com/melcutz/software-factory/commit/ca37770f2)).
- **Single-call triage + deterministic Batch-DAG packer (`pack_batches()`):** Packs file-overlapping and dependent issues into one batch (up to `5` issues / weight `6`), schedules disjoint batches in parallel, and isolates human-gated risk into solo batches ([`758a597`](https://github.com/melcutz/software-factory/commit/758a59787)).
- **One hot `batch-builder` session + cherry-pick commit salvage:** Implements issues in topological order with one `(#N)` commit per issue (merged non-squash so every issue stays individually revertible). If one issue breaks `checks.sh` even after a retry, Python cherry-picks the passing commits onto a clean branch, ships the healthy subset, and escalates *only* the broken issue ([`2bb23da`](https://github.com/melcutz/software-factory/commit/2bb23da18)).

### 5. Non-Hermetic Tests Can Literally Halt Your Factory -- or Commit Code to Your Real Repo
When autonomous agents run `pytest` on your orchestrator itself, non-hermetic tests turn into slapstick horror:

- **The Self-Halting Spec Writer ([`7cb3a63`](https://github.com/melcutz/software-factory/commit/7cb3a63bc)):** During Issue `#12`, `spec-writer` ran `pytest` on `tests/test_git_state.py`. One test (`test_halt_and_resume_preserves_checkpoint_without_needs_human`) forgot to isolate `FACTORY_HOST_DIR` -- so it wrote `"lunch break"` to the *real* `~/.config/factory/halt.signal`, causing `spec-writer` to **halt itself mid-run**.
- **The Runaway Test That Committed Real Code:** Later, when wiring `opencode` fallback chains into `factory/config.json`, an e2e test wasn't isolating `FACTORY_OPENCODE_BIN`. `pytest` dispatched to the *real* `~/.opencode/bin/opencode` binary with `cwd` set to our live repo -- and the real LLM happily wrote `fix.py` + `tests/test_fix.py` and **committed `af18f54` directly onto our working branch** before a supervisor killed it!

**Rule carved in stone:** Put an `autouse` fixture in `tests/conftest.py` that poisons *every* real coder binary, local MCP URL, `HC_TERMINAL_ID`, and `FACTORY_HOST_DIR` so any unmocked call fails loudly in 1 millisecond instead of mutating your machine.

### 6. Git-Ref State Works Great -- Once You Patch Its Five Sharp Edges
Using `refs/factory/*` as a zero-server coordination store worked remarkably well (`0` Actions minutes, `<10ms` local object writes, zero branch pollution), once we hunted down five production edge cases:

- **`git commit-tree` empty/unfetched parent (`exit 128`):** `put_train_ticket` used `git ls-remote` instead of `git fetch`, so when a peer machine owned the previous ref, the parent commit SHA wasn't in our local `.git/objects` yet and `commit-tree -p` crashed ([`73a3fe0`](https://github.com/melcutz/software-factory/commit/73a3fe031), [`21b62c2`](https://github.com/melcutz/software-factory/commit/21b62c253)).
- **One GitHub blip killed the whole worker daemon:** An unhandled `gh issue list` network hiccup (`error connecting to api.github.com`) crashed `worker-b` silently, leaving `worker-a` carrying 9 issues solo until we wrapped every poll iteration in `try/except` ([`0a822fa`](https://github.com/melcutz/software-factory/commit/0a822fab5)).
- **`git pull --ff-only` in `fleet update` broke on multi-branch remotes** (`fatal: Cannot fast-forward to multiple branches`) -- always pass explicit `origin <branch>` ([`6f798a5`](https://github.com/melcutz/software-factory/commit/6f798a536)), and remember that pulling code on disk doesn't reload long-lived Python worker daemons (`factory fleet restart`, [`21b62c2`](https://github.com/melcutz/software-factory/commit/21b62c253)).
- **Split-brain local vs. remote state:** When `worker-a` merged or cleaned a job, `worker-b` still kept stale local `factory/jobs/job-N/` and old `factory/job-N` branches -- causing retries to resume empty branches and `factory status` to print `NOT RUNNING / stale` for jobs actively running on the other machine! Reconciling against remote refs, adding a local `cat-file -e` fast path (cutting `factory status` from `~7.5s` to `~1.7s`), and making `factory clean` reconcile closed issues without deleting logs made status trustworthy again ([`b58bd3f`](https://github.com/melcutz/software-factory/commit/b58bd3f69), [`51069c7`](https://github.com/melcutz/software-factory/commit/51069c7a5)).

### 7. Background Rescue Shells Silently Wedge Your Worker Slots -- and QA Fix Rounds Need Their Own Evidence
Two deceptively small bugs caused hours of head-scratching:

- **The PTY Rescue-Shell Trap ([`003a915`](https://github.com/melcutz/software-factory/commit/003a91569)):** In our Herdr design, if a job hit `needs-human` inside a TTY, `_exec-job` dropped into an interactive `/bin/bash` shell in the worktree so a human could debug immediately. Sounds helpful -- except `hc run --bg` allocates a real PTY (`sys.stdin.isatty() == True`)! Every escalated background job parked an idle `/bin/bash` forever, permanently hogging worker slots until the machine looked full while doing nothing.
- **Blind QA Fix Rounds Across Machines ([`f2c1c7a`](https://github.com/melcutz/software-factory/commit/f2c1c7a1c)):** After a staging QA failure and rollback, `qa-failure.md` wasn't being injected into the Round-2 builder prompt, *and* the restored rollback branch wasn't synced back to `refs/factory/checkpoints/*`. If `worker-b` picked up the retry after `worker-a` rolled back staging, Builder literally had to guess why staging failed!

### 8. Teach QA to Recognize `external_blocker` -- Code Edits Can't Fix Dashboard Toggles
On `job-640`, our factory cleanly removed `<script type="speculationrules">` from templates and JS, deployed to staging, and Playwright *still* recorded `HTTP 503` (`cf-speculation-refused`) across **3 consecutive QA rounds** and 3 automated rollbacks.

The culprit wasn't in the repo at all: **Cloudflare Speed Brain** was enabled at the zone level, injecting `speculation-rules: "/cdn-cgi/speculation"` into every response header at the edge!

No amount of Builder retries could flip a Cloudflare dashboard toggle. Adding an explicit `external_blocker` field to the QA output schema ([`21b62c2`](https://github.com/melcutz/software-factory/commit/21b62c253)) lets QA escalate immediately on Round 1 instead of burning 3 revert-and-rebuild cycles -- paired with a structured `### Factory Escalation` comment (**Root Cause** + **Action Needed** at the top) and remote `factory logs` fallback so you can debug a failure on `worker-b` from `worker-a` ([`488c95d`](https://github.com/melcutz/software-factory/commit/488c95dd5)).

### 9. Audit Reviewers and Approvers With Data -- Ceremony Masquerades as Safety
Across **88 reviewer LLM calls** in our 30-job telemetry report:

- `review-code`, `review-security`, `review-ux`, and `review-ui` returned `PASS` **87 / 88 times (`98.9%`)**.
- `review-security` (running on a heavy reasoning model) averaged **`71.6s` median** (`7x` slower than `review-code` at `10.2s`), was the wall-clock bottleneck in **`10 / 11` security-tagged rounds (`91%`)**, and caught **`0` failures**.
- `approver` triggered sequentially on `10 / 20` jobs whenever any reviewer left a harmless non-blocking `low` nit, adding `31 - 46s` and approving **`10 / 10` (`100%`)**.

Telemetry let us trim ~`25% - 30%` of latency and cost without weakening real safety gates: relax `auto_approve_clean` to skip `approver` when every reviewer returns `PASS` with zero blocking findings, drop `intake` and `planner` to low-effort tiers, and only escalate `review-security` to frontier reasoning when `risk == "high"` ([`52a469f`](https://github.com/melcutz/software-factory/commit/52a469f4b)).

### 10. Multi-Provider Fallback Chains + Layered Config Make an Open-Source Factory Usable
Depend on one coding CLI and one quota bucket, and a single `429` stalls your entire fleet. Hardcode your personal model tiers, pricing tables, and machine hostnames in `factory/config.json`, and open-sourcing becomes a leak hazard.

Our latest milestone ([PR `#15`](https://github.com/melcutz/software-factory/commit/3db5ae36f)) fixed both:
- **Pluggable coder backends & per-role fallback chains:** Every role accepts an ordered provider chain across `agy`, `opencode` (including free/low-cost tiers), and an optional local GPU MCP decision gate (`gpu:*`, which abstains cleanly below `0.9` confidence so the chain falls through).
- **Shared cooldown buckets (`refs/factory/quota`):** A `429` puts *only that provider bucket* in fleet-wide cooldown while the chain fails over immediately to the next tier -- never escalating to `needs-human`.
- **Layered config + denylist leak checks:** `DEFAULT_CONFIG` -> `~/.config/factory/config.json` -> `<repo>/factory/config.json` -> `<repo>/factory/config.local.json` (gitignored), backed by a denylist-driven `scripts/check_leaks.py` gate in `checks.sh` so private domains, hostnames, and endpoints never sneak into public commits.

---

## If You're Scaling Your Factory Beyond One Machine, Start Here

1. **Promote every local lock and counter to OS or Git scope** -- thread `RLock`s and in-process CPU counters break the instant you spawn multi-process workers.
2. **Gate every worker on a ruthless `doctor` check** -- non-login `PATH`, missing Chromium libs, old CLIs, and `SIGILL` CPUs will burn more overnight hours than any LLM hallucination.
3. **Watch Amdahl's Law on shared staging** -- widen deploy-train batching windows and keep slow browser QA out of backend-only PRs, or 10 machines will run no faster than 1.
4. **Make your test suite aggressively hermetic** -- poison every real coder binary, MCP URL, and host config path in `conftest.py` before letting agents run `pytest` on your orchestrator.
5. **Batch related work in hot sessions, and teach QA to stop on external blockers** -- cold multi-stage subprocesses and 3-round retries on dashboard toggles are pure token waste.

You can explore the zero-server multi-machine orchestrator (`v0.3.0`), Smart Batch Mode, pluggable coder backends, and full 30-job telemetry report in [`software-factory`](https://github.com/melcutz/software-factory).
