"""Unit tests for queue dependency DAG planner, topological scheduling, and multi-issue CLI."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

from factory.lib.planner import build_dependency_dag, parse_explicit_dependencies
from factory.lib.pipeline import run_factory


def _load_cli_module():
    cli_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "factory", "bin", "factory"))
    loader = importlib.machinery.SourceFileLoader("factory_cli_mod", cli_path)
    spec = importlib.util.spec_from_loader("factory_cli_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


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
                results = run_factory(tmp, issue_numbers=[10, 11, 12])

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


if __name__ == "__main__":
    unittest.main()
