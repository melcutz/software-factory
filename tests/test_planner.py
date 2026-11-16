"""Unit tests for queue dependency DAG planner, topological scheduling, and multi-issue CLI."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

from factory import cli
from factory.lib.config import estimate_usage_cost_usd
from factory.lib.git_ops import GitHubClient, is_worktree_clean
from factory.lib.pipeline import (
    factory_runner_status,
    format_status_table,
    process_job,
    run_factory,
)
from factory.lib.planner import build_dependency_dag, parse_explicit_dependencies
from factory.lib.state import advance_stage, create_or_resume_job, list_jobs, update_job


def _load_cli_module():
    return cli


class PlannerTests(unittest.TestCase):
    def test_parse_explicit_dependencies_within_batch(self):
        issues = [
            {"number": 10, "title": "Base schema", "body": "Initial tables"},
            {"number": 11, "title": "API", "body": "Depends on #10 and #999 (external)"},
            {"number": 12, "title": "UI", "body": "Blocked by #10, #11\nAfter #12 (self)"},
        ]
        deps = parse_explicit_dependencies(issues)
        self.assertEqual(deps, {10: set(), 11: {10}, 12: {10, 11}})

    def test_build_dependency_dag_merges_surfaces_and_breaks_cycles(self):
        issues = [
            {"number": 1, "title": "A", "body": ""},
            {"number": 2, "title": "B", "body": ""},
            {"number": 3, "title": "C", "body": ""},
        ]

        def fake_planner(_batch):
            return {
                "edges": [{"issue": 2, "depends_on": [1]}],
                "predicted_surfaces": [
                    {"issue": 1, "paths": ["factory/lib/pipeline.py"]},
                    {"issue": 3, "paths": ["factory/lib/pipeline.py"]},
                ],
                "reasoning": "1 and 3 touch pipeline.py",
            }

        deps, surfaces = build_dependency_dag(issues, planner_fn=fake_planner)
        self.assertEqual(deps[1], set())
        self.assertEqual(deps[2], {1})
        self.assertEqual(deps[3], {1})
        self.assertEqual(surfaces[1], ["factory/lib/pipeline.py"])

        # Cycle from LLM falls back to explicit edges
        explicit_issues = [
            {"number": 1, "title": "A", "body": ""},
            {"number": 2, "title": "B", "body": "Depends on #1"},
        ]

        def cyclic_planner(_batch):
            return {
                "edges": [{"issue": 1, "depends_on": [2]}],
                "predicted_surfaces": [],
                "reasoning": "cycle",
            }

        deps_fallback, _ = build_dependency_dag(explicit_issues, planner_fn=cyclic_planner)
        self.assertEqual(deps_fallback, {1: set(), 2: {1}})

        # Explicit cycle is also broken deterministically
        explicit_cycle = [
            {"number": 1, "title": "A", "body": "Depends on #2"},
            {"number": 2, "title": "B", "body": "Depends on #1"},
        ]
        deps_acyclic, _ = build_dependency_dag(explicit_cycle)
        self.assertTrue(len(deps_acyclic[1]) + len(deps_acyclic[2]) <= 1)

    def test_run_factory_topological_order_and_failed_parent_skips_dependents(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "factory", "jobs"), exist_ok=True)
            with open(os.path.join(tmp, "factory", "config.json"), "w", encoding="utf-8") as f:
                f.write('{"concurrency": 4}\n')

            issues_map = {
                10: {"number": 10, "title": "Parent", "body": "", "url": "u10"},
                11: {"number": 11, "title": "Child", "body": "Depends on #10", "url": "u11"},
                12: {"number": 12, "title": "Independent", "body": "", "url": "u12"},
            }
            call_order = []

            def fake_fetch(_self, num):
                return issues_map[num], f"# {num}"

            def fake_process(_repo, num, **_kw):
                call_order.append(num)
                if num == 10:
                    return {"id": "job-10", "issue": 10, "stage": "needs-human"}
                return {"id": f"job-{num}", "issue": num, "stage": "merged"}

            with (
                patch("factory.lib.pipeline.GitHubClient.fetch_issue", fake_fetch),
                patch("factory.lib.pipeline.GitHubClient.comment_issue"),
                patch("factory.lib.pipeline.process_job", fake_process),
            ):
                results = run_factory(tmp, issue_numbers=[10, 11, 12], batch_mode=False)

            # 11 depended on 10, which halted in needs-human -> 11 must be skipped
            self.assertIn(10, call_order)
            self.assertIn(12, call_order)
            self.assertNotIn(11, call_order)
            self.assertEqual({r["issue"] for r in results}, {10, 12})

    def test_cli_run_accepts_zero_one_or_multiple_issues(self):
        cli = _load_cli_module()
        seen = []

        def fake_run_factory(_repo_root, issue_number=None, issue_numbers=None):
            seen.append((issue_number, issue_numbers))
            return []

        with patch.object(cli, "run_factory", side_effect=fake_run_factory):
            self.assertEqual(cli.main(["run"]), 0)
            self.assertEqual(cli.main(["run", "42"]), 0)
            self.assertEqual(cli.main(["run", "42", "43"]), 0)

        self.assertEqual(seen, [(None, None), (None, [42]), (None, [42, 43])])

    def test_generic_directories_like_tests_and_static_do_not_serialize_batch(self):
        issues = [
            {"number": 564, "title": "Ingest A", "body": ""},
            {"number": 565, "title": "Ingest B", "body": ""},
            {"number": 566, "title": "UI Reader", "body": ""},
            {"number": 567, "title": "UI Settings", "body": ""},
            {"number": 568, "title": "Migration A", "body": ""},
            {"number": 569, "title": "Migration B", "body": ""},
        ]

        def fake_planner(_batch):
            return {
                "edges": [],
                "predicted_surfaces": [
                    {"issue": 564, "paths": ["src/app/ingest.py", "tests/", "tests/test_ingest.py"]},
                    {"issue": 565, "paths": ["src/app/ingest.py", "tests/"]},
                    {"issue": 566, "paths": ["templates/reader.html", "src/app/static/", "src/app/static/tailwind-abc.css", "tests/"]},
                    {"issue": 567, "paths": ["templates/settings.html", "templates/", "src/app/static/", "src/app/entitlements/", "tests/"]},
                    {"issue": 568, "paths": ["migrations/", "tests/"]},
                    {"issue": 569, "paths": ["migrations/0009_idx.sql", "src/app/entitlements/", "tests/"]},
                ],
                "reasoning": "Only exact file or migration dir overlaps serialize.",
            }

        deps, _ = build_dependency_dag(issues, planner_fn=fake_planner)
        self.assertEqual(deps[564], set())
        self.assertEqual(deps[565], {564})
        self.assertEqual(deps[566], set())
        self.assertEqual(deps[567], set())
        self.assertEqual(deps[568], set())
        self.assertEqual(deps[569], {568})

    def test_soft_surface_overlap_does_not_skip_downstream_when_earlier_job_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "factory", "jobs"), exist_ok=True)
            os.makedirs(os.path.join(tmp, "factory", "agents"), exist_ok=True)
            with open(os.path.join(tmp, "factory", "agents", "planner.md"), "w", encoding="utf-8") as f:
                f.write("# Planner\n")
            with open(os.path.join(tmp, "factory", "config.json"), "w", encoding="utf-8") as f:
                f.write('{"concurrency": 2}\n')

            issues_map = {
                10: {"number": 10, "title": "First ingest fix", "body": "", "url": "u10"},
                11: {"number": 11, "title": "Second ingest fix", "body": "", "url": "u11"},
                12: {"number": 12, "title": "Hard child", "body": "Depends on #10", "url": "u12"},
            }
            call_order = []

            def fake_fetch(_self, num):
                return issues_map[num], f"# {num}"

            def fake_run_agent(*_args, **_kwargs):
                return (
                    {
                        "edges": [],
                        "predicted_surfaces": [
                            {"issue": 10, "paths": ["src/app/ingest.py"]},
                            {"issue": 11, "paths": ["src/app/ingest.py"]},
                            {"issue": 12, "paths": []},
                        ],
                        "reasoning": "10 and 11 both touch ingest.py",
                    },
                    {},
                    0.1,
                )

            def fake_process(_repo, num, **_kw):
                call_order.append(num)
                if num == 10:
                    return {"id": "job-10", "issue": 10, "stage": "needs-human"}
                return {"id": f"job-{num}", "issue": num, "stage": "merged"}

            with (
                patch("factory.lib.pipeline.GitHubClient.fetch_issue", fake_fetch),
                patch("factory.lib.pipeline.GitHubClient.comment_issue"),
                patch("factory.lib.pipeline.run_agent", fake_run_agent),
                patch("factory.lib.pipeline.process_job", fake_process),
            ):
                results = run_factory(tmp, issue_numbers=[10, 11, 12], batch_mode=False)

            self.assertEqual(call_order, [10, 11])
            self.assertEqual({r["issue"] for r in results}, {10, 11})

    def test_list_queue_issues_requests_body_so_batch_planner_sees_dependencies(self):
        captured_args = []

        def fake_runner(args, _cwd):
            captured_args.append(args)
            return (
                '[{"number": 21, "title": "Child", "body": "Depends on #20", '
                '"url": "u21", "labels": [{"name": "factory"}]}, '
                '{"number": 20, "title": "Parent", "body": "Base work", '
                '"url": "u20", "labels": [{"name": "factory"}]}]'
            )

        gh = GitHubClient("/tmp", runner=fake_runner)
        cfg = {
            "labels": {
                "queue": "factory",
                "needs_info": "factory:needs-info",
                "needs_human": "factory:needs-human",
            }
        }
        items = gh.list_queue_issues(cfg)
        json_fields = captured_args[0][captured_args[0].index("--json") + 1].split(",")
        self.assertIn("body", json_fields)
        deps = parse_explicit_dependencies(items)
        self.assertEqual(deps, {20: set(), 21: {20}})

    def test_run_factory_pre_registers_queued_jobs_visible_in_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = os.path.join(tmp, "factory", "jobs")
            os.makedirs(jobs_dir, exist_ok=True)
            with open(os.path.join(tmp, "factory", "config.json"), "w", encoding="utf-8") as f:
                f.write('{"concurrency": 1}\n')

            batch = [
                {"number": 201, "title": "First", "body": "", "url": "u201"},
                {"number": 202, "title": "Second", "body": "", "url": "u202"},
                {"number": 203, "title": "Third", "body": "Depends on #201", "url": "u203"},
            ]
            snapshots = []

            def fake_list_queue(_self, _cfg):
                if snapshots:
                    return []
                return batch

            def fake_process(_repo, num, **_kw):
                if num == 201:
                    jobs = list_jobs(jobs_dir)
                    snapshots.append(
                        (
                            {j["id"]: j["stage"] for j in jobs},
                            format_status_table(jobs),
                            factory_runner_status(jobs_dir, jobs),
                        )
                    )
                job, action = create_or_resume_job(
                    jobs_dir,
                    {"number": num, "title": f"T{num}", "url": f"u{num}"},
                    max_rounds=3,
                )
                self.assertIn(action, {"new", "resume"})
                self.assertEqual(job["stage"], "intake")
                return {"id": f"job-{num}", "issue": num, "stage": "merged"}

            with (
                patch("factory.lib.pipeline.GitHubClient.list_queue_issues", fake_list_queue),
                patch(
                    "factory.lib.pipeline.run_agent",
                    return_value=({"edges": [], "predicted_surfaces": [], "reasoning": "ok"}, {}, 0.1),
                ),
                patch("factory.lib.pipeline.process_job", fake_process),
            ):
                results = run_factory(tmp, batch_mode=False)

            self.assertEqual(len(results), 3)
            stages_map, table_str, banner_str = snapshots[0]
            self.assertEqual(stages_map, {"job-201": "queued", "job-202": "queued", "job-203": "queued"})
            self.assertIn("job-202", table_str)
            self.assertIn("job-203", table_str)
            self.assertIn("queued", banner_str)

    def test_gemini_4_hydrogen_cost_estimation(self):
        cost = estimate_usage_cost_usd(
            "gemini-4-hydrogen-medium",
            {"input_tokens": 100_000, "cache_read_tokens": 200_000, "output_tokens": 10_000},
        )
        self.assertIsNotNone(cost)
        self.assertGreater(cost, 0.0)

    def test_resumed_build_slice_cleans_uncommitted_debris_on_first_attempt(self):
        import json
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            repo = os.path.join(tmp, "repo")
            os.makedirs(origin)
            subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=origin, check=True)
            subprocess.run(["git", "clone", origin, repo], check=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
                f.write("# Repo\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True)

            wt = os.path.join(repo, ".worktrees", "job-77")
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            subprocess.run(["git", "worktree", "add", "-b", "factory/job-77", wt, "origin/main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=wt, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=wt, check=True)
            with open(os.path.join(wt, "slice1.py"), "w", encoding="utf-8") as f:
                f.write("SLICE_1 = True\n")
            subprocess.run(["git", "add", "slice1.py"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-m", "slice 1"], cwd=wt, check=True)
            with open(os.path.join(wt, "half_written.py"), "w", encoding="utf-8") as f:
                f.write("def broken(\n")
            self.assertFalse(is_worktree_clean(wt))

            jobs_dir = os.path.join(repo, "factory", "jobs")
            jdir = os.path.join(jobs_dir, "job-77")
            os.makedirs(os.path.join(jdir, "round-1"), exist_ok=True)
            with open(os.path.join(repo, "factory", "config.json"), "w", encoding="utf-8") as f:
                f.write('{"max_slice_retries": 1}\n')
            with open(os.path.join(jdir, "spec.md"), "w", encoding="utf-8") as f:
                f.write("# Spec\n")
            with open(os.path.join(jdir, "plan.json"), "w", encoding="utf-8") as f:
                json.dump({"slices": [{"id": "s1", "title": "Slice 1"}, {"id": "s2", "title": "Slice 2"}]}, f)
            with open(os.path.join(jdir, "job.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": "job-77",
                        "issue": 77,
                        "issue_url": "u77",
                        "issue_title": "Resume test",
                        "branch": "factory/job-77",
                        "stage": "build",
                        "round": 1,
                        "round_limit": 3,
                        "risk": "low",
                        "surfaces": [],
                        "total_slices": 2,
                        "completed_slices": 1,
                        "pr_number": None,
                        "pr_url": None,
                    },
                    f,
                )

            observed_clean_at_start = []

            def fake_run_agent(_fdir, _jdir, worktree_arg, step, *_rest, **_kw):
                if step == "build":
                    observed_clean_at_start.append(
                        (
                            is_worktree_clean(worktree_arg),
                            os.path.exists(os.path.join(worktree_arg, "slice1.py")),
                            os.path.exists(os.path.join(worktree_arg, "half_written.py")),
                        )
                    )
                    return (
                        {
                            "verdict": "BLOCKED",
                            "summary": [],
                            "changes": [],
                            "tests_added": [],
                            "blocked_reason": "stop after check",
                            "notes": "",
                        },
                        {},
                        0.1,
                    )
                raise AssertionError(f"Unexpected step {step}")

            class DummyGH:
                def fetch_issue(self, _n):
                    return {"number": 77, "title": "Resume test", "url": "u77"}, "# 77"

                def edit_labels(self, *_a, **_kw):
                    pass

                def comment_issue(self, *_a, **_kw):
                    pass

            with patch("factory.lib.pipeline.run_agent", fake_run_agent):
                process_job(repo, 77, gh=DummyGH())

            self.assertEqual(observed_clean_at_start, [(True, True, False)])

    def test_cli_status_missing_job_returns_nonzero_and_terminal_stage_clears_pid(self):
        cli = _load_cli_module()
        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = os.path.join(tmp, "factory", "jobs")
            os.makedirs(jobs_dir, exist_ok=True)
            self.assertEqual(cli.main(["status", "--repo", tmp, "job-9999"]), 1)

            create_or_resume_job(jobs_dir, {"number": 55, "title": "T55", "url": "u55"}, max_rounds=3)
            update_job(jobs_dir, "job-55", activity="running:intake", pid=12345)
            halted = advance_stage(jobs_dir, "job-55", "needs-human", last_error="err")
            self.assertIsNone(halted["activity"])
            self.assertIsNone(halted["pid"])


if __name__ == "__main__":
    unittest.main()
