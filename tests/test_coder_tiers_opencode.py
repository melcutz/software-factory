import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from factory.lib.agy_runner import (
    AgentExecutionError,
    QuotaExhaustedError,
    classify_model_provider,
    run_agent,
)
from factory.lib.opencode_runner import (
    detect_opencode_error,
    extract_json_from_text,
    parse_opencode_jsonl,
    resolve_opencode_bin,
    route_free_models,
)
from factory.lib.schemas import SCHEMAS


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


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_classify_model_provider():
    assert classify_model_provider("gemini-4-hydrogen-medium") == ("agy", "standard")
    assert classify_model_provider("opencode/deepseek-v4.1-flash-free") == ("opencode", "free")
    assert classify_model_provider("opencode/big-pickle") == ("opencode", "free")
    assert classify_model_provider("opencode/custom-model") == ("opencode", "standard")
    assert classify_model_provider("opencode-go/glm-5.3") == ("opencode-go", "standard")
    assert classify_model_provider("openrouter/cohere/north-mini-code:free") == ("openrouter", "free")
    assert classify_model_provider("openrouter/anthropic/claude") == ("openrouter", "standard")
    assert classify_model_provider("gpu:decision-model") == ("gpu", "local")


def test_dispatcher_routes_prefixes_and_tags_usage(tmp_path: Path, monkeypatch):
    wt = _init_git_wt(tmp_path)
    bin_dir = tmp_path / "bin"
    calls_log = tmp_path / "calls.jsonl"

    _write_exe(
        bin_dir / "opencode",
        f"""#!/usr/bin/env python3
import json, os, sys
with open({str(calls_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"bin": "opencode", "argv": sys.argv[1:]}}) + "\\n")
payload = json.dumps({{"decision": "APPROVE", "summary": ["ok"], "reasoning": "safe", "merge_danger": "low", "question": ""}})
print(json.dumps({{"type": "step_start", "sessionID": "ses_oc1", "part": {{"type": "step-start"}}}}))
print(json.dumps({{"type": "text", "sessionID": "ses_oc1", "part": {{"type": "text", "text": payload}}}}))
print(json.dumps({{"type": "step_finish", "sessionID": "ses_oc1", "part": {{"type": "step-finish", "cost": 0, "tokens": {{"input": 100, "output": 20, "reasoning": 5, "cache": {{"read": 10, "write": 0}}}}}}}}))
""",
    )
    _write_exe(
        bin_dir / "agy",
        f"""#!/usr/bin/env python3
import json, sys
with open({str(calls_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"bin": "agy", "argv": sys.argv[1:]}}) + "\\n")
print(json.dumps({{"status": "SUCCESS", "duration_seconds": 1.2, "usage": {{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}, "structured_output": {{"decision": "APPROVE", "summary": ["ok"], "reasoning": "safe", "merge_danger": "low", "question": ""}}}}))
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(bin_dir / "opencode"))

    cases = [
        ("opencode/deepseek-v4.1-flash-free", "opencode", "free"),
        ("opencode-go/glm-5.3", "opencode-go", "standard"),
        ("openrouter/cohere/north-mini-code:free", "openrouter", "free"),
        ("gemini-4-hydrogen-medium", "agy", "standard"),
    ]
    for idx, (model, exp_provider, exp_tier) in enumerate(cases):
        out, usage, dur = run_agent(
            os.path.abspath("factory"),
            str(tmp_path / f"job-{idx}"),
            str(wt),
            "approve",
            "approver",
            model,
            "Check PR",
            SCHEMAS["approve"],
            30,
            sandbox=True,
            log_name=f"r-{idx}",
        )
        assert out["decision"] == "APPROVE"
        assert usage["provider"] == exp_provider
        assert usage["tier"] == exp_tier
        assert usage["model"] == model
        assert dur >= 0.0

    recorded = [json.loads(ln) for ln in calls_log.read_text(encoding="utf-8").splitlines()]
    assert [r["bin"] for r in recorded] == ["opencode", "opencode", "opencode", "agy"]
    assert recorded[0]["argv"][:4] == ["run", "-m", "opencode/deepseek-v4.1-flash-free", "--format"]


def test_resolve_opencode_bin_path_and_home_fallbacks(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FACTORY_OPENCODE_BIN", raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))

    # Fallback when nothing exists
    assert resolve_opencode_bin() == "opencode"

    # Unconfigured ~/.opencode/bin/opencode is NOT implicitly searched
    oc_home = _write_exe(fake_home / ".opencode" / "bin" / "opencode", "#!/bin/sh\nexit 0\n")
    assert resolve_opencode_bin() == "opencode"

    # path_prepend enables finding ~/.opencode/bin/opencode
    cfg_prepend = {"path_prepend": [str(fake_home / ".opencode" / "bin")]}
    assert resolve_opencode_bin(config=cfg_prepend) == str(oc_home)

    # PATH finds opencode without config
    path_dir = tmp_path / "path_bin"
    path_oc = _write_exe(path_dir / "opencode", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", str(path_dir))
    assert resolve_opencode_bin() == str(path_oc)

    # providers.opencode.bin overrides PATH and path_prepend
    explicit_oc = _write_exe(tmp_path / "custom" / "opencode", "#!/bin/sh\nexit 0\n")
    cfg_bin = {
        "path_prepend": [str(fake_home / ".opencode" / "bin")],
        "providers": {"opencode": {"bin": str(explicit_oc)}},
    }
    assert resolve_opencode_bin(config=cfg_bin) == str(explicit_oc)

    # FACTORY_OPENCODE_BIN overrides everything
    env_oc = _write_exe(tmp_path / "env_bin" / "opencode", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(env_oc))
    assert resolve_opencode_bin(config=cfg_bin) == str(env_oc)


def test_opencode_jsonl_parsing_and_usage_summing():
    sample = "\n".join(
        [
            "non-json noise line",
            json.dumps(
                {
                    "type": "step_start",
                    "sessionID": "ses_123",
                    "part": {"type": "step-start"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_123",
                    "part": {"type": "text", "text": "Here is the JSON:\n"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_123",
                    "part": {"type": "text", "text": '{"ok": true}'},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_123",
                    "part": {
                        "type": "step-finish",
                        "cost": 0.001,
                        "tokens": {"input": 100, "output": 10, "reasoning": 5, "cache": {"read": 20, "write": 0}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_123",
                    "part": {
                        "type": "step-finish",
                        "cost": 0.002,
                        "tokens": {"input": 50, "output": 5, "reasoning": 0, "cache": {"read": 10, "write": 0}},
                    },
                }
            ),
        ]
    )
    text, sid, usage = parse_opencode_jsonl(sample)
    assert sid == "ses_123"
    assert text == 'Here is the JSON:\n{"ok": true}'
    assert usage["input_tokens"] == 150
    assert usage["output_tokens"] == 15
    assert usage["thinking_tokens"] == 5
    assert usage["cache_read_tokens"] == 30
    assert usage["total_tokens"] == 200
    assert usage["cost_usd"] == pytest.approx(0.003)


def test_opencode_extracts_fenced_and_trailing_json():
    fenced = 'Sure!\n```json\n{"a": 1}\n```\nDone.'
    assert extract_json_from_text(fenced) == {"a": 1}

    trailing = 'Thinking about {"ignored": 0} first, final answer: {"a": 2, "b": "ok"} trailing text'
    assert extract_json_from_text(trailing) == {"a": 2, "b": "ok"}

    with pytest.raises(ValueError):
        extract_json_from_text("no json here")


def test_opencode_validation_failure_reasks_once_in_session_then_succeeds(tmp_path: Path, monkeypatch):
    wt = _init_git_wt(tmp_path)
    bin_dir = tmp_path / "bin"
    calls_file = tmp_path / "reask_calls.json"

    _write_exe(
        bin_dir / "opencode",
        f"""#!/usr/bin/env python3
import json, os, sys
calls = json.loads(open({str(calls_file)!r}).read()) if os.path.exists({str(calls_file)!r}) else []
calls.append(sys.argv[1:])
open({str(calls_file)!r}, "w").write(json.dumps(calls))
if len(calls) == 1:
    # Missing required fields -> fails validate_stage_output
    text = json.dumps({{"decision": "APPROVE"}})
    tokens = {{"input": 100, "output": 10, "reasoning": 0, "cache": {{"read": 0, "write": 0}}}}
else:
    assert "--session" in sys.argv
    idx = sys.argv.index("--session")
    assert sys.argv[idx + 1] == "ses_reask_1"
    text = json.dumps({{"decision": "APPROVE", "summary": ["ok"], "reasoning": "fixed", "merge_danger": "low", "question": ""}})
    tokens = {{"input": 40, "output": 20, "reasoning": 0, "cache": {{"read": 5, "write": 0}}}}
print(json.dumps({{"type": "text", "sessionID": "ses_reask_1", "part": {{"type": "text", "text": text}}}}))
print(json.dumps({{"type": "step_finish", "sessionID": "ses_reask_1", "part": {{"type": "step-finish", "cost": 0, "tokens": tokens}}}}))
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(bin_dir / "opencode"))

    out, usage, _ = run_agent(
        os.path.abspath("factory"),
        str(tmp_path / "job-reask"),
        str(wt),
        "approve",
        "approver",
        "opencode/deepseek-v4.1-flash-free",
        "Check PR",
        SCHEMAS["approve"],
        30,
        sandbox=True,
        log_name="reask",
    )
    assert out["decision"] == "APPROVE"
    assert usage["input_tokens"] == 140
    assert usage["output_tokens"] == 30
    assert usage["cache_read_tokens"] == 5
    assert usage["total_tokens"] == 175
    recorded = json.loads(calls_file.read_text(encoding="utf-8"))
    assert len(recorded) == 2


def test_opencode_validation_failure_after_reask_raises_agent_error(tmp_path: Path, monkeypatch):
    wt = _init_git_wt(tmp_path)
    bin_dir = tmp_path / "bin"
    counter = tmp_path / "count"

    _write_exe(
        bin_dir / "opencode",
        f"""#!/usr/bin/env python3
import json, os
n = int(open({str(counter)!r}).read()) + 1 if os.path.exists({str(counter)!r}) else 1
open({str(counter)!r}, "w").write(str(n))
print(json.dumps({{"type": "text", "sessionID": "ses_bad", "part": {{"type": "text", "text": "not json"}}}}))
print(json.dumps({{"type": "step_finish", "sessionID": "ses_bad", "part": {{"type": "step-finish", "cost": 0, "tokens": {{"input": 10, "output": 2, "reasoning": 0, "cache": {{"read": 0}}}}}}}}))
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(bin_dir / "opencode"))

    with pytest.raises(AgentExecutionError):
        run_agent(
            os.path.abspath("factory"),
            str(tmp_path / "job-fail"),
            str(wt),
            "approve",
            "approver",
            "opencode/deepseek-v4.1-flash-free",
            "Check PR",
            SCHEMAS["approve"],
            30,
            sandbox=True,
        )
    assert int(counter.read_text(encoding="utf-8")) == 2


def test_opencode_quota_mapping_free_openrouter_go_and_retry_after():
    # Default cooldown is 3600 when unconfigured and no retry-after header is present
    err_zen = detect_opencode_error(
        "opencode/deepseek-v4.1-flash-free", "", "Error: Free usage limit reached for today"
    )
    assert isinstance(err_zen, QuotaExhaustedError)
    assert err_zen.bucket == "opencode-free"
    assert err_zen.retry_after_sec == 3600

    # Explicit retry-after in provider output takes precedence
    err_retry = detect_opencode_error(
        "openrouter/cohere/north-mini-code:free", "", "429 free-models-per-day exceeded; Retry after 120s"
    )
    assert isinstance(err_retry, QuotaExhaustedError)
    assert err_retry.bucket == "openrouter-free"
    assert err_retry.retry_after_sec == 120

    # Configured bucket cooldowns override default when provider omits retry-after
    custom_cfg = {
        "quota": {
            "default_cooldown_sec": 900,
            "buckets": [
                {"match": "openrouter/*:free", "bucket": "openrouter-free", "cooldown_sec": 600},
                {"match": "opencode-go/*", "bucket": "opencode-go", "cooldown_sec": 1200},
            ],
        }
    }
    err_or = detect_opencode_error(
        "openrouter/cohere/north-mini-code:free", "", "429 free-models-per-day exceeded", config=custom_cfg
    )
    assert isinstance(err_or, QuotaExhaustedError)
    assert err_or.bucket == "openrouter-free"
    assert err_or.retry_after_sec == 600

    err_go = detect_opencode_error(
        "opencode-go/glm-5.3", "", "Weekly limit reached for plan", config=custom_cfg
    )
    assert isinstance(err_go, QuotaExhaustedError)
    assert err_go.bucket == "opencode-go"
    assert err_go.retry_after_sec == 1200


def test_opencode_broken_model_raises_agent_execution_error():
    for msg in (
        "ProviderModelNotFoundError: model not found",
        "This model is deprecated",
        "Access requires Global regions",
    ):
        err = detect_opencode_error("opencode/iceman-free", "", msg)
        assert isinstance(err, AgentExecutionError)


def test_detect_opencode_error_ignores_model_text_and_tool_output():
    noisy = "quota exceeded, usage limit reached, rate limit 429, deprecated, model not found"
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "step_start",
                    "sessionID": "ses_ok",
                    "part": {"type": "step-start"},
                }
            ),
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "ses_ok",
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "state": {"status": "completed", "output": noisy},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "reasoning",
                    "sessionID": "ses_ok",
                    "part": {"type": "reasoning", "text": noisy},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_ok",
                    "part": {"type": "text", "text": noisy},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_ok",
                    "part": {
                        "type": "step-finish",
                        "reason": "stop",
                        "cost": 0,
                        "tokens": {"input": 100, "output": 20, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    },
                }
            ),
        ]
    )
    for model_spec in (
        "opencode-go/minimax-m3",
        "opencode/x-free",
        "openrouter/x:free",
    ):
        assert detect_opencode_error(model_spec, stdout, "") is None


def test_detect_opencode_error_catches_error_json_event_and_stderr():
    err_stdout = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_err",
            "error": {"name": "APIError", "data": {"message": "usage limit reached"}},
        }
    )
    err_go = detect_opencode_error("opencode-go/minimax-m3", err_stdout, "")
    assert isinstance(err_go, QuotaExhaustedError)
    assert err_go.bucket == "opencode-go"
    assert err_go.retry_after_sec == 3600

    err_free = detect_opencode_error("opencode/x-free", "", "Free usage limit")
    assert isinstance(err_free, QuotaExhaustedError)
    assert err_free.bucket == "opencode-free"
    assert err_free.retry_after_sec == 3600


def test_route_free_models_all_task_kinds():
    for kind in ("lint", "ui", "longctx", "tests", "plan", "default", "unknown-kind"):
        models = route_free_models(kind)
        assert len(models) >= 5
        assert models[0].startswith("openrouter/") and models[0].endswith(":free")
        assert any(m.startswith("opencode/") for m in models)
        assert all(m.endswith(":free") or m.endswith("-free") or m == "opencode/big-pickle" for m in models)


def test_pipeline_default_config_chains_never_executes_real_opencode(tmp_path: Path, monkeypatch):
    from factory.lib.config import load_config
    from factory.lib.pipeline import _call_run_agent

    cfg = load_config(os.path.abspath("factory"))
    builder_low = ["opencode/deepseek-v4.1-flash-free", "gemini-3.8-flash-medium"]

    # conftest.py must have overridden FACTORY_OPENCODE_BIN to a hermetic fake under tmp
    oc_bin = resolve_opencode_bin()
    assert oc_bin != os.path.expanduser("~/.opencode/bin/opencode")
    assert "_hermetic_bins" in oc_bin

    wt = _init_git_wt(tmp_path)
    bin_dir = tmp_path / "bin"
    _write_exe(
        bin_dir / "agy",
        """#!/usr/bin/env python3
import json
print(json.dumps({
    "status": "SUCCESS",
    "duration_seconds": 0.5,
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    "structured_output": {
        "verdict": "DONE",
        "summary": ["ok"],
        "changes": [],
        "tests_added": [],
        "blocked_reason": "",
        "notes": ""
    }
}))
""",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    out, usage, _ = _call_run_agent(
        os.path.abspath("factory"),
        str(tmp_path / "job-hermetic"),
        str(wt),
        "build",
        "builder",
        builder_low,
        "Implement feature",
        SCHEMAS["build"],
        30,
        config=cfg,
        sandbox=False,
    )
    assert out["verdict"] == "DONE"
    assert usage["provider"] == "agy"
    assert usage["model"] == builder_low[1]
