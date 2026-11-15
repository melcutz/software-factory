We pointed our autonomous software factory at a live SaaS repo (FastAPI + Cloudflare Workers + D1 + 3,264 tests) and queued 36 real GitHub issues.

So far: 29 multi-file PRs merged (70 vertical slices), 217M tokens processed at an 87.7% cache hit rate.

Everyone assumes LLM latency and model quality are the main bottlenecks in agentic coding. The telemetry told a very different story:

1. 66.6% of our wall-clock time wasn't LLMs.
Across uninterrupted runs, median delivery was 52.2 min -- and all LLM turns combined took just 16.3 min (33.4%). The other 35.9 min went to serial test gates, Cloudflare Worker deploys, and lock waits. Parallelizing pytest (`5.4x` faster), adding diff-aware slice gates (`<4s`), and coalescing staging deploys into a batched deploy train cut clean 2-slice delivery to 18.9 - 22.3 min.

2. Model tiering + a 90-line REPO_MAP.md cut PR cost 35x.
Our early all-frontier runs hit $7 - $14.25 per PR. Routing spec/security/high-risk approval to frontier models and structured build/review/QA to fast mid-tier models -- plus prepending a static REPO_MAP.md to eliminate cold-start grep turns -- dropped our median cost across 19 consecutive merged PRs to $0.27 ($4.97 total, 100% merged).

3. Local unit tests lied about edge runtimes.
Twice, PRs passed all 3,264 local tests and multi-agent code review, then failed live Playwright QA on staging:
- A 2 MB file upload hit HTTP 400 in Cloudflare Workers due to Starlette's default 1 MiB multipart limit.
- Extracting inline JS to `/static/*.js` returned 404 in Workers because `wrangler.staging.jsonc` omitted `**/*.js` from its bundle globs.
In both cases, the factory automatically reverted `main`, rolled back staging Workers, handed the Playwright trace back to Builder, and merged the self-healed fix (in just 11.5 minutes on the Wrangler bug).

4. Bonus war story: a misconfigured 45s subprocess watchdog accidentally stress-tested our automated rollback pipeline 15 times in a row -- without breaking `staging/health` once.

I wrote up the full architecture, per-job telemetry, and 10 engineering lessons on Medium:
[Insert Medium Article Link]

Curious: for those running coding agents in CI/CD, where is your biggest bottleneck right now -- model quality, test speed, or deployment loops?
