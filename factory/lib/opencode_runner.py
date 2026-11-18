"""OpenCode CLI runner (`opencode run --format json --auto`) with JSONL parsing and session re-ask."""
from __future__ import annotations

from collections.abc import Callable
import json
import os
import re
import shutil
import time

from factory.lib.agy_runner import (
    _SUBPROCESS_TIMEOUT_GRACE_SEC,
    AgentExecutionError,
    QuotaExhaustedError,
    _invoke_agy_process,
)
from factory.lib.schemas import validate_stage_output

_FREE_ROUTES: dict[str, list[str]] = {
    "lint": [
        "openrouter/cohere/north-mini-code:free",
        "openrouter/nvidia/nemotron-3.5-super-vl:free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/mimo-v2.6-flash-free",
        "opencode/big-pickle",
    ],
    "ui": [
        "openrouter/nvidia/nemotron-3.5-super-vl:free",
        "openrouter/google/gemma-4-31b-it:free",
        "opencode/nemotron-3.5-super-vl-free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/inkling-2-small-free",
    ],
    "longctx": [
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter/thinkingmachines/inkling-2-small:free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/longcat-2.5-free",
        "opencode/hy4-flash-free",
    ],
    "tests": [
        "openrouter/cohere/north-mini-code:free",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/nemotron-3.5-super-vl-free",
        "opencode/flint-40b-free",
    ],
    "plan": [
        "openrouter/thinkingmachines/inkling-2-small:free",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/hy4-flash-free",
        "opencode/iceman-free",
    ],
    "default": [
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter/thinkingmachines/inkling-2-small:free",
        "openrouter/cohere/north-mini-code:free",
        "opencode/deepseek-v4.1-flash-free",
        "opencode/mimo-v2.6-flash-free",
        "opencode/beam-free",
    ],
}

_OPENROUTER_LIMIT_RE = re.compile(
    r"(?:rate[ _-]?limit|\b429\b|free-models-per|limit exceeded|insufficient credits|quota)",
    re.IGNORECASE,
)
_ZEN_FREE_LIMIT_RE = re.compile(r"Free usage limit", re.IGNORECASE)
_ZEN_RATE_LIMIT_RE = re.compile(r"(?:free-models-per|rate[ _-]?limit|\b429\b|quota)", re.IGNORECASE)
_GO_LIMIT_RE = re.compile(r"(?:usage limit|weekly limit|limit reached|quota)", re.IGNORECASE)
_BROKEN_MODEL_RE = re.compile(
    r"(?:deprecated|ProviderModelNotFound|model not found|requires Global regions)",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def resolve_opencode_bin() -> str:
    """Return FACTORY_OPENCODE_BIN if set, else `opencode` on PATH, ~/.opencode/bin/opencode, or ~/.local/bin/opencode."""
    env_bin = (os.environ.get("FACTORY_OPENCODE_BIN") or "").strip()
    if env_bin:
        return env_bin
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.opencode/bin/opencode"),
        os.path.expanduser("~/.local/bin/opencode"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "opencode"


def route_free_models(task_kind: str = "default") -> list[str]:
    """Return ordered free model fallback list (OpenRouter :free then Zen free) for task_kind."""
    return list(_FREE_ROUTES.get(task_kind, _FREE_ROUTES["default"]))


def build_opencode_prompt(full_prompt: str, schema: dict) -> str:
    """Append JSON schema and strict single-JSON-object instructions to prompt."""
    schema_str = json.dumps(schema, indent=2)
    return (
        f"{full_prompt}\n\n"
        f"## Output Schema\n\n"
        f"```json\n{schema_str}\n```\n\n"
        "Reply with ONLY one JSON object matching this schema."
    )


def parse_opencode_jsonl(stdout_text: str) -> tuple[str, str | None, dict]:
    """Parse `opencode run --format json` JSON-lines output into (text, session_id, usage)."""
    text_parts: list[str] = []
    session_id: str | None = None
    input_tokens = 0
    output_tokens = 0
    thinking_tokens = 0
    cache_read_tokens = 0
    cost_usd = 0.0

    for raw_line in (stdout_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue

        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if session_id is None:
            sid = ev.get("sessionID") or part.get("sessionID") or ev.get("sessionId")
            if isinstance(sid, str) and sid.strip():
                session_id = sid.strip()

        ev_type = ev.get("type")
        part_type = part.get("type")
        if ev_type == "text" or part_type == "text":
            chunk = part.get("text") if "text" in part else ev.get("text", "")
            if isinstance(chunk, str):
                text_parts.append(chunk)
        elif ev_type == "step_finish" or part_type == "step-finish":
            cost_val = part.get("cost", ev.get("cost", 0))
            if isinstance(cost_val, (int, float)) and not isinstance(cost_val, bool):
                cost_usd += float(cost_val)
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            if isinstance(tokens.get("input"), int):
                input_tokens += tokens["input"]
            if isinstance(tokens.get("output"), int):
                output_tokens += tokens["output"]
            if isinstance(tokens.get("reasoning"), int):
                thinking_tokens += tokens["reasoning"]
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            if isinstance(cache.get("read"), int):
                cache_read_tokens += cache["read"]

    total_tokens = input_tokens + output_tokens + thinking_tokens + cache_read_tokens
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "cache_read_tokens": cache_read_tokens,
        "total_tokens": total_tokens,
        "cost_usd": round(cost_usd, 6),
    }
    return "".join(text_parts), session_id, usage


def extract_json_from_text(text: str) -> dict:
    """Extract the last valid JSON object from model text, handling ```json fences."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty response text from opencode")

    # 1. Check fenced code blocks from last to first
    fences = _FENCE_RE.findall(raw)
    for block in reversed(fences):
        candidate = block.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 2. Try whole text directly
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 3. Scan backwards from the last '{' with raw_decode
    decoder = json.JSONDecoder()
    pos = len(raw)
    while True:
        idx = raw.rfind("{", 0, pos)
        if idx < 0:
            break
        pos = idx
        try:
            parsed, _ = decoder.raw_decode(raw, idx)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    raise ValueError(f"Could not extract JSON object from opencode text: {raw[:300]!r}")


def detect_opencode_error(model_spec: str, stdout_text: str, stderr_text: str) -> Exception | None:
    """Map opencode quota/limit/broken-model output to QuotaExhaustedError or AgentExecutionError."""
    combined = f"{stderr_text}\n{stdout_text}".strip()
    if not combined:
        return None
    snippet = combined[:400]

    if model_spec.startswith("openrouter/") and _OPENROUTER_LIMIT_RE.search(combined):
        return QuotaExhaustedError(
            model_spec,
            retry_after_sec=10800,
            message=snippet,
            bucket="openrouter-free",
        )
    if _ZEN_FREE_LIMIT_RE.search(combined):
        return QuotaExhaustedError(
            model_spec,
            retry_after_sec=10800,
            message=snippet,
            bucket="opencode-free",
        )
    if (
        model_spec.startswith("opencode/")
        and (model_spec.endswith("-free") or model_spec == "opencode/big-pickle")
        and _ZEN_RATE_LIMIT_RE.search(combined)
    ):
        return QuotaExhaustedError(
            model_spec,
            retry_after_sec=10800,
            message=snippet,
            bucket="opencode-free",
        )
    if model_spec.startswith("opencode-go/") and _GO_LIMIT_RE.search(combined):
        return QuotaExhaustedError(
            model_spec,
            retry_after_sec=86400,
            message=snippet,
            bucket="opencode-go",
        )
    if _BROKEN_MODEL_RE.search(combined):
        return AgentExecutionError(f"opencode model unavailable ({model_spec}): {snippet}")
    return None


def _merge_usage(u1: dict, u2: dict) -> dict:
    return {
        "input_tokens": int(u1.get("input_tokens", 0)) + int(u2.get("input_tokens", 0)),
        "output_tokens": int(u1.get("output_tokens", 0)) + int(u2.get("output_tokens", 0)),
        "thinking_tokens": int(u1.get("thinking_tokens", 0)) + int(u2.get("thinking_tokens", 0)),
        "cache_read_tokens": int(u1.get("cache_read_tokens", 0)) + int(u2.get("cache_read_tokens", 0)),
        "total_tokens": int(u1.get("total_tokens", 0)) + int(u2.get("total_tokens", 0)),
        "cost_usd": round(float(u1.get("cost_usd", 0.0)) + float(u2.get("cost_usd", 0.0)), 6),
    }


def _run_opencode_cmd(
    cmd: list[str],
    *,
    worktree: str,
    model: str,
    timeout_sec: int,
    extra_env: dict[str, str] | None,
    idle_grace_sec: float,
    abort_check: Callable[[], BaseException | None] | None,
) -> tuple[str, str | None, dict, float]:
    t0 = time.monotonic()
    returncode, stdout_text, stderr_text, killed_reason = _invoke_agy_process(
        cmd,
        cwd=worktree,
        env=extra_env,
        hard_timeout_sec=float(timeout_sec + _SUBPROCESS_TIMEOUT_GRACE_SEC),
        idle_bg_grace_sec=idle_grace_sec,
        abort_check=abort_check,
        label="opencode",
        idle_output_timeout_sec=idle_grace_sec,
    )
    elapsed = round(time.monotonic() - t0, 3)

    if killed_reason:
        raise AgentExecutionError(f"{killed_reason}: {(stderr_text or stdout_text)[:400]}")

    mapped_err = detect_opencode_error(model, stdout_text, stderr_text)
    if mapped_err is not None:
        raise mapped_err

    if returncode != 0:
        raise AgentExecutionError(
            f"opencode exited with code {returncode}: {(stderr_text or stdout_text)[:400]}"
        )

    text, session_id, usage = parse_opencode_jsonl(stdout_text)
    return text, session_id, usage, elapsed


def run_opencode_turn(
    worktree: str,
    stage: str,
    model: str,
    full_prompt: str,
    schema: dict,
    timeout_sec: int,
    *,
    extra_env: dict[str, str] | None = None,
    idle_grace_sec: float = 600.0,
    abort_check: Callable[[], BaseException | None] | None = None,
) -> dict:
    """Run one opencode turn with JSONL parsing and at most 1 in-session validation re-ask."""
    oc_bin = resolve_opencode_bin()
    prompt = build_opencode_prompt(full_prompt, schema)
    cmd = [oc_bin, "run", "-m", model, "--format", "json", "--auto", prompt]

    text, session_id, usage, duration = _run_opencode_cmd(
        cmd,
        worktree=worktree,
        model=model,
        timeout_sec=timeout_sec,
        extra_env=extra_env,
        idle_grace_sec=idle_grace_sec,
        abort_check=abort_check,
    )

    err_msg = ""
    try:
        parsed = extract_json_from_text(text)
        validate_stage_output(stage, parsed)
        return {
            "status": "SUCCESS",
            "structured_output": parsed,
            "usage": usage,
            "duration_seconds": duration,
        }
    except (ValueError, json.JSONDecodeError) as first_err:
        err_msg = str(first_err)
        if not session_id:
            raise AgentExecutionError(
                f"opencode ({model}) failed validation with no sessionID for re-ask: {err_msg}"
            ) from first_err

    reask_prompt = (
        f"Validation error: {err_msg}. "
        "Reply with only the corrected JSON object matching the schema."
    )
    reask_cmd = [
        oc_bin,
        "run",
        "-m",
        model,
        "--format",
        "json",
        "--auto",
        "--session",
        session_id,
        reask_prompt,
    ]
    text2, _, usage2, duration2 = _run_opencode_cmd(
        reask_cmd,
        worktree=worktree,
        model=model,
        timeout_sec=timeout_sec,
        extra_env=extra_env,
        idle_grace_sec=idle_grace_sec,
        abort_check=abort_check,
    )
    total_usage = _merge_usage(usage, usage2)
    total_duration = round(duration + duration2, 3)

    try:
        parsed2 = extract_json_from_text(text2)
        validate_stage_output(stage, parsed2)
    except (ValueError, json.JSONDecodeError) as second_err:
        raise AgentExecutionError(
            f"opencode ({model}) failed validation after session re-ask: {second_err}"
        ) from second_err

    return {
        "status": "SUCCESS",
        "structured_output": parsed2,
        "usage": total_usage,
        "duration_seconds": total_duration,
    }
