# Changelog

All notable changes to `software-factory` (formerly `software-factory-agy`) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — Current `main` (untagged; latest GitHub release tag is `v0.2.0`)

### Added
- **Smart Batch-DAG Mode (default for `factory run`):** Single-call queue triage (`batch-planner`), deterministic `pack_batches()` Batch-DAG packing (`factory/lib/batch.py`), hot `batch-builder` sessions with per-issue atomic commits, partial-failure commit salvage, `factory plan` dry-run command, `--no-batch` / `--max-batch` flags, and AST-enriched `factory/REPO_MAP.md` generation.
- **Pluggable Coder Backends & Fallback Chains (#15):**
  - OpenCode CLI runner (`factory/lib/opencode_runner.py`) supporting `opencode/*`, `opencode-go/*`, and `openrouter/*` models with JSONL parsing, session re-ask, and rate-limit detection.
  - Optional local-LLM Streamable-HTTP decision MCP (`factory/lib/gpu_runner.py`, `gpu:*` models) restricted to `planner`, `intake`, and `approver`, with abstain-safe confidence and precondition checks.
  - Ordered `models.<role>` fallback chains, `provider_timeouts`, `providers.<name>.bin`, `path_prepend`, bucketed `quota.buckets` cooldowns, and `model_pricing` + `by_tier`/`by_provider` telemetry in `factory stats`.
  - Layered configuration merging `DEFAULT_CONFIG`, `$FACTORY_USER_CONFIG` / `~/.config/factory/config.json`, `<repo>/factory/config.json`, and `<repo>/factory/config.local.json`.
- **Onboarding, Observability, Cleanup & Self-Hosting (#3, #4, #5, #6, #7, #8, #9, #13):**
  - `factory init`, `factory map`, and `factory learn` (`factory/PITFALLS.md` distillation).
  - `factory stats`, `factory logs` (with remote `refs/factory/*` fallback), `factory patrol` (`factory/patrol_hooks.py`), and `factory clean` (worktree + terminal/closed ref reconciliation).
  - `factory fleet restart`, deep `herdr-core` (`hc`) child terminal metadata and OSC 7501 progress updates.
  - Closed `stage=ready` PR review fix-round loop with `factory:ship` label support, `max_job_cost_usd` circuit-breaker, `builder_low` tiering, distributed `refs/factory/train/*` staging train coordination, and `allow_self_host` mode.
  - Leak-check scanner (`scripts/check_leaks.py`) wired into `factory/checks.sh`.

### Changed
- **Repository & Package Rename (#16):** Renamed project and Python distribution from `software-factory-agy` to `software-factory`, updated clone URLs and docs, and added `FACTORY_HOME` checkout lookup fallback in `fleet update`.
- **Doctor Checks:** Expanded `factory doctor` to verify `git` user identity, `pytest` availability when used by `checks.sh`, `staging_qa` hooks/secrets, `opencode` when configured, `gpu.mcp_url` reachability, and merged config layer paths.
- **Reliability & Performance:** Cross-process `repo_lock`, active job lease renewal, transient poll error resilience, `ramp_up_delay_sec` worker claiming, bounded cross-process pytest workers, `qa-failure.md` fix-round injection, and `staging_qa` rollback branch sync.

## [0.2.0] - 2026-11-15

### Added
- **Git-Native Multi-Machine Coordination (`factory/lib/git_state.py`):** Zero-server distributed coordination under `refs/factory/*` using orphan commits and `--force-with-lease` CAS (`jobs`, `workers`, `checkpoints`, `locks/staging`, `control`, `quota`, `dag`).
- **Continuous Worker & Fleet CLI (`factory/lib/worker.py`, `factory/lib/fleet.py`):** Competing-consumer `factory worker` loop with spread-first `ClaimPacer` and warm-worktree affinity, `factory pause` / `unpause`, global `factory halt` / `resume`, and `factory fleet` (`status`, `wake`, `pause`, `unpause`, `update`, `remove`) over `hc machine` or SSH (`~/.config/factory/fleet.json`).
- **Herdr Integration (`factory/lib/herdr_bridge.py`):** `hc run --bg` child terminal spawning and OSC 7501 status reporting with plain-terminal fallback.
- **Host & Quota Governor (`factory/lib/governor.py`):** Cross-platform host CPU/RAM admission checks and shared fleet-wide 429 quota cooldown backoff.

## [0.1.0] - 2026-11-15

### Added
- **Single-Machine Autonomous Issue-to-PR Pipeline (`factory/cli.py`, `factory/lib/pipeline.py`):** End-to-end state machine (`intake` → `spec_writer` → `builder` in isolated `.worktrees/job-<n>` → deterministic gate → parallel specialist reviews → `approver` → `merging`).
- **Queue Dependency DAG & Worktree Ops (`factory/lib/planner.py`, `factory/lib/git_ops.py`, `factory/lib/gate_runner.py`):** `Depends on #N` + LLM surface overlap topological scheduling, `<untrusted_issue_data>` escaping, `protected_paths` verification, `FACTORY_GATE_SCOPE=slice|full`, `auto_polish_worktree`, generated-asset conflict resolution, and agent-assisted merge conflict resolution.
- **Schemas, State Machine & Agent Runner (`factory/lib/schemas.py`, `factory/lib/state.py`, `factory/lib/agy_runner.py`):** Atomic locked `job.json` persistence, strict JSON schema validation, and headless `agy -p` runner with `/proc` child-aware idle watchdog.
- **GitHub Project Board & Staging QA (`factory/lib/project_board.py`, `factory/lib/staging_qa.py`):** `factory init-board` ProjectV2 setup, stage-to-column sync, and generic staging deploy train with `factory/staging_hooks.py` delegation and surgical `git revert`.
