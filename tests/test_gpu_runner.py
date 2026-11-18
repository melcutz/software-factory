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
        self.status_payload: dict = {
            "ollama_host": "http://127.0.0.1:11434",
            "currently_warm_in_vram": ["minicpm5-128k:latest"],
            "multimodal_sidecars": {},
            "model_catalog": {
                "minicpm5-128k:latest": {"installed": True, "tier": 1},
                "bge-m3:latest": {"installed": False, "tier": 2},
            },
        }
        self.decide_payload: dict = {}
        self.classify_payload: dict = {
            "model_used": "minicpm5-128k:latest",
            "wall_s": 0.45,
            "new_high_volume_items": {},
            "classifications": {},
            "added_by_category": {},
            "excluded_already_in_filters": [],
            "excluded_below_min_count": [],
        }
        self.seen_methods: list[str] = []
        self.seen_session_headers: list[str | None] = []
        self.seen_tool_calls: list[tuple[str, dict]] = []


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
                tool_args = params.get("arguments") or {}
                state.seen_tool_calls.append((str(tool_name), tool_args))
                if tool_name == "local_gpu_status":
                    payload = state.status_payload
                elif tool_name == "local_system1_jev_decide":
                    payload = state.decide_payload
                elif tool_name == "local_batch_classify_and_filter":
                    payload = state.classify_payload
                else:
                    payload = {"error": f"unknown tool {tool_name}", "tool": str(tool_name)}
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
    state.status_payload = {
        "ollama_host": "http://127.0.0.1:11434",
        "model_catalog": {"qwen-0.5b:latest": {"installed": True}},
    }
    server, url = _start_fake_mcp_server(state)
    try:
        assert gpu_available(url, force=True) is False

        # Catalog lists minicpm5 with installed=false -> False (regression: no substring search)
        state.status_payload = {
            "ollama_host": "http://127.0.0.1:11434",
            "model_catalog": {"minicpm5-128k:latest": {"installed": False, "tier": 1}},
        }
        assert gpu_available(url, force=True) is False

        # Present minicpm5 with installed=true -> True
        state.status_payload = {
            "ollama_host": "http://127.0.0.1:11434",
            "model_catalog": {
                "minicpm5-128k:latest": {"installed": True, "tier": 1},
                "bge-m3:latest": {"installed": False},
            },
        }
        assert gpu_available(url, force=True) is True

        # Cached True even if status changes until force=True
        state.status_payload = {"model_catalog": {}}
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
        # High confidence ACCEPT via run_agent (real contract shape)
        state.decide_payload = {
            "model_used": "minicpm5-128k:latest",
            "decisions_count": 4,
            "decisions": {
                "accept": {
                    "type": "bool",
                    "decision": True,
                    "confidence": 0.94,
                    "distribution": {"True": 0.94, "False": 0.06},
                    "true_probability": 0.94,
                },
                "risk": {
                    "type": "choice",
                    "decision": "low",
                    "confidence": 0.75,
                    "distribution": {"low": 0.75, "medium": 0.20, "high": 0.05},
                },
                "touches_ui": {
                    "type": "bool",
                    "decision": True,
                    "confidence": 0.80,
                    "distribution": {"True": 0.80, "False": 0.20},
                    "true_probability": 0.80,
                },
                "touches_security": {
                    "type": "bool",
                    "decision": False,
                    "confidence": 0.90,
                    "distribution": {"True": 0.10, "False": 0.90},
                    "true_probability": 0.10,
                },
            },
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

        # Verify request sent "id" and "options" (not "name" / "choices")
        decide_calls = [args for name, args in state.seen_tool_calls if name == "local_system1_jev_decide"]
        assert len(decide_calls) == 1
        sent_decisions = decide_calls[0]["decisions"]
        assert all("id" in d and "name" not in d for d in sent_decisions)
        risk_dec = next(d for d in sent_decisions if d["id"] == "risk")
        assert risk_dec["options"] == ["low", "medium", "high"]
        assert "choices" not in risk_dec

        # Fallback to distribution["True"] when true_probability omitted
        state.decide_payload = {
            "decisions": {
                "accept": {"type": "bool", "distribution": {"True": 0.92, "False": 0.08}},
                "risk": {"type": "choice", "distribution": {"low": 0.8, "medium": 0.15, "high": 0.05}},
                "touches_ui": {"type": "bool", "distribution": {"True": 0.1, "False": 0.9}},
                "touches_security": {"type": "bool", "distribution": {"True": 0.1, "False": 0.9}},
            }
        }
        env = run_gpu_stage("intake", "gpu:jev", "Clear issue", mcp_url=url, min_confidence=0.90)
        assert env["structured_output"]["decision"] == "ACCEPT"

        # Error payload -> GpuAbstain
        state.decide_payload = {"error": "model timeout", "tool": "local_system1_jev_decide"}
        with pytest.raises(GpuAbstain):
            run_gpu_stage("intake", "gpu:jev", "Clear issue", mcp_url=url, min_confidence=0.90)

        # Below threshold -> GpuAbstain
        state.decide_payload = {
            "decisions": {
                "accept": {"type": "bool", "true_probability": 0.89, "distribution": {"True": 0.89, "False": 0.11}},
                "risk": {"type": "choice", "distribution": {"low": 0.9, "medium": 0.1, "high": 0.0}},
                "touches_ui": {"type": "bool", "true_probability": 0.0},
                "touches_security": {"type": "bool", "true_probability": 0.0},
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
        "model_used": "minicpm5-128k:latest",
        "decisions": {
            "safe_to_merge": {"type": "bool", "true_probability": 0.95, "distribution": {"True": 0.95, "False": 0.05}},
            "merge_danger": {"type": "bool", "true_probability": 0.02, "distribution": {"True": 0.02, "False": 0.98}},
        },
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
            "safe_to_merge": {"type": "bool", "true_probability": 0.95},
            "merge_danger": {"type": "bool", "true_probability": 0.02},
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
        state.decide_payload = {
            "decisions": {
                "safe_to_merge": {"type": "bool", "true_probability": 0.95},
                "merge_danger": {"type": "bool", "true_probability": 0.15},
            }
        }
        with pytest.raises(GpuAbstain):
            run_gpu_stage("approve", "gpu:jev", clean_prompt, mcp_url=url)

        # Low safe_to_merge -> abstain
        state.decide_payload = {
            "decisions": {
                "safe_to_merge": {"type": "bool", "true_probability": 0.85},
                "merge_danger": {"type": "bool", "true_probability": 0.01},
            }
        }
        with pytest.raises(GpuAbstain):
            run_gpu_stage("approve", "gpu:jev", clean_prompt, mcp_url=url)

        # Error payload -> abstain
        state.decide_payload = {"error": "gpu oom", "tool": "local_system1_jev_decide"}
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
