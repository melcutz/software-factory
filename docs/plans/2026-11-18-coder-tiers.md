# Coder Tiers Implementation Plan (OpenCode + GPU + Fallback Chains)

**Branch:** `rocket/coder-tiers`  
**Reference:** `~/work/sf-coders-ref/SPEC.md`  
**Constraint:** Python 3.10+ stdlib only (no new runtime dependencies); 100% backward compatible with plain-string `models.<role>` configs (e.g. `"gemini-4-hydrogen-medium"`).

---

## Baseline Verification

Ran `python3 -m pytest -q` on `rocket/coder-tiers`:
- **Result:** `175 passed, 20 subtests passed, 1 failed` in `66.97s`.
- **Single failure:** `tests/test_worker_and_herdr.py::test_fleet_update_runs_git_pull_on_all_machines` (`Rocket: FAILED (rc=1) fatal: couldn't find remote ref rocket/coder-tiers`) — the known branch-induced failure documented in `SPEC.md`.

---

## Cross-Cutting Data Shapes & Contracts

### 1. Provider Envelope (internal between provider runners and `agy_runner.run_agent`)
Every provider (`agy`, `opencode`, `gpu`) returns a dict envelope to `run_agent`:
```python
{
    "status": "SUCCESS",              # or non-SUCCESS on failure
    "structured_output": dict,        # raw JSON dict before validate_stage_output()
    "usage": {
        "input_tokens": int,
        "output_tokens": int,
        "thinking_tokens": int,
        "cache_read_tokens": int,
        "total_tokens": int,
        "cost_usd": float | None,     # optional direct cost from provider (e.g. opencode step_finish)
    },
    "duration_seconds": float,
}
```

### 2. Returned `usage` Dict (public return of `run_agent`)
`run_agent(...) -> tuple[dict, dict, float]` enriches `usage` with:
- `provider`: `"agy" | "opencode" | "opencode-go" | "openrouter" | "gpu"`
- `tier`: `"subscription" | "free" | "paid" | "local"`
- `model`: `str` (the exact model spec that succeeded, e.g. `"opencode/deepseek-v4.1-flash-free"`, `"gpu:jev"`, `"gemini-4-hydrogen-medium"`)

Classification rules (`classify_model_provider(model_spec: str) -> tuple[str, str]`):
- `gpu:*` -> `("gpu", "local")`
- `opencode-go/*` -> `("opencode-go", "subscription")`
- `openrouter/*` -> `("openrouter", "free" if spec.endswith(":free") else "paid")`
- `opencode/*` -> `("opencode", "free" if (spec.endswith("-free") or spec == "opencode/big-pickle") else "paid")`
- anything else -> `("agy", "paid")`

### 3. Cooldown Buckets (`refs/factory/quota`)
`quota_bucket_for_model(model_spec: str) -> str` in `factory/lib/governor.py`:
- `opencode/*` free (`spec.endswith("-free")` or `spec == "opencode/big-pickle"`) -> `"opencode-free"` (shared daily cap across all Zen free models)
- `openrouter/*:free` -> `"openrouter-free"`
- `opencode-go/*` -> `"opencode-go"`
- `gpu:*` -> `"gpu"`
- `gemini-*` (or any other `agy` / non-free spec) -> `normalize_base_model(spec)` (existing base-model behavior, e.g. `"gemini-4-argon-high"` -> `"gemini-4-argon"`).

### 4. Chain Semantics & Exception Flow
For a chain `[m_1, m_2, ...]`:
1. If `state_store` is present, check `is_model_in_cooldown(quota_state, m_i)` (which resolves via `quota_bucket_for_model(m_i)`). If in cooldown, skip `m_i` without calling it (log skip).
2. Resolve provider timeout from `config["provider_timeouts"]` (falling back to `timeout_sec` when `None` or unset).
3. Call `run_agent(...)` (or injected `runner` in tests/batch).
4. **Move on to `m_{i+1}`** on:
   - `GpuAbstain`
   - `QuotaExhaustedError` (also record cooldown in `refs/factory/quota` using `quota_bucket_for_model(m_i)` and `exc.retry_after_sec` if `state_store` is present, OR if all models fail with `QuotaExhaustedError`, re-raise so outer handler records cooldown; recording per-model on fallthrough ensures a subsequent model in the same job or another worker sees the exhausted bucket immediately)
   - `AgentExecutionError` **except** `"illegally moved HEAD"`
   - `TimeoutError` / `subprocess.TimeoutExpired`
5. **Abort immediately (do NOT move on)** on:
   - `JobHaltedError`
   - `LeaseLostError`
   - Any exception raised/returned by `abort_check`
   - `AgentExecutionError` whose message contains `"illegally moved HEAD"`
   - Any unexpected programming error (`KeyError`, `TypeError`, etc. — let fail loudly)
6. If every model in the chain was skipped due to active cooldowns, raise `QuotaExhaustedError(chain[0], retry_after_sec=min_remaining, message="All models in chain are in quota cooldown")`.
7. If every attempted model failed, re-raise the last exception (if all attempted failures were `QuotaExhaustedError`, re-raise the last `QuotaExhaustedError` so existing `pipeline.py` quota-checkpointing works unchanged).

---

## Scope Items 1–8

### Item 1: Coder Dispatcher in `factory/lib/agy_runner.py`
**Files:** `factory/lib/agy_runner.py`

**Changes:**
- Add `classify_model_provider(model_spec: str) -> tuple[str, str]`.
- Extend `QuotaExhaustedError.__init__(self, model_spec: str, retry_after_sec: int | None = None, message: str = "", bucket: str | None = None)` storing `self.bucket = bucket`.
- Generalize `_invoke_agy_process`:
  - Keep exact name and signature `_invoke_agy_process(cmd, cwd, env, hard_timeout_sec, idle_bg_grace_sec, abort_check=None, *, label: str = "agy", idle_output_timeout_sec: float | None = None)` so existing monkeypatches of `factory.lib.agy_runner._invoke_agy_process` continue to work seamlessly.
  - Pass `stdin=subprocess.DEVNULL` to `subprocess.run` and `subprocess.Popen`.
  - Support `idle_output_timeout_sec` (used by `opencode_runner` when stdout/stderr produces no bytes for too long, in addition to `_IDLE_BG_RE` for `agy`).
- Extract `_run_agy_provider(factory_dir, worktree, stage, agent_name, model, full_prompt, schema, timeout_sec, sandbox, proc_env, effective_idle_grace, abort_check) -> tuple[int, str, str, str | None, dict | None]` (or returning envelope after parsing).
- Dispatch in `run_agent(...)` by prefix:
  - `model.startswith(("opencode/", "opencode-go/", "openrouter/"))` -> `opencode_runner.run_opencode_turn(...)`
  - `model.startswith("gpu:")` -> `gpu_runner.run_gpu_stage(...)`
  - else -> existing `agy` path.
- Common wrapper in `run_agent`:
  - `guard_head = sandbox or protect_head`; `initial_sha = head_sha(worktree) if guard_head else ""`
  - `try ... finally` HEAD guard + `clean_worktree` reset (unchanged).
  - Log attempt record to `logs_dir/{effective_log}.json` (and `-retry.json`).
  - `validate_stage_output(stage, raw_structured)` for all providers.
  - Enrich `usage` with `provider`, `tier`, `model`.
  - Retry behavior: `agy` keeps 2 attempts; `gpu:` and `opencode*` do 1 outer attempt (`GpuAbstain` and `QuotaExhaustedError` never retry; `opencode` handles its 1 in-session re-ask internally).
- Prepend `~/.opencode/bin` and `~/.local/bin` to `proc_env["PATH"]` if those directories exist (when building `proc_env` in `run_agent` / `opencode_runner`).

---

### Item 2: `factory/lib/opencode_runner.py` (New)
**Files:** `factory/lib/opencode_runner.py`

**New Functions & Signatures:**
- `resolve_opencode_bin() -> str`: check `shutil.which("opencode")`, then `~/.opencode/bin/opencode`, `~/.local/bin/opencode`, else `"opencode"`.
- `route_free_models(task_kind: str = "default") -> list[str]`:
  Port `ocrun.sh`'s `route()` with `task_kind` in `{"lint", "ui", "longctx", "tests", "plan", "default"}` using the Nov 18 2026 OpenRouter `:free` models first, then `opencode/*` Zen free models:
  - `lint`: `["openrouter/cohere/north-mini-code:free", "openrouter/nvidia/nemotron-3.5-super-vl:free", "opencode/deepseek-v4.1-flash-free", "opencode/mimo-v2.6-flash-free", "opencode/big-pickle"]`
  - `ui`: `["openrouter/nvidia/nemotron-3.5-super-vl:free", "openrouter/google/gemma-4-31b-it:free", "opencode/nemotron-3.5-super-vl-free", "opencode/deepseek-v4.1-flash-free", "opencode/inkling-2-small-free"]`
  - `longctx`: `["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter/thinkingmachines/inkling-2-small:free", "opencode/deepseek-v4.1-flash-free", "opencode/longcat-2.5-free", "opencode/hy4-flash-free"]`
  - `tests`: `["openrouter/cohere/north-mini-code:free", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "opencode/deepseek-v4.1-flash-free", "opencode/nemotron-3.5-super-vl-free", "opencode/flint-40b-free"]`
  - `plan`: `["openrouter/thinkingmachines/inkling-2-small:free", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "opencode/deepseek-v4.1-flash-free", "opencode/hy4-flash-free", "opencode/iceman-free"]`
  - `default` (any other): `["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter/thinkingmachines/inkling-2-small:free", "openrouter/cohere/north-mini-code:free", "opencode/deepseek-v4.1-flash-free", "opencode/mimo-v2.6-flash-free", "opencode/beam-free"]`
- `build_opencode_prompt(full_prompt: str, schema: dict) -> str`:
  Append schema JSON and `"Reply with ONLY one JSON object matching this schema."`
- `parse_opencode_jsonl(stdout_text: str) -> tuple[str, str | None, dict]`:
  Returns `(concatenated_text, session_id, usage_dict)`:
  - Parse each JSON line; ignore non-JSON lines.
  - Capture `sessionID` (top-level `ev.get("sessionID")` or `part.get("sessionID")`).
  - When `ev.get("type") == "text"` (or `part.get("type") == "text"`), append `part.get("text", "")`.
  - When `ev.get("type") == "step_finish"` (or `part.get("type") == "step-finish"`):
    sum `part.get("cost", 0)` into `cost_usd`, and `part.get("tokens", {})` (`input` -> `input_tokens`, `output` -> `output_tokens`, `reasoning` -> `thinking_tokens`, `cache.read` -> `cache_read_tokens`). Compute `total_tokens = input_tokens + output_tokens + thinking_tokens + cache_read_tokens`.
- `extract_json_from_text(text: str) -> dict`:
  Strip/search ```` ```json ```` or ```` ``` ```` code fences first, then fall back to `json.JSONDecoder().raw_decode` scanning backwards from the last `{` to extract the last valid JSON dict.
- `detect_opencode_error(model_spec: str, stdout_text: str, stderr_text: str) -> Exception | None`:
  Port `ocrun.sh` regexes:
  - If `model_spec.startswith("openrouter/")` and matches `rate limit|429|free-models-per|limit exceeded|insufficient credits|quota` -> `QuotaExhaustedError(model_spec, retry_after_sec=10800, bucket="openrouter-free")`
  - If matches `Free usage limit` (or `model_spec.startswith("opencode/")` free model + `free-models-per|rate limit|429`) -> `QuotaExhaustedError(model_spec, retry_after_sec=10800, bucket="opencode-free")`
  - If `model_spec.startswith("opencode-go/")` and matches `usage limit|weekly limit|limit reached|quota` -> `QuotaExhaustedError(model_spec, retry_after_sec=86400, bucket="opencode-go")`
  - If matches `deprecated|ProviderModelNotFound|model not found|requires Global regions` -> `AgentExecutionError(...)`
- `run_opencode_turn(worktree: str, stage: str, model: str, full_prompt: str, schema: dict, timeout_sec: int, *, extra_env: dict[str, str] | None = None, idle_grace_sec: float = 600.0, abort_check=None) -> dict`:
  - Runs `opencode run -m <model> --format json --auto <prompt>` via `_invoke_agy_process` (with `stdin=subprocess.DEVNULL`, `start_new_session=True`, `cwd=worktree`).
  - Checks `detect_opencode_error` on non-zero exit or error output.
  - Parses JSONL -> extracts JSON -> validates with `validate_stage_output(stage, parsed)`.
  - On JSON extraction or schema validation failure: if `session_id` is present, re-asks ONCE with `opencode run -m <model> --format json --auto --session <session_id> "Validation error: <err>. Reply with only the corrected JSON."`, accumulates usage/duration, and re-validates. On second failure raises `AgentExecutionError`.
  - Returns envelope `{status: "SUCCESS", structured_output, usage, duration_seconds}`.

---

### Item 3: `factory/lib/gpu_runner.py` (New)
**Files:** `factory/lib/gpu_runner.py`

**New Classes & Functions:**
- `class GpuAbstain(RuntimeError): pass`
- `resolve_gpu_mcp_url(factory_dir: str | None = None, config: dict | None = None) -> str`:
  Precedence: `os.environ.get("FACTORY_GPU_MCP_URL")` -> `(config or {}).get("gpu", {}).get("mcp_url")` -> `"http://rocket-ubuntu:8000/mcp"`.
- `class McpStreamableClient`:
  - `__init__(self, url: str, connect_timeout: float = 5.0, call_timeout: float = 60.0)`
  - `_rpc(self, method: str, params: dict | None = None, req_id: int | None = 1, timeout: float | None = None) -> dict | None`:
    POST JSON-RPC 2.0 with `Content-Type: application/json`, `Accept: application/json, text/event-stream`, and `Mcp-Session-Id` header once obtained. Parses both SSE (`data: {...}` lines) and plain JSON responses.
  - `call_tool(self, name: str, arguments: dict) -> dict`: runs `initialize` (with `connect_timeout`), `notifications/initialized`, then `tools/call` (with `call_timeout`), returning parsed tool result JSON.
- `gpu_available(mcp_url: str, *, cache_ttl_sec: float = 60.0, force: bool = False) -> bool`:
  Module-level TTL cache keyed by `mcp_url`. Calls `local_gpu_status`; returns `True` only if reachable, no error, and `"minicpm5"` appears in the status/models payload. Never raises; returns `False` on any network/parse error.
- `clear_gpu_availability_cache() -> None` (for tests).
- `decide(mcp_url: str, state: str, decisions: list[dict], *, timeout_sec: float = 60.0) -> dict`:
  Wraps `local_system1_jev_decide`.
- `classify(mcp_url: str, items: list[dict], allowed_categories: list[str], *, timeout_sec: float = 60.0) -> dict`:
  Wraps `local_batch_classify_and_filter`.
- `run_gpu_stage(stage: str, model: str, task_prompt: str, *, mcp_url: str, min_confidence: float = 0.90, timeout_sec: float = 60.0) -> dict`:
  - Checks `gpu_available(mcp_url)`; if `False`, raises `GpuAbstain("GPU MCP unavailable or minicpm5 not installed")`.
  - Stage adapters:
    - `"intake"`: calls `decide()` with issue text and decisions (`accept` bool, `risk` choice `["low", "medium", "high"]`, `touches_ui` bool, `touches_security` bool).
      - Only returns `ACCEPT` when `p_accept >= min_confidence` (0.90).
      - `risk` = argmax of `low`/`medium`/`high`.
      - `surfaces` = `["security"]` / `["ui"]` where bool probability `>= 0.5`.
      - Deterministic text fields: `summary=[f"GPU fast-path triage (p={p_accept:.2f})"]`, `scope_in="Issue is clear and in-scope per GPU fast-path triage."`, `scope_out="None specified."`, `questions=[]`, `reasoning=f"GPU fast-path triage (p={p_accept:.2f})"`.
      - Otherwise raises `GpuAbstain`.
    - `"approve"`:
      - Deterministically checks prompt for `RISK: low` (or `risk: low` in Intake) and that `## Gate Status` has `VERDICT: PASS` and all `## Reviews` have `VERDICT: PASS` (no `VERDICT: CHANGES`). If not, raises `GpuAbstain` immediately.
      - Calls `decide()` for `safe_to_merge` and `merge_danger`.
      - Only returns `APPROVE` when `p_safe >= min_confidence` (0.90) and `p_danger < 0.10`.
      - Otherwise raises `GpuAbstain` (never returns `ESCALATE`).
    - `"plan"`:
      - Counts `## Issue #` blocks in `task_prompt`. If `<= 1`, returns `{"edges": [], "predicted_surfaces": [], "reasoning": "GPU fast-path: at most one issue, no dependency edges."}`.
      - Otherwise raises `GpuAbstain`.
    - Any other stage -> raises `GpuAbstain(f"GPU tier unsupported for stage {stage!r}")`.
  - Returns envelope with `usage={"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}` (and `run_agent` tags `provider="gpu"`, `tier="local"`, `model=model`).

---

### Item 4: List-Valued Fallback Chains & Config Validation
**Files:** `factory/lib/config.py`, `factory/lib/pipeline.py`, `factory/lib/batch.py`, `factory/lib/worker.py`

**`factory/lib/config.py`:**
- Add `GPU_ALLOWED_ROLES = {"intake", "approver", "planner"}` and `KNOWN_MODEL_PREFIXES = ("gemini-", "opencode/", "opencode-go/", "openrouter/", "gpu:")`.
- Add `DEFAULT_PROVIDER_TIMEOUTS = {"agy": None, "opencode": 1800, "opencode-go": 1800, "openrouter": 1200, "gpu": 60}`.
- Add `"provider_timeouts"` and `"gpu": {"mcp_url": "http://rocket-ubuntu:8000/mcp", "min_confidence": 0.9}` to `DEFAULT_CONFIG` (merged as dict keys in `load_config`).
- Add `model_chain(config: dict, role: str) -> list[str]`:
  - If `models[role]` is `None`, returns `[]`.
  - If `str`, returns `[val.strip()]` (if non-empty).
  - If `list`, returns `[s.strip() for s in val]`.
- Add `provider_timeout_sec(config: dict, model_spec: str, default_timeout_sec: int) -> int`:
  Looks up `provider` from `classify_model_provider(model_spec)` in `config.get("provider_timeouts")`; returns `int(val)` if non-None, else `int(default_timeout_sec)`.
- Update `resolve_reviewers(config: dict, risk: str, surfaces: list[str]) -> dict[str, list[str]]` to return `model_chain(config, role)` for each selected reviewer role.
- `load_config` validation rules:
  - `models.<role>`: must be `None` (only allowed for `builder_low`), a non-empty `str`, or a non-empty `list[str]` of non-empty strings.
  - Every model spec must start with one of `KNOWN_MODEL_PREFIXES` (raise `ValueError` otherwise).
  - `gpu:` prefix is rejected (`ValueError`) in any role outside `GPU_ALLOWED_ROLES` (`intake`, `approver`, `planner`).
  - `provider_timeouts`: must be a dict; each value must be `None` or a positive `int` (not `bool`).
  - `gpu`: must be a dict; `gpu.mcp_url` must be an `http://` or `https://` URL with non-empty hostname; `gpu.min_confidence` must be a number in `(0.0, 1.0]`.

**Chain Runner & Every `run_agent` Call Site:**
- Add `run_agent_chain(...)` helper (in `pipeline.py` or `agy_runner.py` — putting `run_agent_chain` in `pipeline.py` or `agy_runner.py` and accepting an optional `runner_fn` makes both `pipeline.py` and `batch.py` clean; putting it in `agy_runner.py` with `runner_fn=None` defaulting to `run_agent` avoids circular imports and lets `pipeline.py` pass `_call_run_agent` and `batch.py` pass `runner`!).
  - `run_agent_chain(factory_dir, job_dir, worktree, stage, agent_name, models, task_prompt, schema, timeout_sec, *, config=None, state_store=None, runner_fn=None, log_fn=None, **run_kwargs) -> tuple[dict, dict, float]`
  - Accepts `models: str | list[str]` (normalizes `str` -> `[str]`).
  - Iterates models in order, checks `state_store.get_quota_state()` cooldowns (skipping models whose bucket is in cooldown), resolves per-provider timeout via `provider_timeout_sec(config, m, timeout_sec)`, invokes `runner_fn(...)`, and implements the exact exception flow above.
- **Every `run_agent` / `_call_run_agent` call site to make chain-aware:**
  1. `pipeline.py:_sync_pr_with_base` (line 487, conflict-resolve `builder`)
  2. `pipeline.py:run_staging_qa` (line 1571, `qa`)
  3. `pipeline.py:process_job` `stage == "intake"` (line 2373, `intake`)
  4. `pipeline.py:process_job` `stage == "spec"` (line 2438, `spec_writer`)
  5. `pipeline.py:process_job` `stage == "spec"` retry on protected paths (line 2481, `spec_writer`)
  6. `pipeline.py:process_job` `stage == "build"` slice loop (line 2602, `select_builder_chain`)
  7. `pipeline.py:process_job` `stage == "build"` whole build (line 2792, `select_builder_chain`)
  8. `pipeline.py:process_job` `stage == "fix"` (line 2892, `select_builder_chain`)
  9. `pipeline.py:process_job` `stage == "review"` (line 3002, `resolve_reviewers` chains)
  10. `pipeline.py:process_job` `stage == "approve"` (line 3120, `approver`)
  11. `pipeline.py:plan_queue_dependencies._run_planner` (line 3401, `planner`)
  12. `batch.py:triage_and_plan_queue` (line 354, `planner`)
  13. `batch.py:_review_batch` (line 703, `resolve_reviewers` chains)
  14. `batch.py:execute_batch` (line 768, `batch_builder` / `builder_low` chain)
  15. `batch.py:run_batch_dag._planner_hook` (line 992, passes chain to `runner`)
- At each call site, use `used_model = usage.get("model") or chain[0]` for `_clear_quota_cooldown_if_needed(state_store, used_model)` and `_record_and_format_step(jobs_dir, job_id, used_model, usage, dur, cfg)`.
- Add `select_builder_chain(cfg, job, *, attempt, is_fix_round=False, is_conflict=False) -> list[str]` in `pipeline.py`, and keep `select_builder_model(...) -> str` returning `select_builder_chain(...)[0]` for backward compatibility with `tests/test_reliability_and_efficiency.py::test_select_builder_model_tiering`.
- Update `worker.py` (lines 398-407): check `model_chain(cfg, role)` for `("intake", "spec_writer", "builder")` — only mark `in_cd = True` when **all** models in a role's chain are in cooldown.

---

### Item 5: Pricing & `factory stats` Per-Tier Breakdown
**Files:** `factory/lib/config.py`, `factory/lib/state.py`, `factory/cli.py`

**`factory/lib/config.py`:**
- Update `DEFAULT_MODEL_PRICING`:
  - `"gemini-3.6-flash"`: `{"input_per_m": 0.15, "cached_per_m": 0.0375, "output_per_m": 0.60}` (kept)
  - `"gemini-3.8-flash"`: `{"input_per_m": 0.30, "cached_per_m": 0.075, "output_per_m": 2.50}` (kept)
  - `"gemini-4-hydrogen"`: `{"input_per_m": 1.50, "cached_per_m": 0.375, "output_per_m": 7.50}`
  - `"gemini-4-argon"`: `{"input_per_m": 2.00, "cached_per_m": 0.50, "output_per_m": 10.00}`
  - `"gemini-4-carbon"`: `{"input_per_m": 2.00, "cached_per_m": 0.50, "output_per_m": 10.00}`
- Update `estimate_usage_cost_usd(model_spec, usage, config=None) -> float | None`:
  - Direct `cost_usd` / `cost` > 0 from `usage` still takes precedence (covers `opencode` `step_finish` direct cost).
  - `gpu:*`, `opencode/*-free`, `opencode/big-pickle`, `openrouter/*:free` -> return `0.0`.
  - `opencode-go/*` -> return `0.0` (subscription: $0 marginal cost, tokens still counted).
  - `cached_per_m` defaults to `in_rate * 0.25` when omitted (already implemented).

**`factory/lib/state.py`:**
- In `record_usage(jobs_dir, job_id, usage, duration_seconds, cost_usd=None)`:
  - If `usage` contains `provider` / `tier`, accumulate per-tier and per-provider counters on `job["usage"]["by_tier"]` and `job["usage"]["by_provider"]` (each storing `calls`, `total_tokens`, `cost_usd`, `duration_seconds`), while keeping top-level `job["usage"]` keys unchanged.

**`factory/cli.py`:**
- In `factory stats` (`args.command == "stats"`):
  - Aggregate `by_tier` and `by_provider` across jobs into `summary["by_tier"]` and `summary["by_provider"]` in `--json` output, and print a concise `Tiers:` / `Providers:` breakdown line in human-readable output.

---

### Item 6: Provider-Aware Governor Cooldowns
**Files:** `factory/lib/governor.py`, `factory/lib/pipeline.py`, `factory/lib/worker.py`

- Add `quota_bucket_for_model(model_spec: str) -> str` in `factory/lib/governor.py`:
  - Maps `opencode/*-free` & `opencode/big-pickle` -> `"opencode-free"`, `openrouter/*:free` -> `"openrouter-free"`, `opencode-go/*` -> `"opencode-go"`, `gpu:*` -> `"gpu"`, otherwise `normalize_base_model(model_spec)`.
- Update `is_model_in_cooldown(quota_state, base_model, *, now_ts=None)` in `governor.py` to also check `quota_bucket_for_model(base_model)` (preserving direct key lookup for backward compatibility).
- Update `pipeline.py`:
  - `_clear_quota_cooldown_if_needed(state_store, model_spec)` uses `quota_bucket_for_model(model_spec)`.
  - `except QuotaExhaustedError as exc:` (line 3321) uses `exc.bucket or quota_bucket_for_model(exc.model_spec)` when calling `state_store.record_quota_cooldown(...)`.
- `GitStateStore.record_quota_cooldown` already respects `retry_after_sec` via `max(QUOTA_BACKOFF_SCHEDULE[idx], int(retry_after_sec or 0))` — verify that `retry_after_sec=10800` (3h) and `86400` (24h) are preserved.

---

### Item 7: Worker PATH & `factory doctor` Checks
**Files:** `factory/lib/worker.py`, `factory/lib/agy_runner.py`, `factory/lib/pipeline.py`

- **PATH:** Prepend `~/.opencode/bin` (alongside `~/.local/bin`) in:
  - `worker.py:exec_job_command`
  - `agy_runner.py` (`run_agent` / `opencode_runner` subprocess env)
  - `herdr_bridge.py`: check if `herdr_bridge.py` spawns with env; `exec_job_command` runs inside the `hc` child terminal and sets `os.environ["PATH"]` before `process_job`. Also add a small helper `ensure_user_bins_on_path()` or prepend in `herdr_bridge.py` if needed.
- **`factory doctor` (`pipeline.py:run_doctor`):**
  - Inspect all configured chains in `cfg["models"]`:
    - If any chain contains `opencode/`, `opencode-go/`, or `openrouter/`: resolve `resolve_opencode_bin()` and run `[oc_bin, "--version"]`; fail (`ok = False`) if missing or non-zero exit.
    - If any chain contains `gpu:`: call `gpu_runner.gpu_available(resolve_gpu_mcp_url(config=cfg), force=True)` (or inspect `local_gpu_status`); emit `WARN: ...` (do **not** set `ok = False`) when unreachable or `minicpm5` is missing, and `OK: ...` when healthy.

---

### Item 8: Default `factory/config.json`, `DEFAULT_CONFIG`, and Docs
**Files:** `factory/config.json`, `factory/lib/config.py`, `README.md`, `INSTALL.md`

- Update `factory/config.json` `models` to the exact Gemini 4 + OpenCode + GPU chains from `SPEC.md` item 8, plus `"provider_timeouts"` and `"gpu": {"mcp_url": "http://rocket-ubuntu:8000/mcp", "min_confidence": 0.9}`.
- Update `DEFAULT_CONFIG` in `factory/lib/config.py` sensibly with `"provider_timeouts"`, `"gpu"`, and Gemini 4 defaults (keeping `load_config("/nonexistent-...")` valid and backward-compatible).
- Update `README.md` and `INSTALL.md` with a concise **Coder Tiers** section covering prefixes, fallback chains, GPU abstain rules, cooldown buckets, and pricing. (Check `.agents/skills/factory/SKILL.md` — currently has no hardcoded model list.)

---

## Risks, Ambiguities & Decisions

1. **Existing `test_select_builder_model_tiering` & `test_load_repo_config_and_route_all_surfaces`:**
   - *Risk:* `test_select_builder_model_tiering` expects `select_builder_model(...) -> str`; `test_load_repo_config_and_route_all_surfaces` asserts exact strings from the old `factory/config.json`.
   - *Decision:* Keep `select_builder_model(...) -> str` returning the head of `select_builder_chain(...)` so existing unit tests stay green, and update `test_load_repo_config_and_route_all_surfaces` in Phase 2 to match the new `resolve_reviewers` (`dict[str, list[str]]`) and `factory/config.json` chains.
2. **Existing monkeypatches of `pipeline.run_agent` and `agy_runner._invoke_agy_process`:**
   - *Risk:* `test_governor_and_quota.py` and `test_reliability_and_efficiency.py` monkeypatch `factory.lib.pipeline.run_agent` and `factory.lib.agy_runner._invoke_agy_process`.
   - *Decision:* `run_agent_chain` in `pipeline.py` calls `_call_run_agent` (which delegates to `pipeline.run_agent`), and `agy_runner` / `opencode_runner` call `_invoke_agy_process`, preserving all test seams.
3. **`gpu:jev` prompt context for `approve` and `intake`:**
   - *Risk:* `run_agent` only receives `(stage, agent_name, model, task_prompt, schema, ...)`.
   - *Decision:* `gpu_runner.run_gpu_stage` parses deterministic markers already present in `approve` (`RISK:`, `## Gate Status`, `## Reviews`) and `plan` (`## Issue #`) prompts, and passes issue text to `local_system1_jev_decide` for `intake`.
4. **Cooldown recording during chain fallthrough:**
   - *Risk:* If model 1 in a chain hits `QuotaExhaustedError` and model 2 succeeds, model 1's cooldown would be forgotten unless recorded on fallthrough.
   - *Decision:* Record `state_store.record_quota_cooldown(bucket, str(exc), exc.retry_after_sec)` when catching `QuotaExhaustedError` during chain fallthrough (when `state_store` is provided), and only re-raise if the entire chain fails.

---

## Planned Test Suite (Hermetic: Fake `opencode` Binaries + Fake Threading `http.server` MCP)

- `tests/test_coder_tiers_opencode.py` (new):
  - `test_dispatcher_routes_prefixes_and_tags_usage`
  - `test_resolve_opencode_bin_path_and_home_fallbacks`
  - `test_opencode_jsonl_parsing_and_usage_summing`
  - `test_opencode_extracts_fenced_and_trailing_json`
  - `test_opencode_validation_failure_reasks_once_in_session_then_succeeds`
  - `test_opencode_validation_failure_after_reask_raises_agent_error`
  - `test_opencode_quota_mapping_free_openrouter_go_and_retry_after`
  - `test_opencode_broken_model_raises_agent_execution_error`
  - `test_route_free_models_all_task_kinds`
- `tests/test_gpu_runner.py` (new):
  - `test_mcp_client_sse_and_plain_json_responses`
  - `test_gpu_available_false_when_unreachable_or_missing_minicpm5`
  - `test_gpu_intake_accept_and_abstain_thresholds`
  - `test_gpu_approve_only_low_risk_clean_reviews_high_confidence`
  - `test_gpu_approve_abstains_on_high_risk_danger_or_low_confidence`
  - `test_gpu_planner_trivial_single_issue_vs_multi_issue_abstain`
  - `test_gpu_unsupported_stage_abstains`
- `tests/test_fallback_chains.py` (new):
  - `test_chain_falls_through_on_abstain_quota_cooldown_and_agent_error`
  - `test_chain_skips_model_in_cooldown_without_calling`
  - `test_chain_does_not_fall_through_on_halt_lease_or_head_mutation`
  - `test_chain_all_quota_exhausted_reraises_quota_error`
  - `test_worker_cooldown_gate_requires_all_models_in_chain_cooled_down`
- `tests/test_config_and_schemas.py` (additions):
  - `test_config_validation_chains_prefixes_gpu_roles_timeouts_and_mcp_url`
  - `test_pricing_gemini4_free_subscription_and_local`
- `tests/test_governor_and_quota.py` (additions):
  - `test_quota_bucket_for_model_normalizes_providers`
  - `test_is_model_in_cooldown_uses_provider_bucket`
- `tests/test_worker_and_herdr.py` / `tests/test_cli_doctor_stats.py` (additions):
  - `test_exec_job_command_prepends_opencode_and_local_bin_to_path`
  - `test_doctor_checks_opencode_version_and_warns_on_unreachable_gpu`
  - `test_stats_aggregates_by_tier_and_provider`
