"""Configuration loading, validation, and reviewer model routing."""
from __future__ import annotations

import copy
import json
import os

VALID_MERGE_METHODS = {"merge", "squash", "rebase"}
VALID_AUTO_MERGE = {"low", "all", "none"}
VALID_RISKS = {"low", "medium", "high"}
VALID_SURFACES = {"ui", "security"}

DEFAULT_CONFIG: dict = {
    "concurrency": 3,
    "max_rounds": 3,
    "max_slice_retries": 2,
    "agent_timeout_sec": 900,
    "gate_timeout_sec": 600,
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
        "intake": "gemini-3.8-flash-medium",
        "spec_writer": "gemini-4-argon-high",
        "builder": "gemini-4-argon-high",
        "approver": "gemini-4-argon-high",
        "code_low": "gemini-3.6-flash-low",
        "code_medium": "gemini-3.8-flash-low",
        "code_high": "gemini-3.8-flash-medium",
        "security": "gemini-4-argon-high",
        "ux": "gemini-3.8-flash-medium",
        "ui": "gemini-3.6-flash-low",
    },
}


def load_config(factory_dir: str) -> dict:
    """Load and validate factory/config.json merged with DEFAULT_CONFIG."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = os.path.join(factory_dir, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config.json root must be a JSON object")
        for key, value in raw.items():
            if key in {"labels", "models"} and isinstance(value, dict):
                cfg[key].update(value)
            else:
                cfg[key] = value

    if cfg["merge_method"] not in VALID_MERGE_METHODS:
        raise ValueError(f"Invalid merge_method: {cfg['merge_method']!r}")
    if cfg["auto_merge"] not in VALID_AUTO_MERGE:
        raise ValueError(f"Invalid auto_merge: {cfg['auto_merge']!r}")

    for int_key in ("concurrency", "max_rounds", "max_slice_retries", "agent_timeout_sec", "gate_timeout_sec"):
        val = cfg.get(int_key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"{int_key} must be a positive integer, got {val!r}")

    if not isinstance(cfg.get("base_branch"), str) or not cfg["base_branch"].strip():
        raise ValueError("base_branch must be a non-empty string")
    if not isinstance(cfg.get("protected_paths"), list):
        raise ValueError("protected_paths must be a list of path patterns")

    return cfg


def resolve_reviewers(config: dict, risk: str, surfaces: list[str]) -> dict[str, str]:
    """Deterministically resolve reviewer roles to model IDs for a given risk and surface set."""
    norm_risk = (risk or "low").strip().lower()
    if norm_risk not in VALID_RISKS:
        norm_risk = "low"

    models = config["models"]
    reviewers: dict[str, str] = {
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
