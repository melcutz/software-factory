import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from factory.lib.agy_runner import run_agent
from factory.lib.gpu_runner import (
    GpuAbstain,
    McpStreamableClient,
    classify,
    clear_gpu_availability_cache,
    decide,
    gpu_available,
    resolve_gpu_mcp_url,
    run_gpu_stage,
)
from factory.lib.schemas import SCHEMAS


class _FakeMcpState:
    def __init__(self, *, sse_mode: bool = False) -> None:
        self.sse_mode = sse_mode
        self.session_id = "mcp-test-session-42"
        self.status_payload: dict = {"status": "ok", "models": ["minicpm5"]}
        self.decide_payload: dict = {}
        self.classify_payload: dict = {"kept": [], "dropped": []}
        self.seen_methods: list[str] = []
        self.seen_session_headers: list[str | None] = []


def _start_fake_mcp_server(state: _FakeMcpState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            req = json.loads(raw)
            method = req.get("method")
            state.seen_methods.append(method)
            state.seen_session_headers.append(self.headers.get("Mcp-Session-Id"))

            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Mcp-Session-Id", state.session_id)
                self.end_headers()
                return

            if method == "initialize":
                rpc_resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-gpu-mcp", "version": "0.1"},
                    },
                }
            elif method == "tools/call":
                params = req.get("params") or {}
                tool_name = params.get("name")
                if tool_name == "local_gpu_status":
                    payload = state.status_payload
                elif tool_name == "local_system1_jev_decide":
                    payload = state.decide_payload
                elif tool_name == "local_batch_classify_and_filter":
                    payload = state.classify_payload
                else:
                    payload = {"error": f"unknown tool {tool_name}"}
                rpc_resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                        "isError": False,
                    },
                }
            else:
                rpc_resp = {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            self.send_response(200)
            self.send_header("Mcp-Session-Id", state.session_id)
            if state.sse_mode:
                body = f"event: message\ndata: {json.dumps(rpc_resp)}\n\n".encode("utf-8")
                self.send_header("Content-Type", "text/event-stream")
            else:
                body = json.dumps(rpc_resp).encode("utf-8")
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    return server, url


def _init_git_wt(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return wt


@pytest.fixture(autouse=True)
def _clean_gpu_cache():
    clear_gpu_availability_cache()
    yield
    clear_gpu_availability_cache()


@pytest.mark.parametrize("sse_mode", [False, True])
def test_mcp_client_sse_and_plain_json_responses(sse_mode: bool):
    state = _FakeMcpState(sse_mode=sse_mode)
    state.classify_payload = {"kept": [{"id": 1, "category": "bug"}], "dropped": []}
    server, url = _start_fake_mcp_server(state)
    try:
        out = classify(url, [{"id": 1, "text": "fix crash"}], ["bug", "docs"])
        assert out == {"kept": [{"id": 1, "category": "bug"}], "dropped": []}
        assert state.seen_methods == ["initialize", "notifications/initialized", "tools/call"]
        assert state.seen_session_headers[0] is None
        assert state.seen_session_headers[1] == "mcp-test-session-42"
        assert state.seen_session_headers[2] == "mcp-test-session-42"
    finally:
        server.shutdown()
        server.server_close()


def test_gpu_available_false_when_unreachable_or_missing_minicpm5():
    # Unreachable port -> False
    assert gpu_available("http://127.0.0.1:1/mcp", force=True) is False

    # Missing minicpm5 -> False
    state = _FakeMcpState(sse_mode=False)
    state.status_payload = {"status": "ok", "models": ["qwen-0.5b"]}
    server, url = _start_fake_mcp_server(state)
    try:
        assert gpu_available(url, force=True) is False

        # Present minicpm5 -> True
        state.status_payload = {"status": "ok", "models": ["minicpm5:latest"]}
        assert gpu_available(url, force=True) is True

        # Cached True even if status changes until force=True
        state.status_payload = {"status": "ok", "models": []}
        assert gpu_available(url, cache_ttl_sec=60.0, force=False) is True
        assert gpu_available(url, force=True) is False
    finally:
        server.shutdown()
        server.server_close()


def test_resolve_gpu_mcp_url_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FACTORY_GPU_MCP_URL", raising=False)
    assert resolve_gpu_mcp_url() == "http://rocket-ubuntu:8000/mcp"

    fdir = tmp_path / "factory"
    fdir.mkdir()
    (fdir / "config.json").write_text(
        json.dumps({"gpu": {"mcp_url": "http://127.0.0.1:9999/mcp"}}),
        encoding="utf-8",
    )
    assert resolve_gpu_mcp_url(factory_dir=str(fdir)) == "http://127.0.0.1:9999/mcp"

    monkeypatch.setenv("FACTORY_GPU_MCP_URL", "http://127.0.0.1:7777/mcp")
    assert resolve_gpu_mcp_url(factory_dir=str(fdir)) == "http://127.0.0.1:7777/mcp"


def test_gpu_intake_accept_and_abstain_thresholds(tmp_path: Path, monkeypatch):
    wt = _init_git_wt(tmp_path)
    state = _FakeMcpState(sse_mode=True)
    server, url = _start_fake_mcp_server(state)
    monkeypatch.setenv("FACTORY_GPU_MCP_URL", url)
    try:
        # High confidence ACCEPT via run_agent
        state.decide_payload = {
            "decisions": {
                "accept": {"p": 0.94},
                "risk": {"probs": {"low": 0.75, "medium": 0.20, "high": 0.05}},
                "touches_ui": {"p": 0.80},
                "touches_security": {"p": 0.10},
            }
        }
        out, usage, _ = run_agent(
            os.path.abspath("factory"),
            str(tmp_path / "job-gpu-intake"),
            str(wt),
            "intake",
            "intake",
            "gpu:jev",
            "Add button tooltip",
            SCHEMAS["intake"],
            60,
            sandbox=True,
            log_name="gpu-intake",
        )
        assert out["decision"] == "ACCEPT"
        assert out["risk"] == "low"
        assert out["surfaces"] == ["ui"]
        assert "0.94" in out["reasoning"]
        assert usage["provider"] == "gpu"
        assert usage["tier"] == "local"
        assert usage["model"] == "gpu:jev"
        assert usage["cost_usd"] == 0.0

        # Below threshold -> GpuAbstain
        state.decide_payload = {
            "decisions": {
                "accept": {"p": 0.89},
                "risk": {"probs": {"low": 0.9, "medium": 0.1, "high": 0.0}},
                "touches_ui": {"p": 0.0},
                "touches_security": {"p": 0.0},
            }
        }
        with pytest.raises(GpuAbstain):
            run_gpu_stage("intake", "gpu:jev", "Ambiguous issue", mcp_url=url, min_confidence=0.90)
    finally:
        server.shutdown()
        server.server_close()


def test_gpu_approve_only_low_risk_clean_reviews_high_confidence():
    state = _FakeMcpState(sse_mode=False)
    state.decide_payload = {
        "decisions": {
            "safe_to_merge": {"p": 0.95},
            "merge_danger": {"p": 0.02},
        }
    }
    server, url = _start_fake_mcp_server(state)
    clean_prompt = (
        "PR #12\nRISK: low\n\n"
        "## Gate Status\nVERDICT: PASS\n\n"
        "## Reviews\n- code: VERDICT: PASS\n"
    )
    try:
        env = run_gpu_stage("approve", "gpu:jev", clean_prompt, mcp_url=url, min_confidence=0.90)
        assert env["structured_output"]["decision"] == "APPROVE"
        assert env["usage"]["cost_usd"] == 0.0
    finally:
        server.shutdown()
        server.server_close()


def test_gpu_approve_abstains_on_high_risk_danger_or_low_confidence():
    state = _FakeMcpState(sse_mode=False)
    state.decide_payload = {
        "decisions": {
            "safe_to_merge": {"p": 0.95},
            "merge_danger": {"p": 0.02},
        }
    }
    server, url = _start_fake_mcp_server(state)
    try:
        # High risk -> abstain before decide()
        with pytest.raises(GpuAbstain):
            run_gpu_stage(
                "approve",
                "gpu:jev",
                "RISK: high\n## Gate Status\nVERDICT: PASS\n## Reviews\nVERDICT: PASS\n",
                mcp_url=url,
            )
        # Review requested changes -> abstain
        with pytest.raises(GpuAbstain):
            run_gpu_stage(
                "approve",
                "gpu:jev",
                "RISK: low\n## Gate Status\nVERDICT: PASS\n## Reviews\nVERDICT: CHANGES\n",
                mcp_url=url,
            )
        # High merge_danger -> abstain
        clean_prompt = "RISK: low\n## Gate Status\nVERDICT: PASS\n## Reviews\nVERDICT: PASS\n"
        state.decide_payload = {"decisions": {"safe_to_merge": {"p": 0.95}, "merge_danger": {"p": 0.15}}}
        with pytest.raises(GpuAbstain):
            run_gpu_stage("approve", "gpu:jev", clean_prompt, mcp_url=url)

        # Low safe_to_merge -> abstain
        state.decide_payload = {"decisions": {"safe_to_merge": {"p": 0.85}, "merge_danger": {"p": 0.01}}}
        with pytest.raises(GpuAbstain):
            run_gpu_stage("approve", "gpu:jev", clean_prompt, mcp_url=url)
    finally:
        server.shutdown()
        server.server_close()


def test_gpu_planner_trivial_single_issue_vs_multi_issue_abstain():
    state = _FakeMcpState(sse_mode=False)
    server, url = _start_fake_mcp_server(state)
    try:
        single_prompt = "## Issue #10: Fix typo\nDetails here.\n"
        env = run_gpu_stage("plan", "gpu:jev", single_prompt, mcp_url=url)
        assert env["structured_output"]["edges"] == []

        multi_prompt = "## Issue #10: One\n...\n## Issue #11: Two\n...\n"
        with pytest.raises(GpuAbstain):
            run_gpu_stage("plan", "gpu:jev", multi_prompt, mcp_url=url)
    finally:
        server.shutdown()
        server.server_close()


def test_gpu_unsupported_stage_abstains():
    state = _FakeMcpState(sse_mode=False)
    server, url = _start_fake_mcp_server(state)
    try:
        for stage in ("build", "spec", "review", "qa"):
            with pytest.raises(GpuAbstain):
                run_gpu_stage(stage, "gpu:jev", "some prompt", mcp_url=url)
    finally:
        server.shutdown()
        server.server_close()
