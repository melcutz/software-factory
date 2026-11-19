import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from factory.lib.gpu_runner import clear_gpu_availability_cache
from factory.lib.pipeline import run_doctor


def _init_doctor_repo(tmp_path: Path, *, models: dict, gpu_url: str | None = None) -> Path:
    repo = tmp_path / "repo"
    (repo / "factory").mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)], check=True)
    (repo / ".gitignore").write_text("/factory/jobs/*\n/.worktrees/\n", encoding="utf-8")

    checks = repo / "factory" / "checks.sh"
    checks.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    checks.chmod(0o755)

    cfg: dict = {"models": models}
    if gpu_url:
        cfg["gpu"] = {"mcp_url": gpu_url, "min_confidence": 0.9}
    (repo / "factory" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return repo


def _install_fake_cli_bins(bin_dir: Path, *, opencode_rc: int | None = 0) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("gh", "agy"):
        p = bin_dir / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    if opencode_rc is not None:
        oc = bin_dir / "opencode"
        oc.write_text(f"#!/bin/sh\necho 'opencode 1.2.3'\nexit {opencode_rc}\n", encoding="utf-8")
        oc.chmod(0o755)


def _start_fake_gpu_server(catalog: dict):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            method = req.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Mcp-Session-Id", "doc-sid")
                self.end_headers()
                return
            if method == "initialize":
                rpc_resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
                }
            elif method == "tools/list":
                rpc_resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {"tools": [{"name": "decide"}, {"name": "classify"}]},
                }
            else:
                rpc_resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"model_catalog": catalog})}],
                        "isError": False,
                    },
                }
            body = json.dumps(rpc_resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Mcp-Session-Id", "doc-sid")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/mcp"


def test_doctor_checks_opencode_binary_when_configured(tmp_path: Path, monkeypatch):
    clear_gpu_availability_cache()
    monkeypatch.delenv("FACTORY_OPENCODE_BIN", raising=False)
    git_dir = Path(subprocess.check_output(["which", "git"], text=True).strip()).parent
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    bin_dir = tmp_path / "bin"
    _install_fake_cli_bins(bin_dir, opencode_rc=None)
    monkeypatch.setenv("PATH", f"{bin_dir}:{git_dir}")

    repo = _init_doctor_repo(
        tmp_path,
        models={"builder_low": ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"]},
    )

    # Missing opencode -> fail with neutral guidance
    ok, msgs = run_doctor(str(repo))
    assert ok is False
    assert any("FAIL: opencode not found; set providers.opencode.bin or path_prepend" in m for m in msgs)

    # Failing opencode --version -> fail
    _install_fake_cli_bins(bin_dir, opencode_rc=2)
    ok, msgs = run_doctor(str(repo))
    assert ok is False
    assert any("opencode" in m and "exited 2" in m for m in msgs)

    # Working opencode --version -> pass
    _install_fake_cli_bins(bin_dir, opencode_rc=0)
    ok, msgs = run_doctor(str(repo))
    assert ok is True
    assert any("OK: `opencode --version` (opencode 1.2.3)" in m for m in msgs)


def test_doctor_warns_on_unreachable_or_missing_gpu_without_failing(tmp_path: Path, monkeypatch):
    clear_gpu_availability_cache()
    git_dir = Path(subprocess.check_output(["which", "git"], text=True).strip()).parent
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    bin_dir = tmp_path / "bin"
    _install_fake_cli_bins(bin_dir, opencode_rc=None)
    monkeypatch.setenv("PATH", f"{bin_dir}:{git_dir}")
    monkeypatch.delenv("FACTORY_GPU_MCP_URL", raising=False)

    # Disabled GPU (mcp_url=None) -> OK disabled message
    repo = _init_doctor_repo(
        tmp_path,
        models={"intake": ["gpu:decision-model", "gemini-4-hydrogen-low"]},
        gpu_url=None,
    )
    ok, msgs = run_doctor(str(repo))
    assert ok is True
    assert any("OK: gpu decision MCP: disabled (gpu.mcp_url not set)" in m for m in msgs)

    # Unreachable GPU -> WARN, ok stays True
    (repo / "factory" / "config.json").write_text(
        json.dumps(
            {
                "models": {"intake": ["gpu:decision-model", "gemini-4-hydrogen-low"]},
                "gpu": {"mcp_url": "http://127.0.0.1:1/mcp", "min_confidence": 0.9},
            }
        ),
        encoding="utf-8",
    )
    ok, msgs = run_doctor(str(repo))
    assert ok is True
    assert any(m.startswith("WARN: GPU MCP unreachable") for m in msgs)

    # Reachable GPU -> OK
    server, url = _start_fake_gpu_server({"decision-model:latest": {"installed": True}})
    try:
        (repo / "factory" / "config.json").write_text(
            json.dumps(
                {
                    "models": {"intake": ["gpu:decision-model", "gemini-4-hydrogen-low"]},
                    "gpu": {"mcp_url": url, "min_confidence": 0.9},
                }
            ),
            encoding="utf-8",
        )
        ok, msgs = run_doctor(str(repo))
        assert ok is True
        assert any(m.startswith("OK: GPU MCP reachable") for m in msgs)
    finally:
        server.shutdown()
        server.server_close()


def test_doctor_reports_loaded_config_layers_without_leaking_values(tmp_path: Path, monkeypatch):
    clear_gpu_availability_cache()
    git_dir = Path(subprocess.check_output(["which", "git"], text=True).strip()).parent
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    bin_dir = tmp_path / "bin"
    _install_fake_cli_bins(bin_dir, opencode_rc=0)
    monkeypatch.setenv("PATH", f"{bin_dir}:{git_dir}")

    user_cfg = tmp_path / "user-config.json"
    user_cfg.write_text(
        json.dumps({"base_branch": "secret-branch-value-xyz", "concurrency": 7}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FACTORY_USER_CONFIG", str(user_cfg))

    repo = _init_doctor_repo(tmp_path, models={"intake": "gemini-3.8-flash-low"})
    local_cfg = repo / "factory" / "config.local.json"
    local_cfg.write_text(json.dumps({"concurrency": 3}), encoding="utf-8")

    ok, msgs = run_doctor(str(repo))
    assert ok is True
    joined = "\n".join(msgs)
    assert str(user_cfg) in joined
    assert str(repo / "factory" / "config.json") in joined
    assert str(local_cfg) in joined
    assert "secret-branch-value-xyz" not in joined
