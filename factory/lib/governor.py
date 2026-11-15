"""Cross-platform host CPU/RAM admission probe and shared LLM quota cooldown helpers."""
from __future__ import annotations

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


def is_model_in_cooldown(
    quota_state: dict,
    base_model: str,
    *,
    now_ts: float | None = None,
) -> tuple[bool, float]:
    """Return (in_cooldown, remaining_sec) for base_model from refs/factory/quota."""
    ts = time.time() if now_ts is None else now_ts
    entry = (quota_state.get("models") or {}).get(base_model)
    if not isinstance(entry, dict):
        return False, 0.0
    until = float(entry.get("cooldown_until_ts") or 0.0)
    if until > ts:
        return True, until - ts
    return False, 0.0
