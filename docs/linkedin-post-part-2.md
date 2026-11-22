After Part 1 on our autonomous software factory, the top question was: "How do you run this across multiple machines without standing up servers or Kubernetes?"

So we built a zero-server multi-machine coordinator on native Git refs (`refs/factory/*`), wired it into Herdr (`hc`) + OSC 7501, pointed the factory at itself, shipped Smart Batch Mode, and pulled full telemetry across 30 consecutive fleet jobs (69 jobs total, 91.3% merge rate, $0.57 median per merged PR, 88.5% cache hit rate).

Here's the honest truth: going from 1 machine to 2 machines broke almost every single-process assumption we had. Four lessons that surprised us most:

1. Writing code is now just 3.1 minutes (12-16% of active time).
Over 70% of active wall-clock time happened *after* the code was written -- and 30-38% was sitting single-file behind our global staging lock. Running full headless Chromium QA on backend-only PRs inside that lock capped our entire fleet at ~9 PRs/hour whether we ran 2 machines or 10.

2. Every in-process lock and counter broke under multi-process workers.
In-process `RLock` let sibling workers clobber `.git/FETCH_HEAD`. In-process `pytest-xdist` budgeting spawned 16+ test workers on an 8-core box (spiking load to 154% and tripping our own CPU governor). Silent lease expiry burned 37 minutes of cross-host duplicate runs.

3. Non-hermetic tests can literally halt your factory -- or commit real code.
When we let the factory build itself, one test forgot to isolate `FACTORY_HOST_DIR` and wrote `"lunch break"` to the real `halt.signal`, causing `spec-writer` to halt itself mid-run. Later, an unisolated `opencode` test dispatched a live LLM against our real repo and committed a phantom `fix.py` onto our working branch.

4. Homelab machines are snowflakes -- and QA needs to recognize external blockers.
Missing Playwright/Chromium libs on our second worker stalled two jobs overnight for 14.6 combined hours, a third laptop crashed on `SIGILL` (no AVX), and Cloudflare Speed Brain triggered 3 straight automated rollbacks on a setting no code edit could ever fix.

I wrote up the full Part 2 -- architecture, 30-job telemetry, Smart Batch Mode, pluggable coder fallback chains, and all 10 multi-machine war stories -- on Medium:
[Insert Medium Article Part 2 Link]

Repo (MIT, `v0.3.0`): https://github.com/melcutz/software-factory

For anyone running coding agents across multiple hosts or CI runners: what broke first when you moved past a single machine?
