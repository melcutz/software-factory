"""Fleet discovery and remote orchestration via herdr-core (hc) machines."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from factory.lib.worker import host_config_dir, is_host_paused, set_host_paused


def _hc_bin() -> str:
    return (os.environ.get("HC_BIN_PATH") or shutil.which("hc") or "").strip()


def discover_machines() -> list[dict]:
    """Discover peer machines via `hc machine ls` plus optional fleet.json fallback."""
    machines: list[dict] = []
    seen: set[str] = set()
    hc = _hc_bin()
    if hc:
        try:
            res = subprocess.run([hc, "machine", "ls"], capture_output=True, text=True, timeout=5.0, check=False)
            if res.returncode == 0:
                for raw in res.stdout.splitlines():
                    line = raw.strip()
                    if not line or line.lower().startswith("name ") or line.startswith("-"):
                        continue
                    name = line.split()[0]
                    if name and name not in seen:
                        seen.add(name)
                        machines.append({"name": name, "via": "hc", "raw": line})
        except (OSError, subprocess.TimeoutExpired):
            pass

    fleet_path = os.path.join(host_config_dir(), "fleet.json")
    if os.path.isfile(fleet_path):
        try:
            with open(fleet_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("machines", []) if isinstance(data, dict) else data
            for item in entries:
                if isinstance(item, str) and item not in seen:
                    seen.add(item)
                    machines.append({"name": item, "via": "ssh", "target": item})
                elif isinstance(item, dict) and item.get("name") and item["name"] not in seen:
                    seen.add(item["name"])
                    machines.append({"name": item["name"], "via": "ssh", "target": item.get("target") or item["name"]})
        except (OSError, ValueError):
            pass

    return machines


def _run_on_machine(machine: dict, cmd: list[str], *, bg: bool = False, name: str = "") -> tuple[int, str]:
    hc = _hc_bin()
    if machine.get("via") == "hc" and hc:
        full = [hc, "run", "--on", machine["name"]]
        if bg:
            full.append("--bg")
        if name:
            full.extend(["--name", name])
        full.append("--")
        full.extend(cmd)
        res = subprocess.run(full, capture_output=True, text=True, timeout=15.0, check=False)
        return res.returncode, (res.stdout or res.stderr or "").strip()

    target = machine.get("target") or machine["name"]
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, " ".join(cmd)],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )
    return res.returncode, (res.stdout or res.stderr or "").strip()


def fleet_status() -> list[dict]:
    machines = discover_machines()
    if not machines:
        return [{"name": "local", "via": "local", "paused": is_host_paused()}]
    return machines


def fleet_wake() -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    for m in discover_machines():
        rc, out = _run_on_machine(
            m,
            ["factory", "worker", "--all"],
            bg=True,
            name=f"factory-worker-{m['name']}",
        )
        results.append((m["name"], rc, out))
    return results


def fleet_pause(machine: str | None = None, *, drain: bool = False) -> list[tuple[str, int, str]]:
    if not machine:
        set_host_paused(True, drain=drain)
        return [("local", 0, "paused")]
    cmd = ["factory", "pause"] + (["--drain"] if drain else [])
    results: list[tuple[str, int, str]] = []
    for m in discover_machines():
        if m["name"] == machine:
            rc, out = _run_on_machine(m, cmd)
            results.append((m["name"], rc, out))
    return results


def fleet_unpause(machine: str | None = None) -> list[tuple[str, int, str]]:
    if not machine:
        set_host_paused(False)
        return [("local", 0, "unpaused")]
    results: list[tuple[str, int, str]] = []
    for m in discover_machines():
        if m["name"] == machine:
            rc, out = _run_on_machine(m, ["factory", "unpause"])
            results.append((m["name"], rc, out))
    return results
