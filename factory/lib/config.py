"""Configuration loading, validation, and reviewer model routing."""
from __future__ import annotations

import copy
import json
import os
from urllib.parse import urlparse

from factory.lib.agy_runner import classify_model_provider

VALID_MERGE_METHODS = {"merge", "squash", "rebase"}
VALID_AUTO_MERGE = {"low", "all", "none"}
VALID_RISKS = {"low", "medium", "high"}
VALID_SURFACES = {"ui", "security"}
EFFORT_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "max", "none"}
KNOWN_MODEL_PREFIXES = ("gemini-", "opencode/", "opencode-go/", "openrouter/", "gpu:")
GPU_ALLOWED_ROLES = {"intake", "approver", "planner"}
MODEL_ROLES = (
    "planner",
    "intake",
    "spec_writer",
    "builder",
    "batch_builder",
    "builder_low",
    "approver",
    "qa",
    "code_low",
    "code_medium",
    "code_high",
    "security",
    "ux",
    "ui",
)

DEFAULT_PROVIDER_TIMEOUTS: dict[str, int | None] = {
    "agy": None,
    "opencode": 1800,
    "opencode-go": 1800,
    "openrouter": 1200,
    "gpu": 60,
}

DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {}

DEFAULT_QUOTA_BUCKETS: list[dict] = [
    {"match": "gpu:*", "bucket": "gpu", "cooldown_sec": None},
    {"match": "opencode-go/*", "bucket": "opencode-go", "cooldown_sec": None},
    {"match": "openrouter/*:free", "bucket": "openrouter-free", "cooldown_sec": None},
    {"match": "opencode/*-free", "bucket": "opencode-free", "cooldown_sec": None},
    {"match": "opencode/big-pickle", "bucket": "opencode-free", "cooldown_sec": None},
]

DEFAULT_CONFIG: dict = {
    "concurrency": 4,
    "max_rounds": 3,
    "max_slice_retries": 2,
    "agent_timeout_sec": 900,
    "gate_timeout_sec": 600,
    "idle_bg_grace_sec": 600,
    "auto_polish": True,
    "auto_approve_clean": True,
    "allow_self_host": False,
    "base_branch": "main",
    "merge_method": "merge",
    "auto_merge": "low",
    "labels": {
        "queue": "factory",
        "needs_info": "factory:needs-info",
        "needs_human": "factory:needs-human",
    },
    "protected_paths": [
        "factory/",
        ".agents/",
        ".github/",
    ],
    "models": {
        "planner": "gemini-3.8-flash-low",
        "intake": "gemini-3.8-flash-low",
        "spec_writer": "gemini-3.8-flash-medium",
        "builder": "gemini-3.8-flash-medium",
        "batch_builder": "gemini-3.8-flash-medium",
        "builder_low": None,
        "approver": "gemini-3.8-flash-medium",
        "qa": "gemini-3.8-flash-medium",
        "code_low": "gemini-3.6-flash-low",
        "code_medium": "gemini-3.8-flash-low",
        "code_high": "gemini-3.8-flash-low",
        "security": "gemini-3.8-flash-medium",
        "ux": "gemini-3.8-flash-medium",
        "ui": "gemini-3.6-flash-low",
    },
    "provider_timeouts": copy.deepcopy(DEFAULT_PROVIDER_TIMEOUTS),
    "gpu": {
        "mcp_url": None,
        "min_confidence": 0.9,
        "tools": {
            "decide": "decide",
            "classify": "classify",
            "status": None,
        },
    },
    "model_pricing": copy.deepcopy(DEFAULT_MODEL_PRICING),
    "quota": {
        "default_cooldown_sec": 3600,
        "buckets": copy.deepcopy(DEFAULT_QUOTA_BUCKETS),
    },
    "path_prepend": [],
    "providers": {
        "agy": {"bin": None},
        "opencode": {"bin": None},
    },
    "max_job_cost_usd": 0.0,
    "batch": {
        "enabled": True,
        "max_batch_issues": 5,
        "max_batch_weight": 6,
        "max_parallel_batches": 2,
        "refresh_repo_map": True,
    },
    "staging_qa": {
        "enabled": False,
        "max_batch_size": 4,
        "coalesce_window_sec": 15,
        "max_qa_rounds": 3,
        "deploy_timeout_sec": 900,
        "qa_timeout_sec": 1200,
        "base_url": "",
        "ops_base_url": "",
        "forbidden_prod_hosts": [],
        "env_files": [],
        "test_mode_secret_keys": ["STRIPE_SECRET_KEY"],
    },
}


def model_chain(config: dict, role: str) -> list[str]:
    """Return the normalized fallback chain for ``models.<role>`` as a list of model specs."""
    models = (config or {}).get("models") if isinstance(config, dict) else None
    if not isinstance(models, dict):
        return []
    val = models.get(role)
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, list):
        return [str(s).strip() for s in val if isinstance(s, str) and s.strip()]
    return []


def provider_timeout_sec(config: dict | None, model_spec: str, default_timeout_sec: int) -> int:
    """Resolve per-provider timeout from ``config['provider_timeouts']``, falling back to ``default_timeout_sec``."""
    provider, _ = classify_model_provider(model_spec)
    pt = (config or {}).get("provider_timeouts") if isinstance(config, dict) else None
    if isinstance(pt, dict) and provider in pt and pt[provider] is not None:
        val = pt[provider]
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    return int(default_timeout_sec)


def split_model_and_effort(model_spec: str) -> tuple[str, str]:
    """Split a model identifier like 'gemini-4-argon-high' into ('gemini-4-argon', 'high')."""
    spec = (model_spec or "").strip()
    if not spec:
        return "unknown", "default"
    if "-" in spec:
        base, suffix = spec.rsplit("-", 1)
        if base and suffix.lower() in EFFORT_LEVELS:
            return base, suffix.lower()
    return spec, "default"


def expand_path_prepend(config: dict | None) -> list[str]:
    """Return expanded directory paths from ``config['path_prepend']``."""
    raw = (config or {}).get("path_prepend") if isinstance(config, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            exp = os.path.expanduser(item.strip())
            if exp not in out:
                out.append(exp)
    return out


def _lookup_pricing_entry(effective_spec: str, pricing_map: dict | None) -> dict | None:
    if not effective_spec or not isinstance(pricing_map, dict):
        return None
    base_model, _ = split_model_and_effort(effective_spec)
    rates = pricing_map.get(effective_spec) or pricing_map.get(base_model)
    if isinstance(rates, dict):
        return rates
    for prefix_key, entry in pricing_map.items():
        if (
            isinstance(prefix_key, str)
            and prefix_key
            and (effective_spec.startswith(prefix_key) or base_model.startswith(prefix_key))
            and isinstance(entry, dict)
        ):
            return entry
    return None


def resolve_model_tier(model_spec: str, config: dict | None = None) -> str:
    """Resolve model tier from ``model_pricing.<model>.tier`` if configured, else generic ('local', 'free', 'standard')."""
    effective_spec = (model_spec or "").strip()
    pricing_map = (config or {}).get("model_pricing") if isinstance(config, dict) else None
    rates = _lookup_pricing_entry(effective_spec, pricing_map)
    if isinstance(rates, dict):
        tier_val = rates.get("tier")
        if isinstance(tier_val, str) and tier_val.strip():
            return tier_val.strip()
    _, tier = classify_model_provider(effective_spec)
    return tier


def estimate_usage_cost_usd(
    model_spec: str,
    usage: dict | None,
    config: dict | None = None,
) -> float | None:
    """Return direct or estimated USD cost for a single agent step's token usage (0.0 when unpriced)."""
    if not isinstance(usage, dict):
        return None
    for direct_key in ("cost_usd", "cost"):
        val = usage.get(direct_key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            usage["priced"] = True
            return round(float(val), 6)

    effective_spec = (model_spec or str(usage.get("model") or "")).strip()
    pricing_map = ((config or {}).get("model_pricing") if isinstance(config, dict) else None) or DEFAULT_MODEL_PRICING
    rates = _lookup_pricing_entry(effective_spec, pricing_map)
    if isinstance(rates, dict):
        tier_val = rates.get("tier")
        if isinstance(tier_val, str) and tier_val.strip():
            usage["tier"] = tier_val.strip()

    provider, tier = classify_model_provider(effective_spec)
    if tier in {"free", "local"} or provider == "gpu":
        usage["priced"] = bool(isinstance(rates, dict))
        if not isinstance(rates, dict):
            return 0.0

    if not isinstance(rates, dict):
        usage["priced"] = False
        return 0.0

    usage["priced"] = True
    in_tok = int(usage.get("input_tokens", 0) or 0)
    cache_tok = int(usage.get("cache_read_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    if in_tok == 0 and cache_tok == 0 and out_tok == 0:
        tot = int(usage.get("total_tokens", 0) or 0)
        if tot == 0:
            return 0.0
        in_tok = tot

    in_rate = float(rates.get("input_per_m", 0.0))
    cache_rate = float(rates.get("cached_per_m", in_rate * 0.25))
    out_rate = float(rates.get("output_per_m", 0.0))
    cost = (in_tok * in_rate + cache_tok * cache_rate + out_tok * out_rate) / 1_000_000.0
    return round(cost, 6)


def _validate_non_prod_url(url: str, field_name: str, forbidden_hosts: set[str]) -> None:
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{field_name} must be a non-empty URL string")
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError(f"{field_name} must be an https URL, got {url!r}")
    if host in forbidden_hosts:
        raise ValueError(f"Refusing production URL in {field_name}: {url!r}")


def _validate_model_spec(role: str, spec: str) -> None:
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"models.{role} must contain non-empty model strings, got {spec!r}")
    clean = spec.strip()
    if not clean.startswith(KNOWN_MODEL_PREFIXES):
        raise ValueError(
            f"models.{role} has unknown model prefix in {clean!r} (expected one of {KNOWN_MODEL_PREFIXES})"
        )
    if clean.startswith("gpu:") and role not in GPU_ALLOWED_ROLES:
        raise ValueError(
            f"models.{role} cannot use 'gpu:' model {clean!r}; gpu: is only allowed in {sorted(GPU_ALLOWED_ROLES)}"
        )


def _user_config_path() -> str:
    """Return path to optional user/host-level config file."""
    env_path = (os.environ.get("FACTORY_USER_CONFIG") or "").strip()
    if env_path:
        return os.path.expanduser(env_path)
    return os.path.expanduser("~/.config/factory/config.json")


def _strip_comment_keys(obj):
    """Recursively remove dict keys starting with '_' (e.g. '_comment')."""
    if isinstance(obj, dict):
        return {
            k: _strip_comment_keys(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(obj, list):
        return [_strip_comment_keys(item) for item in obj]
    return obj


def _deep_merge_config(base: dict, overlay: dict) -> dict:
    """Recursively deep-merge dict sections; scalars and lists replace."""
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        clean_val = _strip_comment_keys(value)
        if isinstance(merged.get(key), dict) and isinstance(clean_val, dict):
            merged[key] = _deep_merge_config(merged[key], clean_val)
        else:
            merged[key] = copy.deepcopy(clean_val)
    return merged


def load_config_with_layers(factory_dir: str) -> tuple[dict, list[str]]:
    """Load and validate layered configuration, returning (merged_config, loaded_layer_paths).

    Precedence (lowest to highest):
      1. DEFAULT_CONFIG
      2. User/host file ($FACTORY_USER_CONFIG or ~/.config/factory/config.json)
      3. <repo>/factory/config.json
      4. <repo>/factory/config.local.json
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    loaded_layers: list[str] = []
    explicit_batch_builder = False

    candidate_paths: list[str] = []
    user_path = _user_config_path()
    if user_path:
        candidate_paths.append(user_path)
    if factory_dir:
        candidate_paths.append(os.path.join(factory_dir, "config.json"))
        candidate_paths.append(os.path.join(factory_dir, "config.local.json"))

    seen_real: set[str] = set()
    for layer_path in candidate_paths:
        if not layer_path or not os.path.exists(layer_path):
            continue
        real_p = os.path.realpath(layer_path)
        if real_p in seen_real:
            continue
        seen_real.add(real_p)
        with open(layer_path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError(f"{os.path.basename(layer_path)} root must be a JSON object")
        clean_raw = _strip_comment_keys(raw)
        if isinstance(clean_raw.get("models"), dict) and "batch_builder" in clean_raw["models"]:
            explicit_batch_builder = True
        cfg = _deep_merge_config(cfg, clean_raw)
        loaded_layers.append(layer_path)

    if not explicit_batch_builder:
        cfg["models"]["batch_builder"] = copy.deepcopy(cfg["models"]["builder"])
    else:
        cfg["models"].setdefault("batch_builder", copy.deepcopy(cfg["models"]["builder"]))

    if cfg["merge_method"] not in VALID_MERGE_METHODS:
        raise ValueError(f"Invalid merge_method: {cfg['merge_method']!r}")
    if cfg["auto_merge"] not in VALID_AUTO_MERGE:
        raise ValueError(f"Invalid auto_merge: {cfg['auto_merge']!r}")

    models_cfg = cfg.get("models")
    if not isinstance(models_cfg, dict):
        raise ValueError("models must be a JSON object")
    for role, val in models_cfg.items():
        if val is None:
            if role != "builder_low":
                raise ValueError(f"models.{role} cannot be null")
            continue
        if isinstance(val, str):
            _validate_model_spec(role, val)
        elif isinstance(val, list):
            if not val:
                raise ValueError(f"models.{role} chain must be a non-empty list")
            for item in val:
                _validate_model_spec(role, item)
        else:
            raise ValueError(f"models.{role} must be a string or non-empty list of strings, got {val!r}")

    pt_cfg = cfg.get("provider_timeouts")
    if not isinstance(pt_cfg, dict):
        raise ValueError("provider_timeouts must be a JSON object")
    for prov, tval in pt_cfg.items():
        if tval is not None and (not isinstance(tval, int) or isinstance(tval, bool) or tval < 1):
            raise ValueError(f"provider_timeouts.{prov} must be null or a positive integer, got {tval!r}")

    gpu_cfg = cfg.get("gpu")
    if not isinstance(gpu_cfg, dict):
        raise ValueError("gpu must be a JSON object")
    mcp_url = gpu_cfg.get("mcp_url")
    if mcp_url is not None:
        if not isinstance(mcp_url, str) or not mcp_url.strip():
            raise ValueError("gpu.mcp_url must be null or a non-empty http(s) URL")
        parsed_mcp = urlparse(mcp_url.strip())
        if parsed_mcp.scheme not in {"http", "https"} or not parsed_mcp.hostname:
            raise ValueError(f"gpu.mcp_url must be an http(s) URL, got {mcp_url!r}")
    min_conf = gpu_cfg.get("min_confidence", 0.9)
    if not isinstance(min_conf, (int, float)) or isinstance(min_conf, bool) or not (0.0 < float(min_conf) <= 1.0):
        raise ValueError(f"gpu.min_confidence must be a number in (0.0, 1.0], got {min_conf!r}")
    tools_cfg = gpu_cfg.get("tools")
    if tools_cfg is not None:
        if not isinstance(tools_cfg, dict):
            raise ValueError("gpu.tools must be a JSON object")
        for tkey in ("decide", "classify"):
            tval = tools_cfg.get(tkey)
            if tval is not None and (not isinstance(tval, str) or not tval.strip()):
                raise ValueError(f"gpu.tools.{tkey} must be a non-empty string, got {tval!r}")
        sval = tools_cfg.get("status")
        if sval is not None and (not isinstance(sval, str) or not sval.strip()):
            raise ValueError(f"gpu.tools.status must be null or a non-empty string, got {sval!r}")

    quota_cfg = cfg.get("quota")
    if not isinstance(quota_cfg, dict):
        raise ValueError("quota must be a JSON object")
    def_cd = quota_cfg.get("default_cooldown_sec")
    if not isinstance(def_cd, int) or isinstance(def_cd, bool) or def_cd < 1:
        raise ValueError(f"quota.default_cooldown_sec must be a positive integer, got {def_cd!r}")
    buckets_cfg = quota_cfg.get("buckets")
    if not isinstance(buckets_cfg, list):
        raise ValueError("quota.buckets must be a list of bucket rules")
    for idx, rule in enumerate(buckets_cfg):
        if not isinstance(rule, dict):
            raise ValueError(f"quota.buckets[{idx}] must be an object")
        m_pat = rule.get("match")
        b_name = rule.get("bucket")
        c_sec = rule.get("cooldown_sec")
        if not isinstance(m_pat, str) or not m_pat.strip():
            raise ValueError(f"quota.buckets[{idx}].match must be a non-empty string")
        if not isinstance(b_name, str) or not b_name.strip():
            raise ValueError(f"quota.buckets[{idx}].bucket must be a non-empty string")
        if c_sec is not None and (not isinstance(c_sec, int) or isinstance(c_sec, bool) or c_sec < 1):
            raise ValueError(f"quota.buckets[{idx}].cooldown_sec must be null or a positive integer, got {c_sec!r}")

    pp_cfg = cfg.get("path_prepend")
    if not isinstance(pp_cfg, list) or any(not isinstance(p, str) or not p.strip() for p in pp_cfg):
        raise ValueError("path_prepend must be a list of non-empty directory strings")

    prov_cfg = cfg.get("providers")
    if not isinstance(prov_cfg, dict):
        raise ValueError("providers must be a JSON object")
    for pname, pval in prov_cfg.items():
        if not isinstance(pval, dict):
            raise ValueError(f"providers.{pname} must be a JSON object")
        bval = pval.get("bin")
        if bval is not None and (not isinstance(bval, str) or not bval.strip()):
            raise ValueError(f"providers.{pname}.bin must be null or a non-empty string, got {bval!r}")

    mp_cfg = cfg.get("model_pricing")
    if not isinstance(mp_cfg, dict):
        raise ValueError("model_pricing must be a JSON object")
    for mkey, mval in mp_cfg.items():
        if not isinstance(mval, dict):
            raise ValueError(f"model_pricing.{mkey} must be a JSON object")
        for rkey in ("input_per_m", "cached_per_m", "output_per_m"):
            if rkey in mval:
                rval = mval[rkey]
                if not isinstance(rval, (int, float)) or isinstance(rval, bool) or rval < 0:
                    raise ValueError(f"model_pricing.{mkey}.{rkey} must be a non-negative number, got {rval!r}")
        if "tier" in mval and mval["tier"] is not None:
            if not isinstance(mval["tier"], str) or not mval["tier"].strip():
                raise ValueError(f"model_pricing.{mkey}.tier must be a non-empty string")

    for int_key in ("concurrency", "max_rounds", "max_slice_retries", "agent_timeout_sec", "gate_timeout_sec"):
        val = cfg.get(int_key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"{int_key} must be a positive integer, got {val!r}")

    idle_grace = cfg.get("idle_bg_grace_sec")
    if not isinstance(idle_grace, int) or isinstance(idle_grace, bool) or idle_grace < 5:
        raise ValueError(f"idle_bg_grace_sec must be an integer >= 5, got {idle_grace!r}")

    for bool_key in ("auto_polish", "allow_self_host"):
        bval = cfg.get(bool_key)
        if not isinstance(bval, bool):
            raise ValueError(f"{bool_key} must be a boolean, got {bval!r}")

    aac = cfg.get("auto_approve_clean")
    if not isinstance(aac, bool) and aac != "all":
        raise ValueError(f"auto_approve_clean must be a boolean or 'all', got {aac!r}")

    max_cost = cfg.get("max_job_cost_usd", 0.0)
    if not isinstance(max_cost, (int, float)) or isinstance(max_cost, bool) or max_cost < 0:
        raise ValueError(f"max_job_cost_usd must be a non-negative number, got {max_cost!r}")

    if not isinstance(cfg.get("base_branch"), str) or not cfg["base_branch"].strip():
        raise ValueError("base_branch must be a non-empty string")
    if not isinstance(cfg.get("protected_paths"), list):
        raise ValueError("protected_paths must be a list of path patterns")

    batch_cfg = cfg.get("batch")
    if not isinstance(batch_cfg, dict):
        raise ValueError("batch must be a JSON object")
    for bkey in ("enabled", "refresh_repo_map"):
        if not isinstance(batch_cfg.get(bkey), bool):
            raise ValueError(f"batch.{bkey} must be a boolean")
    for ikey, hi in (("max_batch_issues", 10), ("max_batch_weight", 20), ("max_parallel_batches", 8)):
        ival = batch_cfg.get(ikey)
        if not isinstance(ival, int) or isinstance(ival, bool) or not (1 <= ival <= hi):
            raise ValueError(f"batch.{ikey} must be an integer in 1..{hi}, got {ival!r}")

    sqa = cfg.get("staging_qa")
    if not isinstance(sqa, dict):
        raise ValueError("staging_qa must be a JSON object")
    if not isinstance(sqa.get("enabled"), bool):
        raise ValueError("staging_qa.enabled must be a boolean")
    for int_key in ("max_batch_size", "max_qa_rounds", "deploy_timeout_sec", "qa_timeout_sec"):
        val = sqa.get(int_key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"staging_qa.{int_key} must be a positive integer, got {val!r}")
    coalesce_sec = sqa.get("coalesce_window_sec")
    if not isinstance(coalesce_sec, int) or isinstance(coalesce_sec, bool) or coalesce_sec < 0:
        raise ValueError(f"staging_qa.coalesce_window_sec must be an integer >= 0, got {coalesce_sec!r}")
    forbidden_hosts = {
        str(h).strip().lower()
        for h in (sqa.get("forbidden_prod_hosts") or [])
        if str(h).strip()
    }
    if sqa["enabled"]:
        _validate_non_prod_url(sqa.get("base_url", ""), "staging_qa.base_url", forbidden_hosts)
        if sqa.get("ops_base_url"):
            _validate_non_prod_url(
                sqa.get("ops_base_url", ""), "staging_qa.ops_base_url", forbidden_hosts
            )
    if not isinstance(sqa.get("env_files"), list) or any(not isinstance(p, str) for p in sqa["env_files"]):
        raise ValueError("staging_qa.env_files must be a list of file paths")
    test_mode_keys = sqa.get("test_mode_secret_keys")
    if not isinstance(test_mode_keys, list) or any(
        not isinstance(k, str) or not k.strip() for k in test_mode_keys
    ):
        raise ValueError("staging_qa.test_mode_secret_keys must be a list of non-empty strings")

    return cfg, loaded_layers


def load_config(factory_dir: str) -> dict:
    """Load and validate layered configuration merged with DEFAULT_CONFIG."""
    cfg, _ = load_config_with_layers(factory_dir)
    return cfg


def resolve_reviewers(config: dict, risk: str, surfaces: list[str]) -> dict[str, str | list[str]]:
    """Deterministically resolve reviewer roles to model IDs (or chains) for a given risk and surface set."""
    norm_risk = (risk or "low").strip().lower()
    if norm_risk not in VALID_RISKS:
        norm_risk = "low"

    models = config["models"]
    reviewers: dict[str, str | list[str]] = {
        "code": models[f"code_{norm_risk}"],
    }

    norm_surfaces = {s.strip().lower() for s in (surfaces or []) if isinstance(s, str)}
    if "both" in norm_surfaces:
        norm_surfaces = {"ui", "security"}

    if "security" in norm_surfaces:
        reviewers["security"] = models["security"]
    if "ui" in norm_surfaces:
        reviewers["ux"] = models["ux"]
        reviewers["ui"] = models["ui"]

    return reviewers
