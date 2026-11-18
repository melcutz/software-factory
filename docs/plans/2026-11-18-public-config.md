# Public & Configurable Factory Implementation Plan

Goal: Turn `software-factory-agy` into a general, public-ready tool where all machine-specific hosts, paths, provider binaries, GPU MCP endpoints, pricing tables, tier labels, and quota cooldowns are user-configured rather than hardcoded.

## Guardrails & Placeholders

- **Git rules:** Never push, rebase, or amend pushed commits. Commit passing progress after each logical step.
- **Test gate:** Keep the suite hermetic and passing after every commit:
  `PYTEST_ADDOPTS="--deselect tests/test_worker_and_herdr.py::test_fleet_update_runs_git_pull_on_all_machines" bash factory/checks.sh`
- **Public placeholders only:** Use `host-a`, `host-b`, `myhost`, `localhost`, `http://localhost:PORT/mcp`, `http://myhost:PORT/mcp`, `~/src/my-app`, `~/src/my-api`, `example-owner`, `my-org`, `my-app`, `example-repo`, `decision-model`. Never copy strings or regexes from any local leak denylist into tracked files, tests, docs, or commit messages.
- **Historical docs untouched:** Do not edit `docs/last-30-jobs-time-report.md`, `docs/lessons-learned.md`, `docs/medium-article.md`, `docs/medium-article.html`, or `docs/superpowers/specs/2026-11-15-multi-machine-durable-factory-design.md`.

---

## Commit 1: Config Layering (`config.py`, `onboarding.py`, `pipeline.py`, `.gitignore`)

### Changes
- **`factory/lib/config.py`**
  - Add `_user_config_path() -> str`: returns `os.environ.get("FACTORY_USER_CONFIG")` if set, else `~/.config/factory/config.json`.
  - Add `_strip_comment_keys(obj)` and `_deep_merge_config(base: dict, overlay: dict) -> dict`: recursively deep-merge dict sections (`labels`, `models`, `model_pricing`, `batch`, `staging_qa`, `provider_timeouts`, `gpu`, `quota`, `providers`, and nested dicts such as `gpu.tools`); replace scalars and lists; ignore keys starting with `_` (such as `_comment`) during load/validation.
  - Add `load_config_with_layers(factory_dir: str) -> tuple[dict, list[str]]` and have `load_config(factory_dir: str) -> dict` delegate to it. Merge order (lowest to highest):
    1. `DEFAULT_CONFIG`
    2. User/host file (`_user_config_path()`, optional)
    3. `<repo>/factory/config.json` (committed)
    4. `<repo>/factory/config.local.json` (optional, gitignored)
  - Preserve `batch_builder` fallback to `models.builder` when `batch_builder` is omitted across all layers.
- **`.gitignore` & `factory/lib/onboarding.py`**
  - Add `factory/config.local.json` to `.gitignore` and to `REQUIRED_GITIGNORE` in `factory/lib/onboarding.py` (`init_repository`).
- **`factory/lib/pipeline.py` (`run_doctor`)**
  - Call `load_config_with_layers(factory_dir)` and emit which config layer file paths were loaded, without printing config values.
- **`tests/conftest.py`**
  - In `_hermetic_coder_tiers`, set `FACTORY_USER_CONFIG` via `monkeypatch.setenv` to a nonexistent path under `tmp_path` by default so tests never read the host's `~/.config/factory/config.json`.

### Tests to Add / Update
- `tests/test_config_and_schemas.py`: test each of the 4 config layers, precedence order, deep-merge of dict sections vs scalar/list replacement, `_comment` key ignoring, and `init_repository` `.gitignore` entries.
- `tests/test_doctor_coder_tiers.py`: verify `run_doctor` reports loaded config layer paths without leaking values.

---

## Commit 2: Neutral Defaults, Pricing, Tier Labels, Quota, and PATH Resolution

### New / Updated Config Keys & Neutral Defaults (`DEFAULT_CONFIG` in `factory/lib/config.py`)
- `models`: single-provider `agy`-only neutral defaults per role (`gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, `gemini-3.6-flash-low`; `builder_low: None`, `qa: "gemini-3.8-flash-medium"`).
- `gpu`: `{"mcp_url": None, "min_confidence": 0.9, "tools": {"decide": "decide", "classify": "classify", "status": None}}`.
- `model_pricing`: `{}` (`DEFAULT_MODEL_PRICING = {}`).
- `quota`:
  ```json
  {
    "default_cooldown_sec": 3600,
    "buckets": [
      {"match": "gpu:*", "bucket": "gpu", "cooldown_sec": null},
      {"match": "opencode-go/*", "bucket": "opencode-go", "cooldown_sec": null},
      {"match": "openrouter/*:free", "bucket": "openrouter-free", "cooldown_sec": null},
      {"match": "opencode/*-free", "bucket": "opencode-free", "cooldown_sec": null},
      {"match": "opencode/big-pickle", "bucket": "opencode-free", "cooldown_sec": null}
    ]
  }
  ```
- `path_prepend`: `[]` (list of directories; `~` expanded).
- `providers`: `{"agy": {"bin": None}, "opencode": {"bin": None}}`.
- `provider_timeouts`: keep neutral defaults (`agy: None`, `opencode: 1800`, `opencode-go: 1800`, `openrouter: 1200`, `gpu: 60`).

### Changes
- **GPU MCP (`factory/lib/gpu_runner.py`, `factory/lib/config.py`, `factory/lib/agy_runner.py`, `factory/lib/pipeline.py`)**
  - Set `DEFAULT_GPU_MCP_URL = None`. Update `resolve_gpu_mcp_url(factory_dir, config)` to return `str | None` (`FACTORY_GPU_MCP_URL` env > `config["gpu"]["mcp_url"]` > `None`), loading layered config when `config` is omitted.
  - Add `resolve_gpu_tools(config)` returning `decide`, `classify`, and optional `status` tool names.
  - Update `decide`, `classify`, and `gpu_available` to use configured tool names (and when `status` is unset, check MCP reachability via `initialize` / `tools/list` without hardcoded model names).
  - In `run_gpu_stage` and `_load_gpu_settings`: when `mcp_url` is `None`/empty, raise `GpuAbstain("GPU decision MCP disabled (gpu.mcp_url not set)")` immediately so chains fall through.
  - In `load_config`: allow `gpu.mcp_url` to be `None` or a valid `http(s)` URL; validate `gpu.tools`.
  - In `pipeline.run_doctor`: when `gpu:*` is in any chain and `mcp_url` is unset, report `"OK: gpu decision MCP: disabled (gpu.mcp_url not set)"`; when set, report generic reachability status.
- **Pricing (`factory/lib/config.py`, `factory/lib/pipeline.py`, `factory/lib/batch.py`)**
  - Set `DEFAULT_MODEL_PRICING = {}`.
  - Update `estimate_usage_cost_usd(model_spec, usage, config=None)`: return `0.0` for unpriced models without crashing; mark `usage["priced"] = False` when no pricing entry matches (and `True` when priced or direct `cost_usd` is present). Keep prefix/base-model matching and `"tier"` field support on pricing entries.
- **Tier labels (`factory/lib/agy_runner.py`, `factory/lib/config.py`, `factory/lib/pipeline.py`, `factory/lib/batch.py`, `factory/lib/state.py`)**
  - In `classify_model_provider`, replace plan-specific tier labels with generic `"local"`, `"free"`, and `"standard"`.
  - Add `resolve_model_tier(model_spec: str, config: dict | None = None) -> str` in `config.py`: return `model_pricing` entry's `"tier"` if configured, else `"local"` for `gpu`, `"free"` for `-free` / `:free` (and `opencode/big-pickle`), else `"standard"`. Use it when recording `usage["tier"]`.
- **Quota & cooldowns (`factory/lib/governor.py`, `factory/lib/opencode_runner.py`, `factory/lib/agy_runner.py`, `factory/lib/pipeline.py`, `factory/lib/git_state.py`)**
  - Update `quota_bucket_for_model(model_spec, config=None)` to evaluate `config["quota"]["buckets"]` glob rules (`fnmatch`) before falling back to `normalize_base_model`.
  - Add `quota_cooldown_for_model(model_spec, config=None, retry_after_sec=None) -> tuple[str, int]`: use rule's `cooldown_sec` if non-null, else `retry_after_sec` if parseable, else `quota.default_cooldown_sec`.
  - Update `opencode_runner.detect_opencode_error` (and `run_opencode_turn` / `run_agent_chain`) to accept `config`, parse optional `retry-after` from error text, remove hardcoded cooldown durations and plan-specific wording, and resolve `(bucket, cooldown)` via `quota_cooldown_for_model`.
- **PATH & binary resolution (`factory/lib/agy_runner.py`, `factory/lib/opencode_runner.py`, `factory/lib/worker.py`, `factory/lib/fleet.py`, `factory/lib/pipeline.py`)**
  - Remove hardcoded `~/.opencode/bin` and `~/.local/bin` PATH injection from `agy_runner._build_subprocess_env`, `worker.exec_job_command`, and `fleet._factory_shell_cmd`; prepend only `config.get("path_prepend")` (with `~` expanded) when configured.
  - Update `opencode_runner.resolve_opencode_bin(config=None, path=None)`: `FACTORY_OPENCODE_BIN` > `providers.opencode.bin` > `opencode` on `PATH` (incl. `path_prepend`).
  - Update `agy_runner.resolve_agy_bin(path=None, config=None)`: `FACTORY_AGY_BIN` > `providers.agy.bin` > `agy` on `PATH` (incl. `path_prepend`).
  - Update `pipeline.run_doctor` missing-binary messages to be generic (`"opencode not found; set providers.opencode.bin or path_prepend"`).

### Tests to Add / Update
- `tests/test_config_and_schemas.py`: neutral defaults, `gpu.mcp_url=None`, `gpu.tools`, `quota` validation, `path_prepend` & `providers.*.bin` validation, unpriced models returning `0.0` + `priced=False`, custom `model_pricing` & `tier`.
- `tests/test_gpu_runner.py`: disabled `mcp_url=None` raises `GpuAbstain`, configurable tool names (`decide`/`classify`/`status`), generic `gpu_available`.
- `tests/test_governor_and_quota.py` & `tests/test_coder_tiers_opencode.py`: `quota_bucket_for_model` / `detect_opencode_error` with explicit neutral test cooldowns (`600`, `1200`), `retry-after` parsing, generic `"standard"` tier.
- `tests/test_fallback_chains.py` & `tests/test_doctor_coder_tiers.py` & `tests/test_worker_and_herdr.py`: `path_prepend`, `providers.*.bin`, disabled GPU doctor output, `by_tier` assertions.

---

## Commit 3: Generalize Product-Specific Strings & Sanitize Tests

### Changes
- **`factory/lib/config.py` & `factory/lib/staging_qa.py`**
  - Add `staging_qa.test_mode_secret_keys` (default `["STRIPE_SECRET_KEY"]`) to `DEFAULT_CONFIG` and validate as `list[str]`.
  - Update `staging_qa.load_staging_secrets` to check against `staging_qa.test_mode_secret_keys` instead of any product-specific secret key name.
- **`factory/agents/planner.md`**
  - Replace product-specific example path with `src/my_app/ingest.py`.
- **`factory/cli.py`**
  - Replace example paths in `load_registered_repos` and CLI help text with `~/src/my-app`, `~/src/my-api`, and `myhost`.
- **Sanitize existing tests**
  - Replace all machine names, owners, product names, MCP aliases (`gpu:decision-model`), URLs (`http://localhost:PORT/mcp` / `http://127.0.0.1:PORT/mcp`), and example paths across `tests/test_worker_and_herdr.py`, `tests/test_config_and_schemas.py`, `tests/test_fallback_chains.py`, `tests/test_doctor_coder_tiers.py`, `tests/test_governor_and_quota.py`, `tests/test_coder_tiers_opencode.py`, `tests/test_gpu_runner.py`, and `tests/test_staging_qa.py`.

---

## Commit 4: Leak Check Script & Gate Integration

### Changes
- **`scripts/check_leaks.py`** (stdlib only)
  - Resolve denylist JSON from first found: `--denylist PATH`, `$FACTORY_LEAK_DENYLIST`, `<repo>/factory/leak-denylist.local.json`, `~/.config/factory/leak-denylist.json`.
  - If none exists, print `"leak check: no denylist configured, skipped"` and exit `0`.
  - Parse `{"patterns": [...], "allow": [...], "exclude_paths": [...]}` (case-insensitive regexes).
  - Scan `git ls-files` text files (skipping binaries), honor `exclude_paths` and `allow` (ignore matches fully contained in an `allow` match), and report `path:line: matched denylist pattern #N` without echoing patterns or matched text.
  - Support `--history <rev-range>` to scan commit messages and added diff lines, reporting `<sha> matched denylist pattern #N`. Exit `1` on hits, `0` when clean.
- **`.gitignore`**
  - Add `factory/leak-denylist.local.json`.
- **`factory/checks.sh`**
  - Run `python3 scripts/check_leaks.py` after `pytest` in the full gate.
- **`tests/test_leak_check.py`**
  - Hermetic tests using a synthetic denylist (`secret-host-zz`) and temporary git repo covering hits, `allow`, `exclude_paths`, binary skipping, missing-denylist skip, `--history`, and verifying output never contains pattern/matched text.

---

## Commit 5: Neutral `factory/config.json` & Documented `factory/config.example.json`

### Changes
- **`factory/config.json`**
  - Reset to neutral single-provider `agy`-only models (`gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, `gemini-3.6-flash-low`), neutral `provider_timeouts`, `gpu: {"mcp_url": null, "min_confidence": 0.9}`, and no `model_pricing` numbers.
- **`factory/config.example.json`**
  - Add complete example covering every option with `"_comment"` keys: multi-provider fallback chains (`opencode/*-free`, `gpu:decision-model`, `gemini-*`), `provider_timeouts`, `gpu` (`http://localhost:PORT/mcp`, `tools`, `min_confidence`), `quota` (`default_cooldown_sec`, `buckets`), `model_pricing` (`0.0` placeholders + `_comment` to fill in provider prices), `path_prepend` (`["~/.opencode/bin"]`), `providers.*.bin`, `batch`, and `staging_qa`.

---

## Commit 6: Public Documentation

### Changes
- **`README.md`**
  - Rewrite for first-time visitors using generic examples only (`Machine A` / `Machine B` Mermaid diagram, `~/src/my-app`, `host-a`, `host-b`, `decision-model`, `http://localhost:PORT/mcp`).
  - Document pipeline lifecycle, `refs/factory/*` coordination, leases, fleet commands, supported backends (`agy`, `opencode`, `openrouter`, `opencode-go`, optional `gpu` decision MCP with request/response JSON contract), fallback chains & cooldowns, full configuration reference (layers/precedence, every key, `config.example.json`, `config.local.json`), repo onboarding (`factory init`, `repos.json`), worker/fleet operation, and troubleshooting (`doctor`, cooldowns, stuck jobs, `needs-human`, leak check). Keep the `v0.1.0` link.
- **`INSTALL.md`**
  - Update prerequisites, installation, configuration (`config.json`, `config.local.json`, `repos.json`, `host.json`), `factory doctor` verification, and first-issue workflow using generic placeholders only.
- **`docs/plans/2026-11-18-coder-tiers.md`**
  - Sanitize into a generic design note (remove specific hosts, ports, pricing, plan names, and personal paths; keep filename).

---

## Final Verification

- Run `PYTEST_ADDOPTS="--deselect tests/test_worker_and_herdr.py::test_fleet_update_runs_git_pull_on_all_machines" bash factory/checks.sh`.
- Run `python3 scripts/check_leaks.py` against the local denylist and verify exit code `0`.
- Verify `git grep` shows no setup-specific hosts, ports, prices, or plan words in `factory/`, `tests/`, `scripts/`, `README.md`, `INSTALL.md`, `factory/config*.json`, and `docs/plans/`.
