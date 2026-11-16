# Smart Batch Mode & Batch-DAG Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make bounded Smart Batch Mode (`N = 1..5` issues per batch, two-level parallel/sequential Batch-DAG) the default `factory run` execution path, with AST-enriched `REPO_MAP.md`, single-call queue triage, hot `batch-builder` sessions with per-issue atomic commits, cherry-pick commit salvage on partial gate failure, and `factory plan` / `--no-batch` / `--max-batch` CLI controls.

**Architecture:** Isolate batch planning and execution in a focused new module `factory/lib/batch.py` rather than expanding `factory/lib/pipeline.py`. One `batch-planner` LLM call triages the queue (`ACCEPT` / `NEEDS_INFO` / `REJECT`, `risk`, `complexity_weight`, `predicted_files`, `depends_on`), Python's `pack_batches()` builds a strictly acyclic `BatchPlan` (packing overlapping/dependent auto-mergeable issues together, splitting oversized clusters into sequential waves, isolating human-gated issues solo, and scheduling disjoint clusters in parallel), and `execute_batch()` runs one hot `batch-builder` session, one `checks.sh` gate, one read-only review, optional cherry-pick salvage, and one non-squash batch PR (`Closes #...`).

**Tech Stack:** Python 3.12 standard library (`ast`, `graphlib`, `concurrent.futures`, `dataclasses`, `json`, `subprocess`), Git CLI, GitHub CLI (`gh`), `agy` headless CLI, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-15-batch-mode-design.md`

## Global Constraints

- Zero external runtime dependencies (`pyproject.toml` `dependencies = []`; Python 3.12 stdlib only).
- `batch` defaults: `enabled = true`, `max_batch_issues = 5` (`1..10`), `max_batch_weight = 6` (`1..20`), `max_parallel_batches = 2` (`1..8`), `refresh_repo_map = true`.
- `models.batch_builder` defaults to `"gemini-3.8-flash-medium"` and falls back to `models.builder` if omitted.
- `generate_repo_map()` output must stay `<= 150` lines.
- Multi-issue batches (`len(surviving) > 1`) must never squash-merge; if `cfg["merge_method"] == "squash"`, use `"merge"` so per-issue commits (`(#<n>)`) stay individually revertible.
- Never spawn live `hc` terminals during `pytest` without `HC_BIN_PATH`; keep fleet mode disabled by default.

## Review Focus

- **LLM omits an issue or invents a cycle in `batch_plan`:** Missing queued issues default conservatively (`ACCEPT`, `risk="medium"`, `complexity_weight=3`, `predicted_files=[]`), and cycles in `depends_on` are broken deterministically via `_ensure_acyclic()` before packing.
- **Builder claims `DONE` without a commit (or commits to `protected_paths`):** `execute_batch()` audits `origin/<base_branch>..HEAD` commits by SHA/`(#n)` tag and reclassifies commit-less `DONE` entries as `BLOCKED` (`needs-human`).
- **Syntax-broken `.py` file in repo during AST `REPO_MAP.md` generation:** `generate_repo_map()` catches `SyntaxError`/`OSError` per file and skips AST extraction for that file instead of crashing.
- **Downstream batch depends on a failed/salvaged-out issue `#X`:** Any issue in a later wave whose `hard_deps` contains an unmerged `#X` is escalated to `needs-human` before the wave's `batch-builder` starts, while independent siblings in that wave still proceed.
- **Parallel batch merges advancing `origin/main` before sibling batch merges:** Pre-merge `merge_lock` + `git rebase origin/<base_branch>` + one `checks.sh` re-run prevents silent broken merges.

---

### Task 1: Config & State Machine Foundations

**Files:**
- Modify: `factory/lib/config.py:45-75, 135-205`
- Modify: `factory/lib/state.py:10-26`
- Test: `tests/test_config.py`, `tests/test_state.py`

**Interfaces:**
- Produces: `DEFAULT_CONFIG["batch"]` (`enabled`, `max_batch_issues`, `max_batch_weight`, `max_parallel_batches`, `refresh_repo_map`), `DEFAULT_CONFIG["models"]["batch_builder"]`, validated `cfg["batch"]`, and `TRANSITIONS["intake"]` including `"build"`.

- [ ] **Step 1: Write the failing config and state transition tests**

Add to `tests/test_config.py`:
```python
def test_batch_config_defaults_and_validation(tmp_path):
    from factory.lib.config import load_config
    fdir = tmp_path / "factory"
    fdir.mkdir()
    (fdir / "config.json").write_text("{}", encoding="utf-8")
    cfg = load_config(str(fdir))
    assert cfg["batch"] == {
        "enabled": True,
        "max_batch_issues": 5,
        "max_batch_weight": 6,
        "max_parallel_batches": 2,
        "refresh_repo_map": True,
    }
    assert cfg["models"]["batch_builder"] == "gemini-3.8-flash-medium"

    (fdir / "config.json").write_text('{"batch": {"max_batch_issues": 0}}', encoding="utf-8")
    with pytest.raises(ValueError, match="max_batch_issues"):
        load_config(str(fdir))
```
Add to `tests/test_state.py`:
```python
def test_intake_can_transition_directly_to_build(tmp_path):
    jobs_dir = tmp_path / "jobs"
    issue = {"number": 91, "title": "Batch direct", "body": "", "url": ""}
    enqueue_job(str(jobs_dir), issue)
    advance_stage(str(jobs_dir), "job-91", "intake")
    job = advance_stage(str(jobs_dir), "job-91", "build")
    assert job["stage"] == "build"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_config.py::test_batch_config_defaults_and_validation tests/test_state.py::test_intake_can_transition_directly_to_build -v`
Expected: FAIL (`KeyError: 'batch'` and `IllegalTransitionError`).

- [ ] **Step 3: Implement config defaults/validation in `factory/lib/config.py` and transition in `factory/lib/state.py`**

- In `factory/lib/config.py`:
  - Add `"batch_builder": "gemini-3.8-flash-medium"` to `DEFAULT_CONFIG["models"]`.
  - Add `"batch": {"enabled": True, "max_batch_issues": 5, "max_batch_weight": 6, "max_parallel_batches": 2, "refresh_repo_map": True}` to `DEFAULT_CONFIG`.
  - Include `"batch"` in the nested-dict merge set in `load_config()`.
  - Fallback `cfg["models"].setdefault("batch_builder", cfg["models"]["builder"])`.
  - Validate `batch.enabled` (`bool`), `batch.refresh_repo_map` (`bool`), `batch.max_batch_issues` (`int` `1..10`), `batch.max_batch_weight` (`int` `1..20`), `batch.max_parallel_batches` (`int` `1..8`).
- In `factory/lib/state.py`:
  - Add `"build"` to `TRANSITIONS["intake"]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py tests/test_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add factory/lib/config.py factory/lib/state.py tests/test_config.py tests/test_state.py
git commit -m "feat(batch): add batch config defaults and intake->build state transition"
```

---

### Task 2: AST-Enriched `REPO_MAP.md` & Test-File Pairing

**Files:**
- Modify: `factory/lib/onboarding.py:52-91`
- Test: `tests/test_onboarding.py`

**Interfaces:**
- Produces: `generate_repo_map(repo_root: str) -> str` emitting `## Test Mapping` and `## Python Symbol Index` with 1-based line numbers, capped at `<= 150` lines.

- [ ] **Step 1: Write the failing onboarding AST map tests**

Add to `tests/test_onboarding.py`:
```python
def test_generate_repo_map_includes_ast_symbols_test_pairs_and_line_cap(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "alpha.py").write_text(
        "class AlphaEngine:\n    pass\n\ndef run_alpha():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "tests" / "test_alpha.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    out = generate_repo_map(str(tmp_path))
    lines = out.splitlines()
    assert len(lines) <= 150
    assert "## Test Mapping" in out
    assert "`src/alpha.py` -> `tests/test_alpha.py`" in out
    assert "## Python Symbol Index" in out
    assert "`src/alpha.py`" in out
    assert "AlphaEngine` (L1)" in out or "AlphaEngine (L1)" in out
    assert "run_alpha" in out and "L4" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_onboarding.py::test_generate_repo_map_includes_ast_symbols_test_pairs_and_line_cap -v`
Expected: FAIL (`AssertionError: assert '## Test Mapping' in out`).

- [ ] **Step 3: Upgrade `generate_repo_map()` in `factory/lib/onboarding.py`**

Add a helper `_extract_python_symbols(path: str, max_symbols: int = 8) -> list[str]` using `ast.parse()` (catching `(SyntaxError, OSError, UnicodeDecodeError)`), returning formatted top-level `ClassName (L<n>)` / `func_name (L<n>)` items. Pair non-test `.py` files with matching `tests/test_<stem>.py` (or `tests/**/test_<stem>.py`) under `## Test Mapping`, list symbol indices under `## Python Symbol Index` (prioritizing `factory/`, `src/`, `app/`), and cap total output at 150 lines (`"\n".join(lines[:150]) + "\n"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_onboarding.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add factory/lib/onboarding.py tests/test_onboarding.py
git commit -m "feat(onboarding): enrich REPO_MAP.md with stdlib AST symbol index and test pairs"
```

---

### Task 3: Batch Schemas (`batch_plan`, `batch_build`) & Agent Prompts

**Files:**
- Modify: `factory/lib/schemas.py:6-38, 204-289`
- Create: `factory/agents/batch-planner.md`
- Create: `factory/agents/batch-builder.md`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Produces: `SCHEMAS["batch_plan"]`, `SCHEMAS["batch_build"]`, validation rules in `validate_stage_output()`, and agent instructions in `factory/agents/batch-planner.md` and `factory/agents/batch-builder.md`.

- [ ] **Step 1: Write the failing schema validation tests**

Add to `tests/test_schemas.py`:
```python
def test_validate_batch_plan_enforces_enums_bounds_and_reasons():
    valid = validate_stage_output(
        "batch_plan",
        {
            "items": [
                {
                    "issue": 12,
                    "decision": "ACCEPT",
                    "risk": "low",
                    "surfaces": ["security"],
                    "complexity_weight": 2,
                    "predicted_files": ["factory/cli.py"],
                    "depends_on": [],
                    "topic_group": "cli",
                    "summary": "Add check",
                    "questions": [],
                    "decision_reason": "",
                }
            ],
            "reasoning": "Single issue ready",
        },
    )
    assert valid["items"][0]["complexity_weight"] == 2

    with pytest.raises(ValueError, match="question"):
        validate_stage_output(
            "batch_plan",
            {
                "items": [{**valid["items"][0], "decision": "NEEDS_INFO", "questions": []}],
                "reasoning": "Missing info",
            },
        )
    with pytest.raises(ValueError, match="complexity_weight"):
        validate_stage_output(
            "batch_plan",
            {
                "items": [{**valid["items"][0], "complexity_weight": 7}],
                "reasoning": "Bad weight",
            },
        )


def test_validate_batch_build_enforces_status_unique_issues_and_blocked_reason():
    valid = validate_stage_output(
        "batch_build",
        {
            "results": [
                {
                    "issue": 12,
                    "status": "DONE",
                    "commit": "abc1234",
                    "summary": ["Added check"],
                    "changes": ["factory/cli.py"],
                    "tests_run": ["tests/test_cli.py"],
                    "blocked_reason": "",
                }
            ],
            "notes": "All green",
        },
    )
    assert valid["results"][0]["status"] == "DONE"

    with pytest.raises(ValueError, match="blocked_reason"):
        validate_stage_output(
            "batch_build",
            {
                "results": [{**valid["results"][0], "status": "BLOCKED", "blocked_reason": ""}],
                "notes": "",
            },
        )
    with pytest.raises(ValueError, match="Duplicate"):
        validate_stage_output(
            "batch_build",
            {
                "results": [valid["results"][0], valid["results"][0]],
                "notes": "",
            },
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_schemas.py::test_validate_batch_plan_enforces_enums_bounds_and_reasons tests/test_schemas.py::test_validate_batch_build_enforces_status_unique_issues_and_blocked_reason -v`
Expected: FAIL (`Unknown schema stage: 'batch_plan'`).

- [ ] **Step 3: Add schemas, validation, and agent markdown prompts**

- Add `SCHEMAS["batch_plan"]` and `SCHEMAS["batch_build"]` and their `validate_stage_output` branches in `factory/lib/schemas.py` matching Spec §§4.2 & 4.4.
- Create `factory/agents/batch-planner.md` documenting triage verdicts (`ACCEPT`/`NEEDS_INFO`/`REJECT`), `complexity_weight` rubric (`1..5`), concrete `predicted_files` (never broad container dirs), `depends_on` edges, and `topic_group`.
- Create `factory/agents/batch-builder.md` documenting ordered execution, targeted `pytest` per issue (never running `factory/checks.sh`), one atomic commit per issue ending in `(#<n>)`, `BLOCKED` / `SKIPPED_DEP` handling with `git checkout -- .`, and `protected_paths` respect.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_schemas.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add factory/lib/schemas.py factory/agents/batch-planner.md factory/agents/batch-builder.md tests/test_schemas.py
git commit -m "feat(batch): add batch_plan and batch_build schemas and agent prompts"
```

---

### Task 4: Deterministic Batch-DAG Packer (`factory/lib/batch.py`)

**Files:**
- Create: `factory/lib/batch.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `planner._normalize_conflict_surfaces`, `planner._surfaces_overlap`, `planner._ensure_acyclic`, `planner._has_path`.
- Produces:
  - `@dataclass class TriageItem(issue: int, title: str, body: str, url: str, decision: str, risk: str, surfaces: list[str], complexity_weight: int, predicted_files: list[str], depends_on: list[int], topic_group: str = "", summary: str = "", questions: list[str] = ..., decision_reason: str = "")`
  - `@dataclass class BatchSpec(id: str, issues: list[TriageItem], solo_only: bool, total_weight: int, predicted_files: list[str], hard_deps: dict[int, set[int]])`
  - `@dataclass class BatchPlan(accepted: list[TriageItem], rejected: list[TriageItem], needs_info: list[TriageItem], batches: list[BatchSpec], batch_deps: dict[str, set[str]], hard_batch_deps: dict[str, set[str]], hard_deps: dict[int, set[int]], reasoning: str = "")`
  - `pack_batches(items: list[TriageItem], hard_deps: dict[int, set[int]], config: dict) -> BatchPlan`

- [ ] **Step 1: Write the failing `pack_batches()` tests in `tests/test_batch.py`**

Cover all 5 packer invariants from Spec §§4.3 & 6:
1. `test_pack_batches_merges_overlapping_files_and_splits_oversized_waves`: `#1` (`weight=3`, `factory/cli.py`), `#2` (`weight=3`, `factory/cli.py`), `#3` (`weight=2`, `factory/cli.py`) under `max_batch_weight=6` pack `#1+#2` into `batch-1` and `#3` into `batch-2` with `"batch-1" in plan.batch_deps["batch-2"]`.
2. `test_pack_batches_keeps_disjoint_surfaces_parallel`: `#1` (`weight=2`, `factory/cli.py`) and `#2` (`weight=2`, `factory/lib/onboarding.py`) land in separate root batches (`batch_deps[b1] == set()` and `batch_deps[b2] == set()`) when surfaces and `topic_group` are disjoint.
3. `test_pack_batches_isolates_human_gated_issues_solo`: under `auto_merge="low"`, a `medium`-risk issue `#2` lands in a `solo_only=True` batch (`len == 1`) even if it shares `factory/cli.py` with `low`-risk `#1` and `#3` (which stay packed together).
4. `test_pack_batches_breaks_cycles_and_respects_hard_deps`: cyclic `hard_deps` don't raise `CycleError`, and topological order inside a batch always places parent before child.
5. `test_pack_batches_respects_max_batch_issues_one`: with `max_batch_issues=1`, every accepted issue gets its own `BatchSpec` of size 1.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_batch.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'factory.lib.batch'`).

- [ ] **Step 3: Implement `TriageItem`, `BatchSpec`, `BatchPlan`, and `pack_batches()` in `factory/lib/batch.py`**

Implement the algorithm in Spec §4.3:
- Classify `solo_only` from `cfg["auto_merge"]`, `item.risk`, and `max_batch_issues == 1`.
- Build affinity components over `hard_deps`, `_surfaces_overlap`, and matching non-empty `topic_group` (excluding `solo_only` items from affinity merging).
- Iterate topological order of `_ensure_acyclic(hard_deps)`, placing each item into an eligible open batch in the same component (respecting `max_batch_issues`, `max_batch_weight`, and acyclic batch placement) or opening a new `BatchSpec`.
- Compute `hard_batch_deps` (inter-batch `hard_deps`) and `batch_deps` (`hard_batch_deps` + wave-ordering edges when distinct batches share `_surfaces_overlap`), verified with `graphlib.TopologicalSorter(batch_deps).prepare()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_batch.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add factory/lib/batch.py tests/test_batch.py
git commit -m "feat(batch): implement deterministic Batch-DAG packer"
```

---

### Task 5: Batch Queue Triage & `factory plan` Dry-Run

**Files:**
- Modify: `factory/lib/batch.py`
- Modify: `factory/cli.py:106-120, 290-450`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `pack_batches`, `planner.parse_explicit_dependencies`, `onboarding.generate_repo_map`, `agy_runner.run_agent`, `state.enqueue_job`, `state.advance_stage`.
- Produces:
  - `triage_and_plan_queue(repo_root: str, issues: list[dict], *, config: dict, gh: GitHubClient, board: ProjectBoardSync | None = None, dry_run: bool = False, planner_runner: Callable | None = None) -> BatchPlan`
  - `format_batch_plan(plan: BatchPlan) -> str`
  - `factory plan [issues...]` CLI command.

- [ ] **Step 1: Write the failing triage & `factory plan` tests in `tests/test_batch.py`**

1. `test_triage_and_plan_queue_fills_omitted_issues_and_enforces_explicit_deps`: mock `planner_runner` omitting issue `#2` (whose body says `Depends on #1`) and marking `#3` as `NEEDS_INFO`; assert `#2` defaults to `ACCEPT`, `risk="medium"`, `complexity_weight=3`, preserves `#1` in `hard_deps[2]`, and in `dry_run=True` does not call `gh.add_label` or `gh.comment_issue`, whereas `dry_run=False` applies `needs_info` label/comment for `#3`.
2. `test_cli_plan_prints_waves_and_verdicts`: invoke `cli.main(["plan", "--repo", str(repo)])` with stubbed `gh.list_queue_issues` and `triage_and_plan_queue`, verify exit code `0` and wave/batch output on stdout.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_batch.py::test_triage_and_plan_queue_fills_omitted_issues_and_enforces_explicit_deps tests/test_batch.py::test_cli_plan_prints_waves_and_verdicts -v`
Expected: FAIL (`ImportError: cannot import name 'triage_and_plan_queue'`).

- [ ] **Step 3: Implement `triage_and_plan_queue()`, `format_batch_plan()`, and `factory plan` CLI subcommand**

- In `factory/lib/batch.py`:
  - Optionally refresh `factory/REPO_MAP.md` when `cfg["batch"]["refresh_repo_map"]` is `True` and `not dry_run` (or generate in-memory map for `dry_run`).
  - Build prompt with `<untrusted_issue_data>` blocks via `format_untrusted_issue_md()`, invoke `run_agent(..., agent_name="batch-planner", stage="batch_plan", model=cfg["models"]["planner"], read_only=True)` (or `planner_runner` hook for tests).
  - Merge LLM triage results with `parse_explicit_dependencies(issues)`, fill any omitted issues conservatively (`ACCEPT`, `risk="medium"`, `complexity_weight=3`), handle `NEEDS_INFO`/`REJECT` transitions + GitHub comments/labels when `not dry_run`, and return `pack_batches(...)`.
  - Implement `format_batch_plan(plan)` rendering triaged verdicts and waves (`Wave 1`, `Wave 2`, …) clearly.
- In `factory/cli.py`:
  - Add `plan` subcommand (`issues` optional positional int args) calling `triage_and_plan_queue(..., dry_run=True)` and printing `format_batch_plan(plan)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_batch.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add factory/lib/batch.py factory/cli.py tests/test_batch.py
git commit -m "feat(batch): add single-call queue triage and factory plan dry-run command"
```

---

### Task 6: Hot Batch Execution, Gate/Review Loop & Cherry-Pick Commit Salvage

**Files:**
- Modify: `factory/lib/batch.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `triage_and_plan_queue`, `git_ops` (`ensure_worktree`, `remove_worktree`, `push_branch`, `sync_with_base`, `head_sha`, `repo_lock`), `pipeline` gate/protected-path/review/merge helpers (`find_protected_violations`, `run_checks_gate`, `GitHubClient`), `state` (`advance_stage`, `update_job`, `record_metrics`).
- Produces:
  - `@dataclass class BatchExecutionOutcome(batch_id: str, merged_issues: list[int], ready_issues: list[int], failed_issues: list[int], pr_url: str = "")`
  - `salvage_passing_commits(repo_root: str, worktree: str, batch_branch: str, base_branch: str, ordered_issues: list[int], issue_commits: dict[int, list[str]], hard_deps: dict[int, set[int]], gate_fn: Callable[[str], tuple[bool, str]], protected_fn: Callable[[str], list[str]]) -> tuple[list[int], dict[int, str]]`
  - `execute_batch(...) -> BatchExecutionOutcome`
  - `run_batch_factory(repo_root: str, issues: list[dict], *, config: dict, gh: GitHubClient, board: ProjectBoardSync | None = None, state_store: GitStateStore | None = None) -> list[dict]`

- [ ] **Step 1: Write the failing execution, salvage, and wave DAG tests in `tests/test_batch.py`**

Using real temporary git repos (`git init --bare` origin + local clone) and stubbed `agent_runner` / `gh`:
1. `test_execute_batch_happy_path_three_issues_non_squash_and_proportional_metrics`: `N=3` issues (`weight` 1, 2, 3), `cfg["merge_method"] == "squash"`; 1 `batch-builder` call creates 3 commits tagged `(#1)`, `(#2)`, `(#3)`, 1 `checks.sh` gate, 1 read-only `reviewer-code`; PR opens with `Closes #1`, `Closes #2`, `Closes #3`, merges using `"merge"` (not `"squash"`), updates all 3 `job.json` records to `merged` with proportional cost split summing to total batch cost, and prunes `.worktrees/batch-*`.
2. `test_execute_batch_handles_blocked_and_skipped_dep_mid_batch`: builder marks `#2` `BLOCKED` and `#3` `SKIPPED_DEP` while committing `#1` `DONE`; `#1` merges cleanly while `#2` and `#3` transition to `needs-human`.
3. `test_execute_batch_audits_missing_commit_for_done_issue`: builder returns `DONE` for `#1` without creating a commit; reclassified as `BLOCKED` -> `needs-human`.
4. `test_salvage_passing_commits_drops_broken_commit_and_ships_healthy_subset`: `#1`, `#2`, `#3` committed in order, `#2` breaks `checks.sh` even after fix round; `salvage_passing_commits` cherry-picks `#1` and `#3` onto clean `origin/main`, drops `#2` (`needs-human`), and ships `#1` + `#3`.
5. `test_run_batch_factory_skips_downstream_dependent_of_failed_parent`: Wave 1 Batch A has `#1` (merges) and `#2` (fails), Wave 2 Batch B has `#3` (depends on `#2`) and `#4` (depends on `#1`); Wave 2 escalates `#3` to `needs-human` before building and builds/merges `#4`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_batch.py -k "execute_batch or salvage or run_batch_factory" -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `salvage_passing_commits()`, `execute_batch()`, and `run_batch_factory()` in `factory/lib/batch.py`**

Implement Spec §§4.4–4.6 & 5 cleanly:
- Check `check_halted` / `QuotaExhaustedError` before triage and before each batch.
- Run `salvage_passing_commits()` in a clean worktree/temp branch reset to `origin/<base_branch>`, cherry-picking per-issue commits in topological order and running `protected_fn` + `gate_fn` after each issue.
- Share `repo_lock(repo_root)` during pre-merge rebase (`sync_with_base`) + post-rebase gate + `gh.merge_pr`, overriding `"squash"` -> `"merge"` when `len(surviving) > 1`.
- Attribute batch stage metrics across participating `job-<n>/job.json` proportionally by `complexity_weight / total_weight` via `record_metrics()`.
- Schedule batches through `graphlib.TopologicalSorter(plan.batch_deps)` with `ThreadPoolExecutor(max_workers=cfg["batch"]["max_parallel_batches"])`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_batch.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add factory/lib/batch.py tests/test_batch.py
git commit -m "feat(batch): implement hot batch execution, commit salvage, and Batch-DAG runner"
```

---

### Task 7: Wire `run_factory()` & CLI Flags (`--no-batch`, `--max-batch`) + Full Suite Verification

**Files:**
- Modify: `factory/lib/pipeline.py:3370-3525`
- Modify: `factory/cli.py:106-112, 340-390`
- Test: `tests/test_batch.py`, `tests/test_multi_repo_cli.py`

**Interfaces:**
- Consumes: `batch.run_batch_factory`.
- Produces: `run_factory(repo_root: str, issue_number: int | None = None, issue_numbers: list[int] | None = None, *, batch_mode: bool | None = None, max_batch_issues: int | None = None) -> list[dict]` and CLI flags `--no-batch`, `--max-batch`.

- [ ] **Step 1: Write the failing `run_factory` & CLI flag integration tests**

Add to `tests/test_batch.py`:
```python
def test_run_factory_uses_batch_by_default_and_respects_no_batch(monkeypatch, tmp_path):
    # Verify default run_factory invokes run_batch_factory, while batch_mode=False
    # (and `factory run --no-batch`) invokes legacy process_job path.
```
Include assertions for `--max-batch 2` overriding `cfg["batch"]["max_batch_issues"]`, and for existing mid-flight jobs (`stage="build"` / `"ready"`) still reconciling/resuming via `process_job()` before batch triage runs.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_batch.py::test_run_factory_uses_batch_by_default_and_respects_no_batch -v`
Expected: FAIL (`TypeError: run_factory() got an unexpected keyword argument 'batch_mode'`).

- [ ] **Step 3: Wire `run_factory()` in `factory/lib/pipeline.py` and CLI flags in `factory/cli.py`**

- In `factory/cli.py`: add `--no-batch` (`action="store_true"`) and `--max-batch` (`type=int`, `default=None`) to the `run` subparser and pass `batch_mode=False if args.no_batch else None, max_batch_issues=args.max_batch` into `run_factory()`.
- In `factory/lib/pipeline.py`:
  - Add optional `batch_mode: bool | None = None` and `max_batch_issues: int | None = None` keyword parameters to `run_factory()`.
  - Apply `max_batch_issues` override to `cfg["batch"]["max_batch_issues"]` if provided.
  - Determine `use_batch = bool(cfg.get("batch", {}).get("enabled", True)) if batch_mode is None else bool(batch_mode)`.
  - When `use_batch` is `True`: reconcile `ready` jobs as usual; separate any explicit/existing mid-flight jobs (`stage not in {"queued"}` already on disk) to resume via `process_job()`; pass remaining newly queued issues to `run_batch_factory()` (respecting `HerdrBridge` / `PYTEST_CURRENT_TEST` guards).
  - When `use_batch` is `False`: preserve existing `process_job` DAG runner unchanged.

- [ ] **Step 4: Run targeted tests and full `factory/checks.sh` suite**

Run: `python3 -m pytest tests/test_batch.py -q && bash factory/checks.sh`
Expected: Full test suite PASS (`0` failures, `0` skipped regressions).

- [ ] **Step 5: Commit**

```bash
git add factory/lib/pipeline.py factory/cli.py tests/test_batch.py
git commit -m "feat(batch): make smart batch mode default in run_factory and CLI"
```
