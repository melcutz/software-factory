"""Fleet discovery and remote orchestration via herdr-core (hc) machines."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess

from factory.lib.herdr_bridge import sanitize_hc_name
from factory.lib.worker import host_config_dir, is_host_paused, set_host_paused


def _hc_bin() -> str:
    return (os.environ.get("HC_BIN_PATH") or shutil.which("hc") or "").strip()


def discover_machines(*, include_local: bool = True) -> list[dict]:
    """Discover local + peer machines via `hc machine ls` plus optional fleet.json fallback."""
    machines: list[dict] = []
    seen: set[str] = set()
    local_name = socket.gethostname().split(".")[0]
    if include_local and local_name:
        seen.add(local_name)
        machines.append(
            {
                "name": local_name,
                "via": "local",
                "host": local_name,
                "state": "paused" if is_host_paused() else "local",
                "version": "",
                "target": local_name,
            }
        )
    hc = _hc_bin()
    if hc:
        try:
            res = subprocess.run([hc, "machine", "ls"], capture_output=True, text=True, timeout=5.0, check=False)
            if res.returncode == 0:
                stdout = (res.stdout or "").strip()
                parsed_json = None
                if stdout.startswith("{") or stdout.startswith("["):
                    try:
                        parsed_json = json.loads(stdout)
                    except ValueError:
                        parsed_json = None

                if isinstance(parsed_json, dict) and isinstance(parsed_json.get("machines"), list):
                    for item in parsed_json["machines"]:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name") or item.get("target") or "").strip()
                        if name and name not in seen:
                            seen.add(name)
                            machines.append(
                                {
                                    "name": name,
                                    "via": "hc",
                                    "host": str(item.get("host") or ""),
                                    "state": str(item.get("state") or ""),
                                    "version": str(item.get("version") or ""),
                                    "target": str(item.get("target") or name),
                                }
                            )
                else:
                    for raw in stdout.splitlines():
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
    if machine.get("via") == "local":
        if bg and hc:
            full = [hc, "run", "--bg"]
            if name:
                full.extend(["--name", name])
            full.append("--")
            full.extend(cmd)
            res = subprocess.run(full, capture_output=True, text=True, timeout=15.0, check=False)
            return res.returncode, (res.stdout or res.stderr or "").strip()
        if bg:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return 0, "started (local background)"
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0, check=False)
        return res.returncode, (res.stdout or res.stderr or "").strip()

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


def _factory_shell_cmd(*args: str, config: dict | None = None) -> list[str]:
    from factory.lib.config import _user_config_path, expand_path_prepend

    quoted = " ".join(args)
    cfg = config
    if cfg is None:
        user_cfg_path = _user_config_path()
        if os.path.isfile(user_cfg_path):
            try:
                with open(user_cfg_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg = loaded
            except (OSError, ValueError):
                pass
    prepend = expand_path_prepend(cfg)
    if prepend:
        joined = ":".join(prepend)
        return ["sh", "-lc", f'export PATH="{joined}:$PATH"; exec factory {quoted}']
    return ["sh", "-lc", f"exec factory {quoted}"]


def fleet_wake(machine: str | None = None) -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    machines = discover_machines()
    if machine:
        machines = [m for m in machines if m["name"] == machine]
    for m in machines:
        rc, out = _run_on_machine(
            m,
            _factory_shell_cmd("worker", "--all"),
            bg=True,
            name=sanitize_hc_name(f"factory-worker-{m['name']}"),
        )
        if rc != 0 and '"name_taken"' in out:
            results.append((m["name"], 0, "already running"))
        else:
            results.append((m["name"], rc, out))
    return results


def fleet_pause(machine: str | None = None, *, drain: bool = False) -> list[tuple[str, int, str]]:
    if not machine:
        set_host_paused(True, drain=drain)
        return [("local", 0, "paused")]
    cmd = _factory_shell_cmd("pause", "--drain") if drain else _factory_shell_cmd("pause")
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
            rc, out = _run_on_machine(m, _factory_shell_cmd("unpause"))
            results.append((m["name"], rc, out))
    return results


def fleet_update(machine: str | None = None) -> list[tuple[str, int, str]]:
    """Pull latest software-factory-agy on all (or one) fleet machine(s)."""
    update_script = (
        'DIR=$(python3 -c "import os,factory; print(os.path.dirname(list(factory.__path__)[0]))" 2>/dev/null || echo "$HOME/work/software-factory-agy"); '
        'BRANCH=$(git -C "$DIR" symbolic-ref --short HEAD 2>/dev/null || echo main); '
        'git -C "$DIR" pull --ff-only origin "$BRANCH"'
    )
    machines = discover_machines()
    if machine:
        machines = [m for m in machines if m["name"] == machine]
    if not machines:
        res = subprocess.run(["sh", "-lc", update_script], capture_output=True, text=True, timeout=30.0, check=False)
        return [("local", res.returncode, (res.stdout or res.stderr or "").strip())]
    results: list[tuple[str, int, str]] = []
    for m in machines:
        rc, out = _run_on_machine(m, ["sh", "-lc", update_script])
        results.append((m["name"], rc, out))
    return results


def fleet_restart(machine: str | None = None) -> list[tuple[str, int, str]]:
    """Stop running factory worker terminals/processes on fleet machine(s) and wake them fresh."""
    hc = _hc_bin()
    stop_script = 'pkill -f "factory(\\.cli)? worker" 2>/dev/null || true'
    machines = discover_machines()
    if machine:
        machines = [m for m in machines if m["name"] == machine]
    for m in machines:
        term_name = sanitize_hc_name(f"factory-worker-{m['name']}")
        if hc and m.get("via") in ("local", "hc"):
            term_ref = term_name if m.get("via") == "local" else f"{m['name']}/{term_name}"
            subprocess.run([hc, "kill", term_ref], capture_output=True, text=True, timeout=15.0, check=False)
        else:
            _run_on_machine(m, ["sh", "-lc", stop_script])
    return fleet_wake(machine)


def fleet_remove(machine: str) -> tuple[int, str]:
    """Remove a peer machine from hc (`hc machine rm`) and/or fleet.json."""
    machines = discover_machines()
    match = next((m for m in machines if m["name"] == machine), None)
    if match is None:
        return 1, f"machine '{machine}' not found"
    if match.get("via") == "local":
        return 1, f"cannot remove local machine '{machine}'"

    removed_msgs: list[str] = []
    hc = _hc_bin()
    if match.get("via") == "hc" and hc:
        res = subprocess.run([hc, "machine", "rm", machine], capture_output=True, text=True, timeout=10.0, check=False)
        if res.returncode != 0:
            return res.returncode, (res.stdout or res.stderr or "").strip()
        removed_msgs.append("removed from hc")

    fleet_path = os.path.join(host_config_dir(), "fleet.json")
    if os.path.isfile(fleet_path):
        try:
            with open(fleet_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("machines", []) if isinstance(data, dict) else data
            if isinstance(entries, list):
                new_entries = [
                    item
                    for item in entries
                    if not (
                        (isinstance(item, str) and item == machine)
                        or (isinstance(item, dict) and item.get("name") == machine)
                    )
                ]
                if len(new_entries) != len(entries):
                    new_data = {**data, "machines": new_entries} if isinstance(data, dict) else new_entries
                    with open(fleet_path, "w", encoding="utf-8") as f:
                        json.dump(new_data, f, indent=2)
                    removed_msgs.append("removed from fleet.json")
        except (OSError, ValueError):
            pass

    return 0, ", ".join(removed_msgs) or "removed"
