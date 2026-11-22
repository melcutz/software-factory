# Repository Architecture Map (`software-factory`)

Zero-dependency Python 3.10+ autonomous issue-to-merge software factory with pluggable coding agent backends (`agy`, optional `opencode`, optional local `gpu:*` decision MCP), Git refs (`refs/factory/*`), and `herdr-core` (`hc`).

## Core Layout & Responsibilities

- `factory/cli.py` — Central multi-repo CLI (`factory run`, `queue`, `plan`, `status`, `gate`, `doctor`, `init-board`, `halt`, `resume`, `worker`, `_exec-job`, `pause`, `unpause`, `fleet`, `clean`, `map`, `init`, `learn`, `stats`, `logs`, `patrol`).
- `factory/bin/factory` — Repo-local CLI wrapper script.
- `factory/config.example.json` — Annotated reference configuration showing layered options, fallback chains, `provider_timeouts`, `gpu`, `quota.buckets`, `model_pricing`, `batch`, and `staging_qa`.
- `factory/agents/*.md` — Headless role instructions (`planner`, `batch-planner`, `intake`, `spec-writer`, `builder`, `batch-builder`, `reviewer-code`, `reviewer-security`, `reviewer-ux`, `reviewer-ui`, `approver`, `qa`, plus interactive `architect` and `researcher`).
- `factory/lib/`
  - `agy_runner.py` — Coder dispatcher (`run_agent`, `run_agent_chain`, `classify_model_provider`), headless `agy -p --output-format json --json-schema` process-group runner, `/proc` child-aware idle watchdog, `QuotaExhaustedError` detection, prompt assembly (`REPO_MAP.md`, `PITFALLS.md`, `allow_self_host`).
  - `batch.py` — Smart batch triage (`triage_and_plan_queue`), deterministic Batch-DAG packer (`pack_batches`), hot `batch-builder` execution (`execute_batch`), partial-failure commit salvage, and `run_batch_factory`.
  - `config.py` — Layered config loader/validator (`DEFAULT_CONFIG`, user/repo/local merge, `allow_self_host`, `auto_merge`, `protected_paths`, `provider_timeouts`, `gpu`, `quota`, `batch`, `staging_qa`), `model_pricing` & USD cost estimation, reviewer routing.
  - `fleet.py` — Peer machine discovery & remote control (`hc machine ls`, `hc run --on`, `fleet.json`, `status`, `wake`, `pause`, `unpause`, `update`, `restart`, `remove`).
  - `gate_runner.py` — Deterministic `protected_paths` verifier (`HARDCODED_PROTECTED` vs `SELF_HOST_HARDCODED_PROTECTED`), pre-gate `auto_polish_worktree` (`ruff` + Tailwind), adaptive `PYTEST_WORKERS`, `factory/checks.sh` runner.
  - `git_ops.py` — Isolated `.worktrees/<job_id>` lifecycle, cross-process `repo_lock`, base-branch sync & asset conflict resolver, `<untrusted_issue_data>` escaping, `GitHubClient` (`gh` CLI wrapper).
  - `git_state.py` — Git-native distributed coordination (`GitStateStore`) using orphan commits and `--force-with-lease` CAS under `refs/factory/{workers,jobs,checkpoints,locks/staging,control,quota,dag,train}`.
  - `governor.py` — Cross-platform host CPU/RAM probe (`/proc/meminfo`, `vm_stat`, `getloadavg`) and shared model/bucket quota cooldown check (`quota_bucket_for_model`, `quota_cooldown_for_model`).
  - `gpu_runner.py` — Optional local-LLM Streamable-HTTP MCP client (`gpu:*` models) and abstain-safe stage adapters for `planner`, `intake`, and `approver`.
  - `herdr_bridge.py` — `herdr-core` (`hc`) background child terminal spawner (`hc run --bg`) and OSC 7501 status protocol emitter.
  - `onboarding.py` — Repository onboarding (`init_repository`), AST-enriched `REPO_MAP.md` generator (`generate_repo_map`), and deterministic `PITFALLS.md` distillation (`learn_from_jobs`).
  - `opencode_runner.py` — OpenCode CLI runner (`opencode run --format json --auto`) for `opencode/*`, `opencode-go/*`, and `openrouter/*` models with JSONL parsing, session re-ask, and rate-limit detection.
  - `patrol.py` — Repository health patrol sensors (`stale-repo-map`, `repeated-gate-retries`), `factory/patrol_hooks.py` loader, and deduplicated GitHub issue creation.
  - `pipeline.py` — End-to-end job state machine driver (`process_job`, `run_factory`, `run_halt`, `run_resume`, `run_doctor`, staging train batch execution, status table formatting).
  - `planner.py` — Queue-level dependency DAG builder (`Depends on #N` + LLM surface overlap + cycle-breaking `graphlib.TopologicalSorter`).
  - `project_board.py` — GitHub ProjectV2 `"Factory"` board creation and stage-to-column sync.
  - `schemas.py` — Strict JSON schemas (`SCHEMAS`, including `batch_plan` and `batch_build`), stage output validator/normalizer, and Markdown artifact renderers.
  - `staging_qa.py` — `StagingTrainCoordinator`, `factory/staging_hooks.py` plugin loader, secret redaction, surgical `git revert`, retry/requeue branch restoration.
  - `state.py` — Thread-safe atomic `job.json` persistence, `TRANSITIONS` enforcement, `create_or_resume_job`, token/cost/tier/provider `record_usage`, `record_metrics`.
  - `worker.py` — Continuous competing-consumer `run_worker` loop, `ClaimPacer`, warm-affinity candidate selector (`Tier 1/2/3`), `exec_job_command`.
- `scripts/check_leaks.py` — Tracked-file and git-history scanner against a local leak denylist (`~/.config/factory/leak-denylist.json` or `factory/leak-denylist.local.json`).

## Testing & Conventions

- **Zero third-party runtime dependencies:** Standard library only in `factory/` (`pyproject.toml` `dependencies = []`).
- **Tests (`tests/`):** Offline `unittest` + `pytest` suites using temporary bare Git remotes (`origin.git`) and mocked `gh` / `agy` / `opencode` / `hc` shims and local mock HTTP MCP servers.
- **Verification Gate (`factory/checks.sh`):** Runs `PYTHONPATH=".:tests" python3 -m pytest -q` (supports `FACTORY_GATE_SCOPE=slice|full`) followed by `python3 scripts/check_leaks.py`. Use `FACTORY_GATE_SCOPE=slice bash factory/checks.sh` during slice turns.
- **Self-Host Protected Paths:** Never modify `factory/checks.sh`, `factory/config.json`, `.agents/`, or `.github/`.
