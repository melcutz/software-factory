"""Bridge to herdr-core (hc) background child terminals and OSC 7501 status protocol."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import TextIO


def sanitize_hc_name(raw: str) -> str:
    """Sanitize a terminal name to hc's required 1-32 chars of [a-z0-9._-]."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-._")
    return (cleaned or "worker")[:32]


class HerdrBridge:
    """Emits OSC 7501 status sequences and spawns hc background child terminals."""

    def __init__(
        self,
        *,
        hc_bin: str = "",
        terminal_id: str = "",
        stream: TextIO | None = None,
    ) -> None:
        self.hc_bin = hc_bin
        self.terminal_id = terminal_id
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, *, stream: TextIO | None = None) -> HerdrBridge:
        terminal_id = (os.environ.get("HC_TERMINAL_ID") or "").strip()
        hc_bin = (os.environ.get("HC_BIN_PATH") or shutil.which("hc") or "").strip()
        return cls(hc_bin=hc_bin, terminal_id=terminal_id, stream=stream)

    @property
    def enabled(self) -> bool:
        return bool(self.terminal_id)

    def emit_status(
        self,
        state: str,
        *,
        kind: str = "",
        progress: int | None = None,
    ) -> None:
        """Emit an OSC 7501 Program Status Protocol sequence when inside herdr-core."""
        if not self.enabled:
            return
        parts = [f"state={state}"]
        if kind:
            parts.append(f"kind={kind}")
        if progress is not None:
            clamped = max(0, min(100, int(progress)))
            parts.append(f"progress={clamped}")
        seq = f"\033]7501;{':'.join(parts)}\033\\"
        with self._lock:
            try:
                self._stream.write(seq)
                self._stream.flush()
            except (OSError, ValueError):
                pass

    def spawn_job_terminal(self, name: str, cwd: str, cmd: list[str]) -> str | None:
        """Spawn a background child terminal via `hc run --bg` when inside herdr-core."""
        if not self.enabled or not self.hc_bin:
            return None
        safe_name = sanitize_hc_name(name)
        full_cmd = [self.hc_bin, "run", "--bg", "--name", safe_name, "--cwd", cwd, "--", *cmd]
        try:
            res = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        out = (res.stdout or "").strip()
        return out.splitlines()[-1].strip() if out else name

    def list_terminals(self) -> list[str]:
        """Return lines from `hc ls`, or [] if hc is unavailable."""
        if not self.hc_bin:
            return []
        try:
            res = subprocess.run(
                [self.hc_bin, "ls"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if res.returncode != 0:
            return []
        return [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]

    def list_live_terminal_names(self) -> set[str]:
        """Return names of currently live `hc` terminals via `hc ls --json`, or empty set."""
        if not self.hc_bin:
            return set()
        try:
            res = subprocess.run(
                [self.hc_bin, "ls", "--json"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        if res.returncode != 0:
            return set()
        try:
            data = json.loads(res.stdout or "{}")
        except ValueError:
            return set()
        return {
            str(t["name"])
            for t in (data.get("terminals") or [])
            if isinstance(t, dict) and t.get("state") == "live" and t.get("name")
        }
