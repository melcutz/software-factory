"""Cross-platform host CPU/RAM admission probe and shared LLM quota cooldown helpers."""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from factory.lib.worker import HostConfig


@dataclass
class HostResources:
    free_ram_gb: float
    cpu_load_pct: float


def parse_linux_meminfo_gb(text: str) -> float:
    """Parse /proc/meminfo MemAvailable (or MemFree fallback) in GiB."""
    avail_kb = None
    free_kb = None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            m = re.search(r"(\d+)", line)
            if m:
                avail_kb = int(m.group(1))
                break
        elif line.startswith("MemFree:"):
            m = re.search(r"(\d+)", line)
            if m:
                free_kb = int(m.group(1))
    kb = avail_kb if avail_kb is not None else (free_kb or 0)
    return kb / (1024.0 * 1024.0)


def parse_macos_vm_stat_gb(text: str, page_size: int = 4096) -> float:
    """Parse macOS vm_stat free + inactive + speculative pages into GiB."""
    pages = 0
    for key in ("Pages free:", "Pages inactive:", "Pages speculative:"):
        for line in text.splitlines():
            if line.startswith(key):
                m = re.search(r"(\d+)", line)
                if m:
                    pages += int(m.group(1))
    return (pages * page_size) / (1024.0 * 1024.0 * 1024.0)


def probe_host_resources() -> HostResources:
    """Probe free RAM (GiB) and 1-minute CPU load percentage on Linux/WSL2 or macOS."""
    cpu_count = max(1, os.cpu_count() or 1)
    try:
        load1 = os.getloadavg()[0]
        cpu_load_pct = (load1 / cpu_count) * 100.0
    except (OSError, AttributeError):
        cpu_load_pct = 0.0

    free_ram_gb = 999.0
    if sys.platform == "darwin":
        try:
            ps_res = subprocess.run(["sysctl", "-n", "hw.pagesize"], capture_output=True, text=True, timeout=2.0, check=False)
            page_size = int((ps_res.stdout or "4096").strip()) if ps_res.returncode == 0 else 4096
            vm_res = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2.0, check=False)
            if vm_res.returncode == 0:
                free_ram_gb = parse_macos_vm_stat_gb(vm_res.stdout, page_size=page_size)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    elif os.path.isfile("/proc/meminfo"):
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                free_ram_gb = parse_linux_meminfo_gb(f.read())
        except OSError:
            pass

    return HostResources(free_ram_gb=round(free_ram_gb, 2), cpu_load_pct=round(cpu_load_pct, 1))


def check_host_admission(
    host_cfg: HostConfig,
    *,
    resources: HostResources | None = None,
) -> tuple[bool, str]:
    """Return (admitted, reason) based on host free RAM and CPU load thresholds."""
    res = resources or probe_host_resources()
    if res.free_ram_gb < host_cfg.min_free_ram_gb:
        return False, f"RAM low ({res.free_ram_gb:.1f}GB < {host_cfg.min_free_ram_gb:.1f}GB)"
    if res.cpu_load_pct >= host_cfg.max_cpu_load_pct:
        return False, f"CPU busy ({res.cpu_load_pct:.0f}% >= {host_cfg.max_cpu_load_pct:.0f}%)"
    return True, ""


def normalize_base_model(model_spec: str) -> str:
    """Normalize a model spec (e.g. 'gemini-4-argon-high' or 'gemini-4-argon:medium') to its base model name."""
    from factory.lib.config import split_model_and_effort

    spec = (model_spec or "").strip()
    if not spec:
        return ""
    base, _ = split_model_and_effort(spec)
    return base.split(":", 1)[0].split()[0] if base else ""


def _resolve_quota_policy(config: dict | None) -> tuple[int, list[dict]]:
    from factory.lib.config import DEFAULT_QUOTA_BUCKETS

    quota_cfg = (config.get("quota") if isinstance(config, dict) and isinstance(config.get("quota"), dict) else None) or {}
    raw_def = quota_cfg.get("default_cooldown_sec")
    default_cd = int(raw_def) if isinstance(raw_def, int) and not isinstance(raw_def, bool) and raw_def >= 1 else 3600
    raw_buckets = quota_cfg.get("buckets")
    buckets = raw_buckets if isinstance(raw_buckets, list) else DEFAULT_QUOTA_BUCKETS
    return default_cd, buckets


def quota_bucket_for_model(model_spec: str, config: dict | None = None) -> str:
    """Normalize a model spec to its shared quota cooldown bucket using config['quota']['buckets']."""
    spec = (model_spec or "").strip()
    if not spec:
        return ""
    _, buckets = _resolve_quota_policy(config)
    for rule in buckets:
        if isinstance(rule, dict) and str(rule.get("bucket") or "").strip() == spec:
            return spec
    for rule in buckets:
        if not isinstance(rule, dict):
            continue
        pat = str(rule.get("match") or "").strip()
        bucket = str(rule.get("bucket") or "").strip()
        if pat and bucket and fnmatch.fnmatchcase(spec, pat):
            return bucket
    return normalize_base_model(spec)


def quota_cooldown_for_model(
    model_spec: str,
    config: dict | None = None,
    retry_after_sec: int | None = None,
) -> tuple[str, int]:
    """Return ``(bucket, cooldown_sec)`` for ``model_spec`` from ``config['quota']``."""
    spec = (model_spec or "").strip()
    default_cd, buckets = _resolve_quota_policy(config)
    matched_rule: dict | None = None
    for rule in buckets:
        if isinstance(rule, dict) and str(rule.get("bucket") or "").strip() == spec:
            matched_rule = rule
            break
    if matched_rule is None:
        for rule in buckets:
            if not isinstance(rule, dict):
                continue
            pat = str(rule.get("match") or "").strip()
            bucket = str(rule.get("bucket") or "").strip()
            if pat and bucket and fnmatch.fnmatchcase(spec, pat):
                matched_rule = rule
                break

    bucket_name = (
        str(matched_rule.get("bucket") or "").strip()
        if isinstance(matched_rule, dict)
        else ""
    ) or normalize_base_model(spec)

    rule_cd = matched_rule.get("cooldown_sec") if isinstance(matched_rule, dict) else None
    if isinstance(rule_cd, int) and not isinstance(rule_cd, bool) and rule_cd >= 1:
        return bucket_name, int(rule_cd)
    if retry_after_sec is not None and isinstance(retry_after_sec, (int, float)) and not isinstance(retry_after_sec, bool) and int(retry_after_sec) >= 1:
        return bucket_name, int(retry_after_sec)
    return bucket_name, default_cd


def is_model_in_cooldown(
    quota_state: dict,
    base_model: str,
    *,
    now_ts: float | None = None,
    config: dict | None = None,
) -> tuple[bool, float]:
    """Return (in_cooldown, remaining_sec) for base_model from refs/factory/quota."""
    ts = time.time() if now_ts is None else now_ts
    models = quota_state.get("models") or {}
    bucket = quota_bucket_for_model(base_model, config=config)
    norm = normalize_base_model(base_model)
    entry = (
        models.get(base_model)
        or (models.get(bucket) if bucket else None)
        or (models.get(norm) if norm else None)
    )
    if not isinstance(entry, dict):
        return False, 0.0
    until = float(entry.get("cooldown_until_ts") or 0.0)
    if until > ts:
        return True, until - ts
    return False, 0.0
