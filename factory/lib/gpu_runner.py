"""Local GPU MCP streamable-HTTP client and abstain-safe stage adapters."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

DEFAULT_GPU_MCP_URL: str | None = None
DEFAULT_GPU_TOOLS: dict[str, str | None] = {
    "decide": "decide",
    "classify": "classify",
    "status": None,
}
_GPU_AVAIL_LOCK = threading.Lock()
_GPU_AVAIL_CACHE: dict[tuple[str, str | None], tuple[float, bool]] = {}

_RISK_LOW_RE = re.compile(r"\bRISK\s*:\s*low\b", re.IGNORECASE)
_RISK_HIGH_MED_RE = re.compile(r"\bRISK\s*:\s*(?:medium|high)\b", re.IGNORECASE)
_ISSUE_HEADER_RE = re.compile(r"(?m)^##\s+Issue\s+#\d+")


class GpuAbstain(RuntimeError):
    """Raised when the local GPU tier cannot handle a stage or lacks confidence."""


def clear_gpu_availability_cache() -> None:
    """Clear cached `gpu_available` results (primarily for tests)."""
    with _GPU_AVAIL_LOCK:
        _GPU_AVAIL_CACHE.clear()


def resolve_gpu_mcp_url(factory_dir: str | None = None, config: dict | None = None) -> str | None:
    """Resolve GPU MCP URL: env FACTORY_GPU_MCP_URL > config['gpu']['mcp_url'] > None."""
    env_url = (os.environ.get("FACTORY_GPU_MCP_URL") or "").strip()
    if env_url:
        return env_url
    cfg = config
    if cfg is None and factory_dir:
        try:
            from factory.lib.config import load_config

            cfg = load_config(factory_dir)
        except Exception:
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
        raw_url = cfg["gpu"].get("mcp_url")
        if isinstance(raw_url, str) and raw_url.strip():
            return raw_url.strip()
    return DEFAULT_GPU_MCP_URL


def resolve_gpu_tools(config: dict | None = None) -> dict[str, str | None]:
    """Return resolved MCP tool names (`decide`, `classify`, optional `status`) from config."""
    out = dict(DEFAULT_GPU_TOOLS)
    gpu_cfg = (config or {}).get("gpu") if isinstance(config, dict) else None
    tools_cfg = gpu_cfg.get("tools") if isinstance(gpu_cfg, dict) else None
    if isinstance(tools_cfg, dict):
        for key in ("decide", "classify"):
            val = tools_cfg.get(key)
            if isinstance(val, str) and val.strip():
                out[key] = val.strip()
        if "status" in tools_cfg:
            sval = tools_cfg.get("status")
            out["status"] = sval.strip() if isinstance(sval, str) and sval.strip() else None
    return out


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

    def ping(self) -> bool:
        """Verify MCP endpoint reachability via initialize handshake."""
        resp = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "software-factory", "version": "1.0"},
            },
            req_id=1,
            timeout=self.connect_timeout,
        )
        if not isinstance(resp, dict) or resp.get("error") is not None:
            return False
        if not isinstance(resp.get("result"), dict):
            return False
        self._rpc("notifications/initialized", None, req_id=None, timeout=self.connect_timeout)
        return True

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


def gpu_available(
    mcp_url: str | None,
    *,
    cache_ttl_sec: float = 60.0,
    force: bool = False,
    status_tool: str | None = None,
) -> bool:
    """Return True if GPU MCP endpoint is reachable (and optional status_tool reports ready)."""
    clean_url = (mcp_url or "").strip()
    if not clean_url:
        return False
    cache_key = (clean_url, status_tool)
    now = time.monotonic()
    if not force:
        with _GPU_AVAIL_LOCK:
            cached = _GPU_AVAIL_CACHE.get(cache_key)
            if cached is not None and (now - cached[0]) < cache_ttl_sec:
                return cached[1]

    available = False
    try:
        client = McpStreamableClient(clean_url, connect_timeout=5.0, call_timeout=10.0)
        if status_tool:
            status = client.call_tool(status_tool, {})
            if isinstance(status, dict) and not status.get("error"):
                if isinstance(status.get("ok"), bool) and not status["ok"]:
                    available = False
                elif isinstance(status.get("ready"), bool):
                    available = status["ready"]
                elif isinstance(status.get("model_catalog"), dict):
                    catalog = status["model_catalog"]
                    available = any(
                        isinstance(v, dict) and v.get("installed") is True
                        for v in catalog.values()
                    )
                elif isinstance(status.get("ok"), bool):
                    available = status["ok"]
                else:
                    available = True
        else:
            available = client.ping()
    except Exception:
        available = False

    with _GPU_AVAIL_LOCK:
        _GPU_AVAIL_CACHE[cache_key] = (time.monotonic(), available)
    return available


def decide(
    mcp_url: str,
    state: str,
    decisions: list[dict],
    *,
    timeout_sec: float = 60.0,
    tool_name: str = "decide",
) -> dict:
    """Call configured MCP decide tool."""
    return McpStreamableClient(mcp_url, connect_timeout=5.0, call_timeout=timeout_sec).call_tool(
        tool_name or "decide",
        {"state": state, "decisions": decisions},
    )


def classify(
    mcp_url: str,
    items: list[dict],
    allowed_categories: list[str],
    *,
    timeout_sec: float = 60.0,
    tool_name: str = "classify",
) -> dict:
    """Call configured MCP classify tool."""
    return McpStreamableClient(mcp_url, connect_timeout=5.0, call_timeout=timeout_sec).call_tool(
        tool_name or "classify",
        {"items": items, "allowed_categories": allowed_categories},
    )


def _extract_decision_map(resp: dict) -> dict[str, dict]:
    """Normalize decision payload from MCP decide tool into {id: entry}."""
    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else resp
        raise GpuAbstain(f"GPU decide returned error: {err}")
    raw = resp.get("decisions")
    if not isinstance(raw, dict):
        raise GpuAbstain("GPU decide response missing 'decisions' dict")
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _get_bool_prob(decisions_map: dict[str, dict], decision_id: str, default: float = 0.0) -> float:
    entry = decisions_map.get(decision_id)
    if not isinstance(entry, dict):
        return default
    val = entry.get("true_probability")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    dist = entry.get("distribution")
    if isinstance(dist, dict):
        for tkey in ("True", "true"):
            dval = dist.get(tkey)
            if isinstance(dval, (int, float)) and not isinstance(dval, bool):
                return float(dval)
    return default


def _get_choice_argmax(
    decisions_map: dict[str, dict],
    decision_id: str,
    options: tuple[str, ...] = ("low", "medium", "high"),
) -> str:
    entry = decisions_map.get(decision_id)
    if not isinstance(entry, dict):
        return "medium"
    dist = entry.get("distribution")
    if isinstance(dist, dict):
        best_opt = None
        best_val = -1.0
        for opt in options:
            val = dist.get(opt)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if float(val) > best_val:
                    best_val = float(val)
                    best_opt = opt
        if best_opt is not None:
            return best_opt
    val = entry.get("decision")
    if isinstance(val, str) and val.lower() in options:
        return val.lower()
    return "medium"


def _run_intake_gpu(
    task_prompt: str,
    *,
    mcp_url: str,
    min_confidence: float,
    timeout_sec: float,
    decide_tool: str = "decide",
) -> dict:
    try:
        resp = decide(
            mcp_url,
            task_prompt,
            decisions=[
                {"id": "accept", "type": "bool", "question": "Is this issue clear, actionable, and in-scope to ACCEPT?"},
                {"id": "risk", "type": "choice", "options": ["low", "medium", "high"], "question": "What is the implementation risk?"},
                {"id": "touches_ui", "type": "bool", "question": "Does this issue touch UI/frontend?"},
                {"id": "touches_security", "type": "bool", "question": "Does this issue touch auth, security, or secrets?"},
            ],
            timeout_sec=timeout_sec,
            tool_name=decide_tool,
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


def _check_approve_context(gpu_context: dict | None) -> None:
    if not isinstance(gpu_context, dict):
        raise GpuAbstain("GPU approve abstains: missing gpu_context")
    if gpu_context.get("risk") != "low":
        raise GpuAbstain(f"GPU approve abstains: risk={gpu_context.get('risk')!r} != 'low'")
    if not gpu_context.get("gate_passed"):
        raise GpuAbstain("GPU approve abstains: gate did not pass")
    if not gpu_context.get("all_reviews_passed"):
        raise GpuAbstain("GPU approve abstains: reviews did not all pass")
    if gpu_context.get("prior_escalation"):
        raise GpuAbstain("GPU approve abstains: prior escalation present")
    if gpu_context.get("round") != 1:
        raise GpuAbstain(f"GPU approve abstains: round={gpu_context.get('round')!r} != 1")


def _run_approve_gpu(
    task_prompt: str,
    *,
    mcp_url: str,
    min_confidence: float,
    timeout_sec: float,
    gpu_context: dict | None = None,
    decide_tool: str = "decide",
) -> dict:
    _check_approve_context(gpu_context)
    try:
        resp = decide(
            mcp_url,
            task_prompt,
            decisions=[
                {"id": "safe_to_merge", "type": "bool", "question": "Is this PR safe to merge?"},
                {"id": "merge_danger", "type": "bool", "question": "Is there any merge danger or unresolved risk?"},
            ],
            timeout_sec=timeout_sec,
            tool_name=decide_tool,
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
    mcp_url: str | None = None,
    min_confidence: float = 0.90,
    timeout_sec: float = 60.0,
    gpu_context: dict | None = None,
    tools: dict[str, str | None] | None = None,
) -> dict:
    """Execute a supported stage on the local GPU MCP tier, or raise GpuAbstain."""
    del model  # currently all gpu:* specs route to the configured MCP server
    clean_url = (mcp_url or "").strip()
    if not clean_url:
        raise GpuAbstain("GPU decision MCP disabled (gpu.mcp_url not set)")
    if stage not in {"intake", "approve", "plan"}:
        raise GpuAbstain(f"GPU tier unsupported for stage {stage!r}")

    resolved_tools = resolve_gpu_tools({"gpu": {"tools": tools}} if isinstance(tools, dict) else None)
    decide_tool = str(resolved_tools.get("decide") or "decide")
    status_tool = resolved_tools.get("status")

    t0 = time.monotonic()
    if not gpu_available(clean_url, status_tool=status_tool):
        raise GpuAbstain("GPU decision MCP unavailable")

    if stage == "intake":
        structured = _run_intake_gpu(
            task_prompt,
            mcp_url=clean_url,
            min_confidence=min_confidence,
            timeout_sec=timeout_sec,
            decide_tool=decide_tool,
        )
    elif stage == "approve":
        structured = _run_approve_gpu(
            task_prompt,
            mcp_url=clean_url,
            min_confidence=min_confidence,
            timeout_sec=timeout_sec,
            gpu_context=gpu_context,
            decide_tool=decide_tool,
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
