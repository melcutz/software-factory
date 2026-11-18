# Coder Tiers Design Note (OpenCode + GPU + Fallback Chains)

**Constraint:** Python 3.10+ stdlib only (no new runtime dependencies); backward compatible with plain-string `models.<role>` configs (e.g. `"gemini-3.8-flash-medium"`).

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
        "cost_usd": float | None,     # optional direct cost from provider
    },
    "duration_seconds": float,
}
```

### 2. Returned `usage` Dict (public return of `run_agent`)
`run_agent(...) -> tuple[dict, dict, float]` enriches `usage` with:
- `provider`: `"agy" | "opencode" | "opencode-go" | "openrouter" | "gpu"`
- `tier`: configured `"tier"` from `model_pricing` if present, else `"local"` (`gpu:*`), `"free"` (`*-free`, `*:free`, `opencode/big-pickle`), or `"standard"`
- `model`: `str` (the exact model spec that succeeded, e.g. `"opencode/minimax-m2.5-free"`, `"gpu:decision-model"`, `"gemini-3.8-flash-medium"`)
- `priced`: `bool` (`True` when a `model_pricing` entry or direct `cost_usd` matched, else `False`)

### 3. Cooldown Buckets (`refs/factory/quota`)
`quota_bucket_for_model(model_spec: str, config: dict | None = None) -> str` in `factory/lib/governor.py` evaluates `config["quota"]["buckets"]` glob rules before falling back to `normalize_base_model`:
- `gpu:*` -> `"gpu"`
- `opencode-go/*` -> `"opencode-go"`
- `openrouter/*:free` -> `"openrouter-free"`
- `opencode/*-free` and `opencode/big-pickle` -> `"opencode-free"`
- `gemini-*` (or any other `agy` / non-free spec) -> `normalize_base_model(spec)`

Cooldown durations come from `quota.buckets[].cooldown_sec` (if non-null), else provider-reported `retry-after` if parseable, else `quota.default_cooldown_sec` (default `3600`).

### 4. Chain Semantics & Exception Flow
For a chain `[m_1, m_2, ...]`:
1. If `state_store` is present, check `is_model_in_cooldown(quota_state, m_i, config=cfg)`. If in cooldown, skip `m_i` without calling it.
2. Resolve provider timeout from `config["provider_timeouts"]` (falling back to `timeout_sec` when `None` or unset).
3. Call `run_agent(...)` (or injected `runner` in tests/batch).
4. **Move on to `m_{i+1}`** on:
   - `GpuAbstain`
   - `QuotaExhaustedError` (recording cooldown in `refs/factory/quota` using the resolved bucket and cooldown)
   - `AgentExecutionError` **except** `"illegally moved HEAD"`
   - `TimeoutError` / `subprocess.TimeoutExpired`
5. **Abort immediately (do NOT move on)** on:
   - `JobHaltedError`
   - `LeaseLostError`
   - Any exception raised/returned by `abort_check`
   - `AgentExecutionError` whose message contains `"illegally moved HEAD"`
   - Any unexpected programming error (`KeyError`, `TypeError`, etc.)
6. If every model in the chain was skipped due to active cooldowns, raise `QuotaExhaustedError`.
7. If every attempted model failed, re-raise the last exception.

---

## Architecture Summary

### 1. Coder Dispatcher (`factory/lib/agy_runner.py`)
- `classify_model_provider(model_spec: str)` routes prefixes:
  - `opencode/`, `opencode-go/`, `openrouter/` -> `opencode_runner.run_opencode_turn(...)`
  - `gpu:` -> `gpu_runner.run_gpu_stage(...)`
  - else -> `agy` path.
- Common wrapper handles HEAD guard, worktree cleanup, attempt logging, `validate_stage_output`, and usage enrichment (`provider`, `tier`, `model`, `priced`).
- Subprocess environments prepend only configured `path_prepend` directories (with `~` expanded).

### 2. OpenCode Runner (`factory/lib/opencode_runner.py`)
- `resolve_opencode_bin(config=None, path=None)` checks `FACTORY_OPENCODE_BIN`, then `providers.opencode.bin`, then `opencode` on `PATH` (including `path_prepend`).
- Parses JSONL events (`sessionID`, `text`, `step_finish` tokens/cost), extracts JSON from fenced or trailing blocks, validates against stage schema, and re-asks once in-session on validation failure.
- `detect_opencode_error` inspects provider error events and stderr for rate-limit/quota signals and deprecated/missing model errors, resolving `(bucket, cooldown_sec)` from `config["quota"]`.

### 3. Local Decision MCP Runner (`factory/lib/gpu_runner.py`)
- Disabled by default (`gpu.mcp_url = None`). When unset, `gpu:*` entries raise `GpuAbstain` immediately so chains fall through.
- `resolve_gpu_mcp_url` checks `FACTORY_GPU_MCP_URL`, then `config["gpu"]["mcp_url"]` (e.g. `http://localhost:PORT/mcp`).
- `gpu.tools` configures tool names (`decide`, `classify`, optional `status`). When `status` is unset, `gpu_available` checks MCP reachability via `initialize` / `tools/list`.
- Restricted to `intake`, `approver`, and `planner` (`GPU_ALLOWED_ROLES`). Abstains whenever confidence is below `gpu.min_confidence` (default `0.9`) or risk/gate/review checks are not cleanly low-risk/passing.

### 4. Config Layering, Pricing, and Stats (`factory/lib/config.py`, `state.py`, `cli.py`)
- Layered merge order (lowest to highest): `DEFAULT_CONFIG` -> `$FACTORY_USER_CONFIG` / `~/.config/factory/config.json` -> `<repo>/factory/config.json` -> `<repo>/factory/config.local.json`.
- `model_pricing` defaults to `{}` (no hardcoded prices). Unpriced models record `$0.0` and `priced: false`.
- `record_usage` accumulates `by_tier` and `by_provider` counters; `factory stats` aggregates them into human-readable and `--json` output.
- `factory doctor` reports loaded config layer paths (without values), checks `opencode` when configured, and reports `gpu` status (`disabled` when `gpu.mcp_url` is unset, or reachability when set).
