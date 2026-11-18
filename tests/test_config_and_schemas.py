import json
import os
import tempfile
import unittest

from factory.lib.config import (
    DEFAULT_MODEL_PRICING,
    estimate_usage_cost_usd,
    load_config,
    load_config_with_layers,
    model_chain,
    provider_timeout_sec,
    resolve_model_tier,
    resolve_reviewers,
    split_model_and_effort,
)
from factory.lib.onboarding import init_repository
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
        self.assertIsNone(default_cfg["gpu"]["mcp_url"])
        self.assertEqual(default_cfg["model_pricing"], {})
        self.assertEqual(default_cfg["path_prepend"], [])
        self.assertEqual(default_cfg["providers"], {"agy": {"bin": None}, "opencode": {"bin": None}})
        cfg = load_config(os.path.abspath("factory"))
        self.assertTrue(cfg["allow_self_host"])
        self.assertEqual(cfg["auto_merge"], "all")
        self.assertIsNone(cfg["gpu"]["mcp_url"])
        self.assertEqual(cfg["model_pricing"], {})
        self.assertIsNone(cfg["models"]["builder_low"])
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
        self.assertEqual(DEFAULT_MODEL_PRICING, {})

        unpriced_usage = {
            "input_tokens": 100_000,
            "cache_read_tokens": 1_000_000,
            "output_tokens": 10_000,
            "thinking_tokens": 5_000,
            "total_tokens": 110_000,
        }
        cost_unpriced = estimate_usage_cost_usd("gemini-4-argon-high", unpriced_usage)
        self.assertEqual(cost_unpriced, 0.0)
        self.assertIs(unpriced_usage["priced"], False)

        with tempfile.TemporaryDirectory() as d:
            rates = dict(input_per_m=2.0, cached_per_m=0.5, output_per_m=8.0, tier="custom-tier")
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"model_pricing": {"gemini-custom-pro": rates}}, f)
            cfg = load_config(d)
            self.assertEqual(list(cfg["model_pricing"].keys()), ["gemini-custom-pro"])
            self.assertEqual(resolve_model_tier("gemini-custom-pro-high", cfg), "custom-tier")
            self.assertEqual(resolve_model_tier("gemini-3.8-flash-medium", cfg), "standard")
            self.assertEqual(resolve_model_tier("opencode/deepseek-v4.1-flash-free", cfg), "free")
            self.assertEqual(resolve_model_tier("gpu:decision-model", cfg), "local")
            priced_usage = {"input_tokens": 500_000, "cache_read_tokens": 200_000, "output_tokens": 50_000}
            custom_cost = estimate_usage_cost_usd("gemini-custom-pro-high", priced_usage, cfg)
            # 0.5 * 2.0 + 0.2 * 0.5 + 0.05 * 8.0 = 1.0 + 0.1 + 0.4 = 1.5
            self.assertAlmostEqual(custom_cost, 1.5)
            self.assertIs(priced_usage["priced"], True)
            self.assertEqual(priced_usage["tier"], "custom-tier")

    def test_config_example_loads_and_validates(self):
        example_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "factory",
            "config.example.json",
        )
        with open(example_path, encoding="utf-8") as fh:
            raw_text = fh.read()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                f.write(raw_text)
            cfg = load_config(d)
        self.assertNotIn("_comment", cfg)
        self.assertNotIn("_comment", cfg["model_pricing"])
        self.assertEqual(cfg["gpu"]["mcp_url"], "http://localhost:PORT/mcp")
        self.assertIn("gpu:decision-model", cfg["models"]["intake"])
        self.assertIn("gpu:decision-model", cfg["models"]["approver"])
        self.assertEqual(cfg["path_prepend"], ["~/.opencode/bin"])
        self.assertIn("agy", cfg["providers"])
        self.assertIn("opencode", cfg["providers"])

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

    def test_config_validation_chains_prefixes_gpu_roles_timeouts_and_mcp_url(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_file = os.path.join(d, "config.json")
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "models": {
                            "intake": ["gpu:decision-model", "gemini-4-hydrogen-low"],
                            "approver": ["gpu:decision-model", "gemini-4-argon-medium"],
                            "planner": ["gpu:decision-model", "gemini-4-hydrogen-low"],
                            "builder": ["opencode/deepseek-v4.1-flash-free", "gemini-4-hydrogen-medium"],
                            "code_low": ["openrouter/cohere/north-mini-code:free", "gemini-4-hydrogen-low"],
                        },
                        "provider_timeouts": {
                            "agy": None,
                            "opencode": 1800,
                            "opencode-go": 1800,
                            "openrouter": 1200,
                            "gpu": 60,
                        },
                        "gpu": {
                            "mcp_url": "http://localhost:9000/mcp",
                            "min_confidence": 0.9,
                            "tools": {"decide": "custom_decide", "classify": "custom_classify", "status": "custom_status"},
                        },
                        "quota": {
                            "default_cooldown_sec": 1800,
                            "buckets": [
                                {"match": "opencode/*-free", "bucket": "oc-free", "cooldown_sec": 600},
                                {"match": "gpu:*", "bucket": "gpu", "cooldown_sec": None},
                            ],
                        },
                        "path_prepend": ["~/custom/bin"],
                        "providers": {"agy": {"bin": "/usr/local/bin/agy"}, "opencode": {"bin": None}},
                    },
                    f,
                )
            cfg = load_config(d)
            self.assertEqual(model_chain(cfg, "intake"), ["gpu:decision-model", "gemini-4-hydrogen-low"])
            self.assertEqual(model_chain(cfg, "builder_low"), [])
            self.assertEqual(model_chain(cfg, "spec_writer"), ["gemini-3.8-flash-medium"])
            self.assertEqual(provider_timeout_sec(cfg, "gpu:decision-model", 900), 60)
            self.assertEqual(provider_timeout_sec(cfg, "opencode/deepseek-v4.1-flash-free", 900), 1800)
            self.assertEqual(provider_timeout_sec(cfg, "openrouter/cohere/north-mini-code:free", 900), 1200)
            self.assertEqual(provider_timeout_sec(cfg, "gemini-4-hydrogen-medium", 900), 900)
            self.assertEqual(cfg["gpu"]["tools"]["decide"], "custom_decide")
            self.assertEqual(cfg["quota"]["default_cooldown_sec"], 1800)

            rev = resolve_reviewers(cfg, "low", [])
            self.assertEqual(rev["code"], ["openrouter/cohere/north-mini-code:free", "gemini-4-hydrogen-low"])

            # Empty list chain rejected
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"models": {"builder": []}}, f)
            with self.assertRaisesRegex(ValueError, "non-empty list"):
                load_config(d)

            # Empty string in chain rejected
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"models": {"builder": ["gemini-4-hydrogen-medium", "  "]}}, f)
            with self.assertRaisesRegex(ValueError, "non-empty model strings"):
                load_config(d)

            # Unknown prefix rejected
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"models": {"builder": ["anthropic/claude-4"]}}, f)
            with self.assertRaisesRegex(ValueError, "unknown model prefix"):
                load_config(d)

            # gpu: rejected in disallowed roles
            for bad_role in ("builder", "batch_builder", "builder_low", "spec_writer", "qa", "code_low", "security", "ux", "ui"):
                with open(cfg_file, "w", encoding="utf-8") as f:
                    json.dump({"models": {bad_role: ["gpu:decision-model", "gemini-4-hydrogen-low"]}}, f)
                with self.assertRaisesRegex(ValueError, "gpu:"):
                    load_config(d)

            # Invalid provider_timeouts
            for bad_pt in ({"opencode": 0}, {"opencode": -5}, {"opencode": True}, {"opencode": "1800"}):
                with open(cfg_file, "w", encoding="utf-8") as f:
                    json.dump({"provider_timeouts": bad_pt}, f)
                with self.assertRaisesRegex(ValueError, "provider_timeouts"):
                    load_config(d)

            # Invalid gpu.mcp_url (null is valid, empty/non-http is invalid)
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"gpu": {"mcp_url": None}}, f)
            self.assertIsNone(load_config(d)["gpu"]["mcp_url"])

            for bad_url in ("", "ftp://localhost:9000/mcp", "not-a-url"):
                with open(cfg_file, "w", encoding="utf-8") as f:
                    json.dump({"gpu": {"mcp_url": bad_url}}, f)
                with self.assertRaisesRegex(ValueError, "gpu.mcp_url"):
                    load_config(d)

            # Invalid quota, path_prepend, and providers.*.bin
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"quota": {"default_cooldown_sec": 0}}, f)
            with self.assertRaisesRegex(ValueError, "quota.default_cooldown_sec"):
                load_config(d)

            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"path_prepend": [""]}, f)
            with self.assertRaisesRegex(ValueError, "path_prepend"):
                load_config(d)

            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({"providers": {"opencode": {"bin": ""}}}, f)
            with self.assertRaisesRegex(ValueError, "providers.opencode.bin"):
                load_config(d)

    def test_pricing_unpriced_defaults_and_custom_rates(self):
        # Default model_pricing is empty -> all unpriced models cost $0.0 and mark priced=False
        for model in (
            "gemini-4-hydrogen-medium",
            "gemini-4-argon-high",
            "opencode/deepseek-v4.1-flash-free",
            "opencode/big-pickle",
            "openrouter/cohere/north-mini-code:free",
            "gpu:decision-model",
            "opencode-go/glm-5.3",
        ):
            u = {"input_tokens": 500_000, "output_tokens": 100_000, "total_tokens": 600_000}
            self.assertEqual(estimate_usage_cost_usd(model, u), 0.0)
            self.assertIs(u["priced"], False)

        # User-configured pricing entry with explicit cached_per_m and 25% default cached_per_m
        explicit_rates = dict(input_per_m=1.5, cached_per_m=0.375, output_per_m=7.5)
        fallback_cache_rates = dict(input_per_m=4.0, output_per_m=12.0)
        custom_cfg = {
            "model_pricing": {
                "gemini-4-hydrogen": explicit_rates,
                "gemini-custom": fallback_cache_rates,
            }
        }
        u_h = {"input_tokens": 1_000_000, "cache_read_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(
            estimate_usage_cost_usd("gemini-4-hydrogen-medium", u_h, custom_cfg),
            1.5 + 0.375 + 7.5,
        )
        self.assertIs(u_h["priced"], True)

        u_c = {"input_tokens": 0, "cache_read_tokens": 1_000_000, "output_tokens": 0}
        self.assertAlmostEqual(
            estimate_usage_cost_usd("gemini-custom-low", u_c, custom_cfg),
            1.0,
        )
        self.assertIs(u_c["priced"], True)

        # Direct step_finish cost > 0 takes precedence and marks priced=True
        u_direct = {"input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.012345}
        self.assertAlmostEqual(
            estimate_usage_cost_usd("opencode/deepseek-v4.1-flash-free", u_direct),
            0.012345,
        )
        self.assertIs(u_direct["priced"], True)

    def test_config_layering_precedence_deep_merge_comments_and_init_gitignore(self):
        with tempfile.TemporaryDirectory() as d:
            user_cfg_path = os.path.join(d, "user-config.json")
            fdir = os.path.join(d, "repo", "factory")
            os.makedirs(fdir, exist_ok=True)
            repo_cfg_path = os.path.join(fdir, "config.json")
            local_cfg_path = os.path.join(fdir, "config.local.json")

            old_env = os.environ.get("FACTORY_USER_CONFIG")
            try:
                # Layer 1 only: DEFAULT_CONFIG
                os.environ["FACTORY_USER_CONFIG"] = user_cfg_path
                cfg1, layers1 = load_config_with_layers(fdir)
                self.assertEqual(layers1, [])
                self.assertEqual(cfg1["concurrency"], 4)
                self.assertEqual(cfg1["auto_merge"], "low")

                # Layer 2: User config overrides defaults and supports _comment keys
                with open(user_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "_comment": "user level defaults",
                            "concurrency": 6,
                            "labels": {"_comment": "lbl", "queue": "user-queue"},
                            "models": {"builder": "gemini-4-hydrogen-medium", "intake": "gemini-3.6-flash-low"},
                            "protected_paths": ["user-only/"],
                        },
                        f,
                    )
                cfg2, layers2 = load_config_with_layers(fdir)
                self.assertEqual(layers2, [user_cfg_path])
                self.assertNotIn("_comment", cfg2)
                self.assertNotIn("_comment", cfg2["labels"])
                self.assertEqual(cfg2["concurrency"], 6)
                self.assertEqual(cfg2["labels"]["queue"], "user-queue")
                self.assertEqual(cfg2["labels"]["needs_human"], "factory:needs-human")
                self.assertEqual(cfg2["models"]["builder"], "gemini-4-hydrogen-medium")
                self.assertEqual(cfg2["models"]["batch_builder"], "gemini-4-hydrogen-medium")
                self.assertEqual(cfg2["protected_paths"], ["user-only/"])

                # Layer 3: Repo config.json overrides user config; dicts deep-merge, lists/scalars replace
                with open(repo_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "_note": "repo config",
                            "concurrency": 2,
                            "labels": {"needs_info": "repo-needs-info"},
                            "models": {"builder": ["opencode/deepseek-v4.1-flash-free", "gemini-3.8-flash-medium"]},
                            "protected_paths": ["factory/", "custom/"],
                        },
                        f,
                    )
                cfg3, layers3 = load_config_with_layers(fdir)
                self.assertEqual(layers3, [user_cfg_path, repo_cfg_path])
                self.assertEqual(cfg3["concurrency"], 2)
                self.assertEqual(cfg3["labels"]["queue"], "user-queue")
                self.assertEqual(cfg3["labels"]["needs_info"], "repo-needs-info")
                self.assertEqual(cfg3["models"]["intake"], "gemini-3.6-flash-low")
                self.assertEqual(
                    cfg3["models"]["builder"],
                    ["opencode/deepseek-v4.1-flash-free", "gemini-3.8-flash-medium"],
                )
                self.assertEqual(
                    cfg3["models"]["batch_builder"],
                    ["opencode/deepseek-v4.1-flash-free", "gemini-3.8-flash-medium"],
                )
                self.assertEqual(cfg3["protected_paths"], ["factory/", "custom/"])

                # Layer 4: Repo config.local.json overrides repo config.json and user config
                with open(local_cfg_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "_comment": "local machine override",
                            "concurrency": 8,
                            "models": {"builder": "gemini-4-argon-high"},
                        },
                        f,
                    )
                cfg4, layers4 = load_config_with_layers(fdir)
                self.assertEqual(layers4, [user_cfg_path, repo_cfg_path, local_cfg_path])
                self.assertEqual(cfg4["concurrency"], 8)
                self.assertEqual(cfg4["models"]["builder"], "gemini-4-argon-high")
                self.assertEqual(cfg4["models"]["batch_builder"], "gemini-4-argon-high")
                self.assertEqual(cfg4["models"]["intake"], "gemini-3.6-flash-low")
                self.assertEqual(load_config(fdir)["concurrency"], 8)
            finally:
                if old_env is None:
                    os.environ.pop("FACTORY_USER_CONFIG", None)
                else:
                    os.environ["FACTORY_USER_CONFIG"] = old_env

            init_repo_dir = os.path.join(d, "fresh-repo")
            os.makedirs(init_repo_dir, exist_ok=True)
            init_repository(init_repo_dir)
            with open(os.path.join(init_repo_dir, ".gitignore"), encoding="utf-8") as f:
                gi_lines = [ln.strip() for ln in f if ln.strip()]
            self.assertIn("factory/config.local.json", gi_lines)


if __name__ == "__main__":
    unittest.main()
