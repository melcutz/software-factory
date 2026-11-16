# Repository Architecture Map (`software-factory-agy`)

Zero-dependency Python 3.10+ autonomous issue-to-PR software factory built on Google Antigravity (`agy`), Git refs (`refs/factory/*`), and `herdr-core` (`hc`).

## Core Layout & Responsibilities

- `factory/cli.py` — Central multi-repo CLI (`factory run`, `worker`, `queue`, `status`, `halt`, `resume`, `pause`, `unpause`, `fleet`, `init-board`, `gate`, `doctor`, `_exec-job`).
- `factory/bin/factory` — Repo-local CLI wrapper script.
- `factory/agents/*.md` — Headless role instructions (`planner`, `intake`, `spec-writer`, `builder`, `reviewer-code`, `reviewer-security`, `reviewer-ux`, `reviewer-ui`, `approver`, `qa`, plus interactive `architect` and `researcher`).
- `factory/lib/`
  - `agy_runner.py` — Headless `agy -p --output-format json --json-schema` process-group runner, `/proc` child-aware idle watchdog, `QuotaExhaustedError` detection, prompt assembly (`REPO_MAP.md`, `allow_self_host`).
  - `config.py` — `DEFAULT_CONFIG` loader/validator (`allow_self_host`, `auto_merge`, `protected_paths`, `staging_qa`), model pricing & USD cost estimation, reviewer routing.
  - `fleet.py` — Peer machine discovery & remote control (`hc machine ls`, `hc run --on`, `fleet.json`, `wake`, `pause`, `unpause`, `update`, `remove`).
  - `gate_runner.py` — Deterministic `protected_paths` verifier (`HARDCODED_PROTECTED` vs `SELF_HOST_HARDCODED_PROTECTED`), pre-gate `auto_polish_worktree` (`ruff` + Tailwind), adaptive `PYTEST_WORKERS`, `factory/checks.sh` runner.
  - `git_ops.py` — Isolated `.worktrees/<job_id>` lifecycle, `repo_lock`, base-branch sync & asset conflict resolver, `<untrusted_issue_data>` escaping, `GitHubClient` (`gh` CLI wrapper).
  - `git_state.py` — Git-native distributed coordination (`GitStateStore`) using orphan commits and `--force-with-lease` CAS under `refs/factory/{workers,jobs,checkpoints,locks/staging,control,quota,dag}`.
  - `governor.py` — Cross-platform host CPU/RAM probe (`/proc/meminfo`, `vm_stat`, `getloadavg`) and shared model quota cooldown check.
  - `herdr_bridge.py` — `herdr-core` (`hc`) background child terminal spawner (`hc run --bg`) and OSC 7501 status protocol emitter.
  - `pipeline.py` — End-to-end job state machine driver (`process_job`, `run_factory`, `run_halt`, `run_resume`, `run_doctor`, staging train batch execution, status table formatting).
  - `planner.py` — Queue-level dependency DAG builder (`Depends on #N` + LLM surface overlap + cycle-breaking `graphlib.TopologicalSorter`).
  - `project_board.py` — GitHub ProjectV2 `"Factory"` board creation and stage-to-column sync.
  - `schemas.py` — Strict JSON schemas (`SCHEMAS`), stage output validator/normalizer, and Markdown artifact renderers.
  - `staging_qa.py` — `StagingTrainCoordinator`, `factory/staging_hooks.py` plugin loader, secret redaction, surgical `git revert`, retry/requeue branch restoration.
  - `state.py` — Thread-safe atomic `job.json` persistence, `TRANSITIONS` enforcement, `create_or_resume_job`, token/cost `record_usage`.
  - `worker.py` — Continuous competing-consumer `run_worker` loop, `ClaimPacer`, warm-affinity candidate selector (`Tier 1/2/3`), `exec_job_command`.

## Testing & Conventions

- **Zero third-party runtime dependencies:** Standard library only in `factory/` (`pyproject.toml` `dependencies = []`).
- **Tests (`tests/`):** Offline `unittest` + `pytest` suites using temporary bare Git remotes (`origin.git`) and mocked `gh` / `agy` / `hc` shims.
- **Verification Gate (`factory/checks.sh`):** Runs `PYTHONPATH=".:tests" python3 -m pytest -q` (supports `FACTORY_GATE_SCOPE=slice|full`). Use `FACTORY_GATE_SCOPE=slice bash factory/checks.sh` during slice turns.
- **Self-Host Protected Paths:** Never modify `factory/checks.sh`, `factory/config.json`, `.agents/`, or `.github/`.
