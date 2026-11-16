import json
import os
import tempfile
import unittest

from factory.lib.config import (
    estimate_usage_cost_usd,
    load_config,
    resolve_reviewers,
    split_model_and_effort,
)
from factory.lib.schemas import (
    SCHEMAS,
    render_approve_md,
    render_build_md,
    render_intake_md,
    render_pr_body,
    render_review_md,
    render_spec_md,
    validate_stage_output,
)


class ConfigAndSchemaTests(unittest.TestCase):
    def test_load_repo_config_and_route_all_surfaces(self):
        default_cfg = load_config("/nonexistent-default-factory-dir")
        self.assertEqual(default_cfg["auto_merge"], "low")
        self.assertFalse(default_cfg["allow_self_host"])
        cfg = load_config(os.path.abspath("factory"))
        self.assertTrue(cfg["allow_self_host"])
        self.assertEqual(cfg["auto_merge"], "all")
        rev = resolve_reviewers(cfg, "high", ["ui", "security"])
        self.assertEqual(set(rev.keys()), {"code", "security", "ux", "ui"})
        self.assertEqual(rev["code"], "gemini-3.8-flash-low")
        self.assertEqual(rev["security"], "gemini-3.8-flash-medium")
        rev_low = resolve_reviewers(cfg, "low", [])
        self.assertEqual(rev_low, {"code": "gemini-3.6-flash-low"})

    def test_nit_only_review_normalizes_to_pass(self):
        raw = {
            "verdict": "CHANGES",
            "summary": ["Minor style comment"],
            "findings": [
                {"severity": "nit", "file": "a.py", "line": 3, "issue": "name", "fix": "rename"}
            ],
            "checked": ["spec compliance"],
        }
        out = validate_stage_output("review", raw)
        self.assertEqual(out["verdict"], "PASS")
        self.assertIn("VERDICT: PASS", render_review_md("code", out))

    def test_actionable_review_keeps_changes_verdict(self):
        raw = {
            "verdict": "CHANGES",
            "summary": ["Bug in auth check"],
            "findings": [
                {"severity": "must_fix", "file": "auth.py", "line": 12, "issue": "missing check", "fix": "verify token"}
            ],
            "checked": ["auth boundary"],
        }
        out = validate_stage_output("review", raw)
        self.assertEqual(out["verdict"], "CHANGES")
        self.assertIn("VERDICT: CHANGES", render_review_md("security", out))

    def test_spec_validation_enforces_one_to_six_slices(self):
        with self.assertRaises(ValueError):
            validate_stage_output(
                "spec",
                {
                    "summary": ["x"],
                    "problem": "p",
                    "solution": "s",
                    "user_stories": ["u"],
                    "implementation_decisions": [],
                    "testing_decisions": [],
                    "glossary_updates": [],
                    "out_of_scope": [],
                    "slices": [],
                },
            )

    def test_invalid_auto_merge_and_merge_method_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"auto_merge": "yolo"}, f)
            with self.assertRaises(ValueError):
                load_config(d)
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"merge_method": "fast-forward"}, f)
            with self.assertRaises(ValueError):
                load_config(d)

    def test_markdown_renderers_and_schemas_cover_all_stages(self):
        self.assertEqual(
            set(SCHEMAS.keys()),
            {"plan", "batch_plan", "intake", "spec", "build", "batch_build", "review", "approve", "qa"},
        )
        intake = validate_stage_output(
            "intake",
            {
                "decision": "ACCEPT",
                "risk": "medium",
                "surfaces": ["security"],
                "summary": ["Add token rotation"],
                "scope_in": "Token rotation endpoint",
                "scope_out": "UI dashboard",
                "questions": [],
                "reasoning": "Clear backend feature.",
            },
        )
        spec = validate_stage_output(
            "spec",
            {
                "summary": ["Rotate refresh tokens"],
                "problem": "Stale tokens remain valid",
                "solution": "Rotate on refresh",
                "user_stories": ["As a user, my refresh token rotates"],
                "implementation_decisions": ["Store token family id"],
                "testing_decisions": ["Test public refresh handler seam"],
                "glossary_updates": [],
                "out_of_scope": ["Admin UI"],
                "slices": [
                    {
                        "id": "slice-1",
                        "title": "Token rotation core",
                        "goal": "Rotate and invalidate reused tokens",
                        "files": ["src/auth.py"],
                        "test_seams": ["refresh_session()"],
                        "acceptance_criteria": ["Old token fails after rotation"],
                    }
                ],
            },
        )
        build = validate_stage_output(
            "build",
            {
                "verdict": "DONE",
                "summary": ["Implemented slice 1"],
                "changes": ["Added refresh_session rotation"],
                "tests_added": ["test_reused_token_revokes_family"],
                "blocked_reason": "",
                "notes": "",
            },
        )
        approve = validate_stage_output(
            "approve",
            {
                "decision": "APPROVE",
                "summary": ["All checks and security review passed"],
                "reasoning": "Matches spec cleanly",
                "merge_danger": "Two-way door; isolated to refresh endpoint",
                "question": "",
            },
        )
        self.assertIn("DECISION: ACCEPT", render_intake_md(intake))
        self.assertIn("## Vertical Slices", render_spec_md(spec))
        self.assertIn("VERDICT: DONE", render_build_md(build))
        self.assertIn("DECISION: APPROVE", render_approve_md(approve))
        pr_body = render_pr_body(
            {"issue": 42, "risk": "medium", "round": 1},
            intake,
            spec,
            {"code": {"verdict": "PASS", "summary": ["Clean"]}},
            approve,
        )
        self.assertIn("Closes #42", pr_body)
        self.assertIn("Two-way door", pr_body)

    def test_reject_intake_requires_non_empty_reasoning(self):
        with self.assertRaises(ValueError):
            validate_stage_output(
                "intake",
                {
                    "decision": "REJECT",
                    "risk": "low",
                    "surfaces": [],
                    "summary": ["Out of scope"],
                    "scope_in": "",
                    "scope_out": "",
                    "questions": [],
                    "reasoning": "   ",
                },
            )

    def test_split_model_and_effort_and_estimate_usage_cost(self):
        self.assertEqual(split_model_and_effort("gemini-4-argon-high"), ("gemini-4-argon", "high"))
        self.assertEqual(split_model_and_effort("gemini-3.8-flash-medium"), ("gemini-3.8-flash", "medium"))
        self.assertEqual(split_model_and_effort("custom-model"), ("custom-model", "default"))
        self.assertEqual(split_model_and_effort(""), ("unknown", "default"))

        cost = estimate_usage_cost_usd(
            "gemini-4-argon-high",
            {
                "input_tokens": 100_000,
                "cache_read_tokens": 1_000_000,
                "output_tokens": 10_000,
                "thinking_tokens": 5_000,
                "total_tokens": 110_000,
            },
        )
        # 0.1M * 1.25 + 1.0M * 0.3125 + 0.01M * 10.0 = 0.5375 (thinking_tokens not double-counted)
        self.assertAlmostEqual(cost, 0.5375)

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "model_pricing": {
                            "custom-pro": {"input_per_m": 2.0, "cached_per_m": 0.5, "output_per_m": 8.0}
                        }
                    },
                    f,
                )
            cfg = load_config(d)
            self.assertIn("gemini-4-argon", cfg["model_pricing"])
            custom_cost = estimate_usage_cost_usd(
                "custom-pro-high",
                {"input_tokens": 500_000, "cache_read_tokens": 200_000, "output_tokens": 50_000},
                cfg,
            )
            # 0.5 * 2.0 + 0.2 * 0.5 + 0.05 * 8.0 = 1.0 + 0.1 + 0.4 = 1.5
            self.assertAlmostEqual(custom_cost, 1.5)

    def test_batch_config_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            cfg = load_config(d)
            self.assertEqual(
                cfg["batch"],
                {
                    "enabled": True,
                    "max_batch_issues": 5,
                    "max_batch_weight": 6,
                    "max_parallel_batches": 2,
                    "refresh_repo_map": True,
                },
            )
            self.assertEqual(cfg["models"]["batch_builder"], "gemini-3.8-flash-medium")

            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"batch": {"max_batch_issues": 0}}, f)
            with self.assertRaisesRegex(ValueError, "max_batch_issues"):
                load_config(d)

    def test_validate_batch_plan_enforces_enums_bounds_and_reasons(self):
        valid = validate_stage_output(
            "batch_plan",
            {
                "items": [
                    {
                        "issue": 12,
                        "decision": "ACCEPT",
                        "risk": "low",
                        "surfaces": ["security"],
                        "complexity_weight": 2,
                        "predicted_files": ["factory/cli.py"],
                        "depends_on": [],
                        "topic_group": "cli",
                        "summary": "Add check",
                        "questions": [],
                        "decision_reason": "",
                    }
                ],
                "reasoning": "Single issue ready",
            },
        )
        self.assertEqual(valid["items"][0]["complexity_weight"], 2)

        with self.assertRaisesRegex(ValueError, "question"):
            validate_stage_output(
                "batch_plan",
                {
                    "items": [{**valid["items"][0], "decision": "NEEDS_INFO", "questions": []}],
                    "reasoning": "Missing info",
                },
            )
        with self.assertRaisesRegex(ValueError, "complexity_weight"):
            validate_stage_output(
                "batch_plan",
                {
                    "items": [{**valid["items"][0], "complexity_weight": 7}],
                    "reasoning": "Bad weight",
                },
            )

    def test_validate_batch_build_enforces_status_unique_issues_and_blocked_reason(self):
        valid = validate_stage_output(
            "batch_build",
            {
                "results": [
                    {
                        "issue": 12,
                        "status": "DONE",
                        "commit": "abc1234",
                        "summary": ["Added check"],
                        "changes": ["factory/cli.py"],
                        "tests_run": ["tests/test_cli.py"],
                        "blocked_reason": "",
                    }
                ],
                "notes": "All green",
            },
        )
        self.assertEqual(valid["results"][0]["status"], "DONE")

        with self.assertRaisesRegex(ValueError, "blocked_reason"):
            validate_stage_output(
                "batch_build",
                {
                    "results": [{**valid["results"][0], "status": "BLOCKED", "blocked_reason": ""}],
                    "notes": "",
                },
            )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_stage_output(
                "batch_build",
                {
                    "results": [valid["results"][0], valid["results"][0]],
                    "notes": "",
                },
            )


if __name__ == "__main__":
    unittest.main()
