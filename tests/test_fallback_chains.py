from __future__ import annotations

import copy
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from factory.lib.agy_runner import AgentExecutionError, QuotaExhaustedError, run_agent_chain
from factory.lib.batch import BatchSpec, TriageItem, _record_batch_usage, _run_batch_review, triage_and_plan_queue
from factory.lib.config import DEFAULT_CONFIG, model_chain, resolve_reviewers
from factory.lib.git_state import LeaseLostError
from factory.lib.gpu_runner import GpuAbstain
from factory.lib.pipeline import JobHaltedError, _call_run_agent, _record_and_format_step, select_builder_chain, select_builder_model
from factory.lib.state import enqueue_job, load_job


class _FakeStateStore:
    def __init__(self, initial_models: dict | None = None) -> None:
        self.quota_state: dict = {"models": dict(initial_models or {})}
        self.recorded: list[tuple[str, str, int | None]] = []
        self.cleared: list[str] = []

    def get_quota_state(self) -> dict:
        return self.quota_state

    def record_quota_cooldown(self, model_name: str, reason: str, retry_after_sec: int | None = None) -> dict:
        from factory.lib.governor import quota_bucket_for_model

        bucket = quota_bucket_for_model(model_name) or model_name
        cd = int(retry_after_sec) if retry_after_sec and retry_after_sec > 0 else 300
        self.recorded.append((bucket, reason, retry_after_sec))
        self.quota_state.setdefault("models", {})[bucket] = {
            "cooldown_until_ts": time.time() + cd,
            "reason": reason,
        }
        return self.quota_state

    def clear_quota_cooldown(self, model_name: str) -> dict:
        from factory.lib.governor import quota_bucket_for_model

        bucket = quota_bucket_for_model(model_name) or model_name
        self.cleared.append(bucket)
        (self.quota_state.get("models") or {}).pop(bucket, None)
        return self.quota_state


class FallbackChainsTest(unittest.TestCase):
    def test_chain_falls_through_gpu_abstain_quota_cooldown_and_agent_error(self) -> None:
        calls: list[tuple[str, int]] = []
        logs: list[str] = []
        store = _FakeStateStore(
            {
                "openrouter-free": {
                    "cooldown_until_ts": time.time() + 600,
                    "reason": "prior 429",
                }
            }
        )

        def fake_runner(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
            calls.append((model_spec, timeout_sec))
            if model_spec.startswith("gpu:"):
                raise GpuAbstain("confidence 0.42 < 0.70")
            if model_spec == "opencode/deepseek-v4.1-flash-free":
                raise QuotaExhaustedError(model_spec, retry_after_sec=120, message="Zen free daily cap")
            if model_spec == "opencode-go/glm-5.1":
                raise AgentExecutionError("Malformed JSON output")
            if model_spec == "gemini-4-hydrogen-low":
                return {"decision": "ACCEPT"}, {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, 1.5
            raise AssertionError(f"Unexpected call to {model_spec}")

        chain = [
            "gpu:qwen2.5-coder:7b",
            "openrouter/qwen/qwen3-coder:free",  # already in cooldown -> skipped without calling
            "opencode/deepseek-v4.1-flash-free",  # raises QuotaExhaustedError -> records opencode-free cooldown
            "opencode/mimo-v2.6-flash-free",  # shares opencode-free bucket -> skipped without calling!
            "opencode-go/glm-5.1",  # raises AgentExecutionError -> falls through
            "gemini-4-hydrogen-low",  # succeeds!
        ]
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        out, usage, dur = run_agent_chain(
            "/tmp/factory",
            "/tmp/job",
            "/tmp/wt",
            "intake",
            "intake",
            chain,
            "triage issue",
            {},
            900,
            config=cfg,
            state_store=store,
            runner_fn=fake_runner,
            log_fn=logs.append,
        )

        self.assertEqual(out, {"decision": "ACCEPT"})
        self.assertEqual(dur, 1.5)
        self.assertEqual(usage["model"], "gemini-4-hydrogen-low")
        self.assertEqual(usage["provider"], "agy")
        self.assertEqual(usage["tier"], "paid")
        # Verify openrouter/qwen/qwen3-coder:free and opencode/mimo-v2.6-flash-free were skipped without calling
        self.assertEqual(
            calls,
            [
                ("gpu:qwen2.5-coder:7b", 60),
                ("opencode/deepseek-v4.1-flash-free", 1800),
                ("opencode-go/glm-5.1", 1800),
                ("gemini-4-hydrogen-low", 900),
            ],
        )
        self.assertEqual(len(store.recorded), 1)
        self.assertEqual(store.recorded[0][0], "opencode-free")
        self.assertEqual(store.recorded[0][2], 120)
        self.assertTrue(any("skipping openrouter/qwen/qwen3-coder:free" in m for m in logs))
        self.assertTrue(any("skipping opencode/mimo-v2.6-flash-free" in m for m in logs))

    def test_chain_falls_through_timeout_errors(self) -> None:
        calls: list[str] = []

        def fake_runner(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
            calls.append(model_spec)
            if model_spec == "opencode/deepseek-v4.1-flash-free":
                raise subprocess.TimeoutExpired(cmd=["opencode"], timeout=timeout_sec)
            return {"ok": True}, {"total_tokens": 50}, 2.0

        out, usage, _ = run_agent_chain(
            "/tmp/factory",
            "/tmp/job",
            "/tmp/wt",
            "spec",
            "spec-writer",
            ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
            "write spec",
            {},
            900,
            config=DEFAULT_CONFIG,
            runner_fn=fake_runner,
        )
        self.assertEqual(out, {"ok": True})
        self.assertEqual(calls, ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"])
        self.assertEqual(usage["model"], "gemini-4-hydrogen-medium")

    def test_chain_never_falls_through_fatal_errors(self) -> None:
        fatal_cases = [
            JobHaltedError("operator halt"),
            LeaseLostError("lease stolen"),
            AgentExecutionError("Sandbox agent review (reviewer-code) illegally moved HEAD (abc -> def)"),
        ]
        for fatal_exc in fatal_cases:
            with self.subTest(exc=type(fatal_exc).__name__, msg=str(fatal_exc)):
                calls: list[str] = []

                def fake_runner(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, _exc=fatal_exc, **kw):
                    calls.append(model_spec)
                    raise _exc

                with self.assertRaises(type(fatal_exc)):
                    run_agent_chain(
                        "/tmp/factory",
                        "/tmp/job",
                        "/tmp/wt",
                        "review",
                        "reviewer-code",
                        ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
                        "review diff",
                        {},
                        900,
                        config=DEFAULT_CONFIG,
                        runner_fn=fake_runner,
                    )
                self.assertEqual(calls, ["opencode/deepseek-v4.1-flash-free"])

    def test_chain_never_falls_through_abort_check_exception(self) -> None:
        class CustomAbortError(RuntimeError):
            pass

        calls: list[str] = []

        def fake_runner(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
            calls.append(model_spec)
            abort_fn = kw.get("abort_check")
            if abort_fn is not None:
                exc = abort_fn()
                if exc is not None:
                    raise exc
            return {"ok": True}, {}, 1.0

        with self.assertRaises(CustomAbortError):
            run_agent_chain(
                "/tmp/factory",
                "/tmp/job",
                "/tmp/wt",
                "build",
                "builder",
                ["opencode/deepseek-v4.1-flash-free", "gemini-4-argon-high"],
                "build",
                {},
                900,
                config=DEFAULT_CONFIG,
                runner_fn=fake_runner,
                abort_check=lambda: CustomAbortError("aborted by test"),
            )
        self.assertEqual(calls, ["opencode/deepseek-v4.1-flash-free"])

    def test_chain_exhaustion_reraises_quota_if_all_quota_or_last_error(self) -> None:
        def all_quota_runner(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
            raise QuotaExhaustedError(model_spec, retry_after_sec=90, message=f"429 on {model_spec}")

        with self.assertRaises(QuotaExhaustedError) as ctx:
            run_agent_chain(
                "/tmp/factory",
                "/tmp/job",
                "/tmp/wt",
                "spec",
                "spec-writer",
                ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
                "spec",
                {},
                900,
                config=DEFAULT_CONFIG,
                runner_fn=all_quota_runner,
            )
        self.assertEqual(ctx.exception.model_spec, "gemini-4-hydrogen-medium")

        # When all models are already in cooldown, raises QuotaExhaustedError without calling any runner
        store = _FakeStateStore(
            {
                "opencode-free": {"cooldown_until_ts": time.time() + 300},
                "gemini-4-hydrogen": {"cooldown_until_ts": time.time() + 180},
            }
        )
        called = False

        def never_called(*args, **kwargs):
            nonlocal called
            called = True
            return {}, {}, 0.0

        with self.assertRaises(QuotaExhaustedError):
            run_agent_chain(
                "/tmp/factory",
                "/tmp/job",
                "/tmp/wt",
                "spec",
                "spec-writer",
                ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
                "spec",
                {},
                900,
                config=DEFAULT_CONFIG,
                state_store=store,
                runner_fn=never_called,
            )
        self.assertFalse(called)

        # Mixed failures (quota then AgentExecutionError) -> raises the last error (AgentExecutionError)
        def mixed_runner(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
            if model_spec.startswith("opencode/"):
                raise QuotaExhaustedError(model_spec, retry_after_sec=60, message="quota")
            raise AgentExecutionError("final model failed")

        with self.assertRaises(AgentExecutionError) as ctx2:
            run_agent_chain(
                "/tmp/factory",
                "/tmp/job",
                "/tmp/wt",
                "spec",
                "spec-writer",
                ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
                "spec",
                {},
                900,
                config=DEFAULT_CONFIG,
                runner_fn=mixed_runner,
            )
        self.assertIn("final model failed", str(ctx2.exception))

    def test_select_builder_model_and_resolve_reviewers_support_strings_and_chains(self) -> None:
        cfg_str = copy.deepcopy(DEFAULT_CONFIG)
        cfg_str["models"]["builder_low"] = "gemini-3.8-flash-low"
        self.assertEqual(
            select_builder_model(cfg_str, {"risk": "low"}, attempt=0),
            "gemini-3.8-flash-low",
        )
        self.assertEqual(
            select_builder_model(cfg_str, {"risk": "high"}, attempt=0),
            cfg_str["models"]["builder"],
        )

        cfg_chain = copy.deepcopy(DEFAULT_CONFIG)
        cfg_chain["models"]["builder_low"] = ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"]
        cfg_chain["models"]["builder"] = ["opencode-go/kimi-k2.5", "gemini-4-argon-high"]
        cfg_chain["models"]["code_high"] = ["opencode/mimo-v2.6-flash-free", "gemini-4-hydrogen-medium"]
        cfg_chain["models"]["security"] = ["opencode-go/glm-5.1", "gemini-4-argon-high"]

        self.assertEqual(
            select_builder_model(cfg_chain, {"risk": "low"}, attempt=0),
            ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
        )
        self.assertEqual(
            select_builder_chain(cfg_chain, {"risk": "medium"}, attempt=1, is_fix_round=True),
            ["opencode-go/kimi-k2.5", "gemini-4-argon-high"],
        )
        revs = resolve_reviewers(cfg_chain, "high", ["security"])
        self.assertEqual(revs["code"], ["opencode/mimo-v2.6-flash-free", "gemini-4-hydrogen-medium"])
        self.assertEqual(revs["security"], ["opencode-go/glm-5.1", "gemini-4-argon-high"])

    def test_pipeline_call_run_agent_and_step_recording_use_actual_winning_model(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = os.path.join(tmp, "jobs")
            os.makedirs(jobs_dir, exist_ok=True)
            enqueue_job(jobs_dir, {"number": 42, "title": "Test chain", "body": "body", "url": "http://example/42"}, max_rounds=3)
            job_id = "job-42"
            jdir = os.path.join(jobs_dir, job_id)
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            chain = ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-low"]

            calls: list[str] = []

            def fake_run_agent(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
                calls.append(model_spec)
                if model_spec.startswith("opencode/"):
                    raise QuotaExhaustedError(model_spec, retry_after_sec=60, message="free cap")
                return {"decision": "ACCEPT"}, {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}, 2.5

            with mock.patch("factory.lib.pipeline.run_agent", side_effect=fake_run_agent):
                out, usage, dur = _call_run_agent(
                    tmp,
                    jdir,
                    tmp,
                    "intake",
                    "intake",
                    chain,
                    "prompt",
                    {},
                    900,
                    config=cfg,
                    job_id=job_id,
                )
            self.assertEqual(calls, ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-low"])
            self.assertEqual(usage["model"], "gemini-4-hydrogen-low")
            line = _record_and_format_step(jobs_dir, job_id, chain, usage, dur, cfg)
            self.assertIn("gemini-4-hydrogen", line)
            job = load_job(jobs_dir, job_id)
            self.assertEqual(job["usage"]["by_tier"]["paid"]["calls"], 1)
            self.assertEqual(job["usage"]["by_provider"]["agy"]["calls"], 1)

    def test_batch_triage_and_review_work_with_model_chains(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "factory", "jobs"), exist_ok=True)
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg["models"]["planner"] = ["gpu:qwen2.5-coder:7b", "opencode/deepseek-v4.1-flash-free"]
            cfg["models"]["reviewer_code"] = ["opencode/mimo-v2.6-flash-free", "gemini-4-hydrogen-medium"]

            planner_calls: list[str] = []

            def fake_run_agent(factory_dir, job_dir, worktree, stage, agent_name, model_spec, prompt, schema, timeout_sec, **kw):
                planner_calls.append(model_spec)
                if model_spec.startswith("gpu:"):
                    raise GpuAbstain("low confidence")
                return (
                    {
                        "reasoning": "ok",
                        "items": [
                            {
                                "issue": 7,
                                "decision": "ACCEPT",
                                "risk": "low",
                                "surfaces": ["cli"],
                                "complexity_weight": 1,
                                "predicted_files": ["factory/cli.py"],
                                "depends_on": [],
                                "topic_group": "cli",
                                "summary": "cli tweak",
                                "questions": [],
                                "decision_reason": "clear",
                            }
                        ],
                    },
                    {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
                    1.2,
                )

            with (
                mock.patch("factory.lib.batch.run_agent", side_effect=fake_run_agent),
                mock.patch("factory.lib.batch._refresh_repo_map_if_needed", return_value="map"),
            ):
                plan = triage_and_plan_queue(
                    tmp,
                    [{"number": 7, "title": "CLI tweak", "body": "fix flag", "url": "http://example/7"}],
                    config=cfg,
                    gh=mock.MagicMock(),
                    dry_run=False,
                )
            self.assertEqual(planner_calls, ["gpu:qwen2.5-coder:7b", "opencode/deepseek-v4.1-flash-free"])
            self.assertEqual(len(plan.accepted), 1)
            job = load_job(os.path.join(tmp, "factory", "jobs"), "job-7")
            self.assertEqual(job["usage"]["cost_usd"], 0.0)
            self.assertEqual(job["usage"]["by_tier"]["free"]["calls"], 1)
            self.assertEqual(job["usage"]["by_provider"]["opencode"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
