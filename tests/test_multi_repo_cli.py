"""Unit tests for central engine/repo separation and multi-repo CLI flags."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from factory import cli
from factory.lib.agy_runner import ENGINE_FACTORY_DIR, load_agent_instructions, resolve_agent_path


class MultiRepoCliTests(unittest.TestCase):
    def test_resolve_agent_path_falls_back_to_engine_and_allows_repo_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_factory_dir = os.path.join(tmp, "factory")
            os.makedirs(repo_factory_dir, exist_ok=True)

            # No repo-local agents/ -> falls back to ENGINE_FACTORY_DIR
            resolved = resolve_agent_path(repo_factory_dir, "intake")
            self.assertEqual(resolved, os.path.join(ENGINE_FACTORY_DIR, "agents", "intake.md"))
            self.assertTrue(load_agent_instructions(repo_factory_dir, "intake"))

            # Repo-local override takes precedence
            override_dir = os.path.join(repo_factory_dir, "agents")
            os.makedirs(override_dir, exist_ok=True)
            override_file = os.path.join(override_dir, "intake.md")
            with open(override_file, "w", encoding="utf-8") as f:
                f.write("---\nname: intake\n---\nCustom intake prompt\n")

            self.assertEqual(resolve_agent_path(repo_factory_dir, "intake"), override_file)
            self.assertEqual(load_agent_instructions(repo_factory_dir, "intake"), "Custom intake prompt")

    def test_resolve_target_repos_from_all_and_repo_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "repo-a")
            repo_b = os.path.join(tmp, "repo-b")
            os.makedirs(repo_a)
            os.makedirs(repo_b)
            cfg_path = os.path.join(tmp, "repos.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump([repo_a, repo_b], f)

            with patch.dict(os.environ, {"FACTORY_REPOS_CONFIG": cfg_path}):
                parser = cli._build_parser()
                args_all = parser.parse_args(["status", "--all"])
                self.assertEqual(cli.resolve_target_repos(args_all), [repo_a, repo_b])

                args_explicit = parser.parse_args(["run", "--repo", repo_b])
                self.assertEqual(cli.resolve_target_repos(args_explicit), [repo_b])

    def test_resolve_target_repos_defaults_to_git_toplevel(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", tmp], check=True, capture_output=True)
            sub = os.path.join(tmp, "nested", "dir")
            os.makedirs(sub)
            old_cwd = os.getcwd()
            try:
                os.chdir(sub)
                parser = cli._build_parser()
                args = parser.parse_args(["status"])
                self.assertEqual(cli.resolve_target_repos(args), [os.path.realpath(tmp)])
            finally:
                os.chdir(old_cwd)

    def test_multi_repo_run_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = os.path.join(tmp, "repo-a")
            repo_b = os.path.join(tmp, "repo-b")
            os.makedirs(os.path.join(repo_a, "factory", "jobs"))
            os.makedirs(os.path.join(repo_b, "factory", "jobs"))

            seen_repos = []

            def fake_run_factory(repo_root, issue_numbers=None):
                seen_repos.append((repo_root, issue_numbers))
                return []

            buf = io.StringIO()
            with patch.object(cli, "run_factory", side_effect=fake_run_factory), contextlib.redirect_stdout(buf):
                rc = cli.main(["run", "--repo", repo_a, "--repo", repo_b])

            self.assertEqual(rc, 0)
            self.assertEqual(set(seen_repos), {(repo_a, None), (repo_b, None)})
            out = buf.getvalue()
            self.assertIn("=== repo-a", out)
            self.assertIn("=== repo-b", out)


if __name__ == "__main__":
    unittest.main()
