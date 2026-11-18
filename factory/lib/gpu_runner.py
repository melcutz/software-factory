"""Local GPU MCP streamable-HTTP client and abstain-safe stage adapters."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

DEFAULT_GPU_MCP_URL = "http://rocket-ubuntu:8000/mcp"
_GPU_AVAIL_LOCK = threading.Lock()
_GPU_AVAIL_CACHE: dict[str, tuple[float, bool]] = {}

_RISK_LOW_RE = re.compile(r"\bRISK\s*:\s*low\b", re.IGNORECASE)
_RISK_HIGH_MED_RE = re.compile(r"\bRISK\s*:\s*(?:medium|high)\b", re.IGNORECASE)
_ISSUE_HEADER_RE = re.compile(r"(?m)^##\s+Issue\s+#\d+")


class GpuAbstain(RuntimeError):
    """Raised when the local GPU tier cannot handle a stage or lacks confidence."""


def clear_gpu_availability_cache() -> None:
    """Clear cached `gpu_available` results (primarily for tests)."""
    with _GPU_AVAIL_LOCK:
        _GPU_AVAIL_CACHE.clear()


def resolve_gpu_mcp_url(factory_dir: str | None = None, config: dict | None = None) -> str:
    """Resolve GPU MCP URL: env FACTORY_GPU_MCP_URL > config['gpu']['mcp_url'] > default."""
    env_url = (os.environ.get("FACTORY_GPU_MCP_URL") or "").strip()
    if env_url:
        return env_url
    cfg = config
    if cfg is None and factory_dir:
        cfg_path = os.path.join(factory_dir, "config.json")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    cfg = raw
            except (OSError, ValueError):
                cfg = None
    if isinstance(cfg, dict) and isinstance(cfg.get("gpu"), dict):
        cfg_url = str(cfg["gpu"].get("mcp_url") or "").strip()
        if cfg_url:
            return cfg_url
    return DEFAULT_GPU_MCP_URL


class McpStreamableClient:
    """Minimal stdlib MCP streamable-HTTP client supporting SSE and plain JSON responses."""

    def __init__(self, url: str, connect_timeout: float = 5.0, call_timeout: float = 60.0) -> None:
        self.url = url
        self.connect_timeout = float(connect_timeout)
        self.call_timeout = float(call_timeout)
        self._session_id: str | None = None

    def _rpc(
        self,
        method: str,
        params: dict | None = None,
        req_id: int | None = 1,
        timeout: float | None = None,
    ) -> dict | None:
        body: dict = {"jsonrpc": "2.0", "method": method}
        if req_id is not None:
            body["id"] = req_id
        if params is not None:
            body["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        eff_timeout = self.call_timeout if timeout is None else float(timeout)
        with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            raw_text = resp.read().decode("utf-8")

        if req_id is None:
            return None
        if not raw_text.strip():
            return None

        # Parse SSE `data: {...}` lines if present, else plain JSON
        last_sse_obj: dict | None = None
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                payload = stripped[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    last_sse_obj = parsed
        if last_sse_obj is not None:
            return last_sse_obj

        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON-RPC object from MCP server, got {type(parsed).__name__}")
        return parsed

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Initialize MCP session, send notifications/initialized, and call tool."""
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "software-factory", "version": "1.0"},
            },
            req_id=1,
            timeout=self.connect_timeout,
        )
        self._rpc("notifications/initialized", None, req_id=None, timeout=self.connect_timeout)
        resp = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            req_id=2,
            timeout=self.call_timeout,
        )
        if not isinstance(resp, dict):
            raise RuntimeError(f"Empty MCP response for tool {name}")
        if "error" in resp and resp["error"] is not None:
            raise RuntimeError(f"MCP error for {name}: {resp['error']}")

        result = resp.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected MCP result for {name}: {resp!r}")
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {name} returned isError: {result}")

        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured

        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        return {"text": text}
        return result


def gpu_available(mcp_url: str, *, cache_ttl_sec: float = 60.0, force: bool = False) -> bool:
    """Return True if GPU MCP endpoint is reachable and reports minicpm5 installed."""
    now = time.monotonic()
    if not force:
        with _GPU_AVAIL_LOCK:
            cached = _GPU_AVAIL_CACHE.get(mcp_url)
            if cached is not None and (now - cached[0]) < cache_ttl_sec:
                return cached[1]

    available = False
    try:
        status = McpStreamableClient(mcp_url, connect_timeout=5.0, call_timeout=10.0).call_tool(
            "local_gpu_status", {}
        )
        if isinstance(status, dict) and not status.get("error"):
            available = "minicpm5" in json.dumps(status).lower()
    except Exception:
        available = False

    with _GPU_AVAIL_LOCK:
        _GPU_AVAIL_CACHE[mcp_url] = (time.monotonic(), available)
    return available


def decide(mcp_url: str, state: str, decisions: list[dict], *, timeout_sec: float = 60.0) -> dict:
    """Call MCP `local_system1_jev_decide`."""
    return McpStreamableClient(mcp_url, connect_timeout=5.0, call_timeout=timeout_sec).call_tool(
        "local_system1_jev_decide",
        {"state": state, "decisions": decisions},
    )


def classify(
    mcp_url: str,
    items: list[dict],
    allowed_categories: list[str],
    *,
    timeout_sec: float = 60.0,
) -> dict:
    """Call MCP `local_batch_classify_and_filter`."""
    return McpStreamableClient(mcp_url, connect_timeout=5.0, call_timeout=timeout_sec).call_tool(
        "local_batch_classify_and_filter",
        {"items": items, "allowed_categories": allowed_categories},
    )


def _extract_decision_map(resp: dict) -> dict[str, dict]:
    """Normalize decision payload from `local_system1_jev_decide` into {name: entry}."""
    raw = resp.get("decisions") if isinstance(resp.get("decisions"), dict) else resp
    if isinstance(resp.get("results"), list):
        out: dict[str, dict] = {}
        for item in resp["results"]:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                out[item["name"]] = item
        return out
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                out[k] = v
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = {"p": float(v)}
    return out


def _get_bool_prob(decisions_map: dict[str, dict], name: str, default: float = 0.0) -> float:
    entry = decisions_map.get(name)
    if not isinstance(entry, dict):
        return default
    for key in ("p", "prob", "probability", "p_true", "score"):
        val = entry.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    probs = entry.get("probs")
    if isinstance(probs, dict):
        for tkey in ("true", "True", "yes", "1"):
            val = probs.get(tkey)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
    return default


def _get_choice_argmax(
    decisions_map: dict[str, dict],
    name: str,
    choices: tuple[str, ...] = ("low", "medium", "high"),
) -> str:
    entry = decisions_map.get(name)
    if not isinstance(entry, dict):
        return "medium"
    probs = entry.get("probs")
    if isinstance(probs, dict):
        best_choice = None
        best_val = -1.0
        for ch in choices:
            val = probs.get(ch)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if float(val) > best_val:
                    best_val = float(val)
                    best_choice = ch
        if best_choice is not None:
            return best_choice
    for key in ("choice", "value", "label"):
        val = entry.get(key)
        if isinstance(val, str) and val.lower() in choices:
            return val.lower()
    return "medium"


def _run_intake_gpu(
    task_prompt: str,
    *,
    mcp_url: str,
    min_confidence: float,
    timeout_sec: float,
) -> dict:
    try:
        resp = decide(
            mcp_url,
            task_prompt,
            decisions=[
                {"name": "accept", "type": "bool", "question": "Is this issue clear, actionable, and in-scope to ACCEPT?"},
                {"name": "risk", "type": "choice", "choices": ["low", "medium", "high"], "question": "What is the implementation risk?"},
                {"name": "touches_ui", "type": "bool", "question": "Does this issue touch UI/frontend?"},
                {"name": "touches_security", "type": "bool", "question": "Does this issue touch auth, security, or secrets?"},
            ],
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        raise GpuAbstain(f"GPU intake call failed: {exc}") from exc

    dmap = _extract_decision_map(resp)
    p_accept = _get_bool_prob(dmap, "accept", default=0.0)
    if p_accept < min_confidence:
        raise GpuAbstain(f"GPU intake abstained: p_accept={p_accept:.2f} < {min_confidence:.2f}")

    risk = _get_choice_argmax(dmap, "risk", ("low", "medium", "high"))
    surfaces: list[str] = []
    if _get_bool_prob(dmap, "touches_security", default=0.0) >= 0.5:
        surfaces.append("security")
    if _get_bool_prob(dmap, "touches_ui", default=0.0) >= 0.5:
        surfaces.append("ui")

    note = f"GPU fast-path triage (p={p_accept:.2f})"
    return {
        "decision": "ACCEPT",
        "risk": risk,
        "surfaces": sorted(surfaces),
        "summary": [note],
        "scope_in": "Issue is clear and in-scope per GPU fast-path triage.",
        "scope_out": "None specified.",
        "questions": [],
        "reasoning": note,
    }


def _check_approve_prompt_clean(task_prompt: str) -> None:
    if _RISK_HIGH_MED_RE.search(task_prompt) or not _RISK_LOW_RE.search(task_prompt):
        raise GpuAbstain("GPU approve abstains when job risk is not explicitly low")

    gate_idx = task_prompt.find("## Gate Status")
    reviews_idx = task_prompt.find("## Reviews")
    if gate_idx == -1 or reviews_idx == -1:
        raise GpuAbstain("GPU approve abstains: missing Gate Status or Reviews section")

    gate_section = task_prompt[gate_idx:reviews_idx] if reviews_idx > gate_idx else task_prompt[gate_idx:]
    reviews_section = task_prompt[reviews_idx:]

    if "VERDICT: PASS" not in gate_section or "VERDICT: FAIL" in gate_section:
        raise GpuAbstain("GPU approve abstains: gate did not pass")
    if "VERDICT: PASS" not in reviews_section or "VERDICT: CHANGES" in reviews_section or "VERDICT: FAIL" in reviews_section:
        raise GpuAbstain("GPU approve abstains: reviews did not all pass")


def _run_approve_gpu(
    task_prompt: str,
    *,
    mcp_url: str,
    min_confidence: float,
    timeout_sec: float,
) -> dict:
    _check_approve_prompt_clean(task_prompt)
    try:
        resp = decide(
            mcp_url,
            task_prompt,
            decisions=[
                {"name": "safe_to_merge", "type": "bool", "question": "Is this PR safe to merge?"},
                {"name": "merge_danger", "type": "bool", "question": "Is there any merge danger or unresolved risk?"},
            ],
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        raise GpuAbstain(f"GPU approve call failed: {exc}") from exc

    dmap = _extract_decision_map(resp)
    p_safe = _get_bool_prob(dmap, "safe_to_merge", default=0.0)
    p_danger = _get_bool_prob(dmap, "merge_danger", default=1.0)
    if p_safe < min_confidence or p_danger >= 0.10:
        raise GpuAbstain(
            f"GPU approve abstained: p_safe={p_safe:.2f} (min {min_confidence:.2f}), p_danger={p_danger:.2f}"
        )

    return {
        "decision": "APPROVE",
        "summary": [f"GPU fast-path approve (p_safe={p_safe:.2f}, p_danger={p_danger:.2f})"],
        "reasoning": f"GPU fast-path approve: low risk, clean gate/reviews, p_safe={p_safe:.2f}, p_danger={p_danger:.2f}.",
        "merge_danger": "none",
        "question": "",
    }


def _run_plan_gpu(task_prompt: str) -> dict:
    issue_count = len(_ISSUE_HEADER_RE.findall(task_prompt or ""))
    if issue_count > 1:
        raise GpuAbstain(f"GPU planner abstains for {issue_count} issues (>1)")
    return {
        "edges": [],
        "predicted_surfaces": [],
        "reasoning": "GPU fast-path: at most one issue, no dependency edges.",
    }


def run_gpu_stage(
    stage: str,
    model: str,
    task_prompt: str,
    *,
    mcp_url: str,
    min_confidence: float = 0.90,
    timeout_sec: float = 60.0,
) -> dict:
    """Execute a supported stage on the local GPU MCP tier, or raise GpuAbstain."""
    del model  # currently all gpu:* specs route to the configured MCP server
    if stage not in {"intake", "approve", "plan"}:
        raise GpuAbstain(f"GPU tier unsupported for stage {stage!r}")

    t0 = time.monotonic()
    if not gpu_available(mcp_url):
        raise GpuAbstain("GPU MCP unavailable or minicpm5 not installed")

    if stage == "intake":
        structured = _run_intake_gpu(
            task_prompt,
            mcp_url=mcp_url,
            min_confidence=min_confidence,
            timeout_sec=timeout_sec,
        )
    elif stage == "approve":
        structured = _run_approve_gpu(
            task_prompt,
            mcp_url=mcp_url,
            min_confidence=min_confidence,
            timeout_sec=timeout_sec,
        )
    else:
        structured = _run_plan_gpu(task_prompt)

    elapsed = round(time.monotonic() - t0, 3)
    return {
        "status": "SUCCESS",
        "structured_output": structured,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        },
        "duration_seconds": elapsed,
    }
