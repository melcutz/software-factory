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

    def test_clean_dry_run_and_live_execution(self):
        import json
        import subprocess
        from factory.lib.git_ops import ensure_worktree

        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin.git")
            repo = os.path.join(tmp, "repo")
            subprocess.run(["git", "init", "--bare", "-b", "main", origin], check=True, capture_output=True)
            subprocess.run(["git", "clone", origin, repo], check=True, capture_output=True)
            os.makedirs(os.path.join(repo, "factory", "jobs"), exist_ok=True)
            with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
                f.write("hi\n")
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "push", "-u", "origin", "main"], check=True, capture_output=True)

            wt_merged, _ = ensure_worktree(repo, "job-1", "main")
            wt_ready, _ = ensure_worktree(repo, "job-2", "main")
            for jid, stage in [("job-1", "merged"), ("job-2", "ready")]:
                jdir = os.path.join(repo, "factory", "jobs", jid)
                os.makedirs(jdir, exist_ok=True)
                with open(os.path.join(jdir, "job.json"), "w", encoding="utf-8") as f:
                    json.dump({"job_id": jid, "stage": stage}, f)

            buf_dry = io.StringIO()
            with contextlib.redirect_stdout(buf_dry):
                rc_dry = cli.main(["clean", "--repo", repo, "--dry-run"])
            self.assertEqual(rc_dry, 0)
            self.assertIn("job-1", buf_dry.getvalue())
            self.assertNotIn("job-2", buf_dry.getvalue())
            self.assertTrue(os.path.isdir(wt_merged))
            self.assertTrue(os.path.isdir(wt_ready))

            buf_live = io.StringIO()
            with contextlib.redirect_stdout(buf_live):
                rc_live = cli.main(["clean", "--repo", repo])
            self.assertEqual(rc_live, 0)
            self.assertIn("job-1", buf_live.getvalue())
            self.assertFalse(os.path.exists(wt_merged))
            self.assertTrue(os.path.isdir(wt_ready))

    def test_init_map_and_learn_cli(self):
        import json
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "demo-app")
            os.makedirs(os.path.join(repo, "src"), exist_ok=True)
            os.makedirs(os.path.join(repo, "tests"), exist_ok=True)
            with open(os.path.join(repo, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write('[project]\nname = "demo-app"\n')
            with open(os.path.join(repo, "src", "app.py"), "w", encoding="utf-8") as f:
                f.write("def hi(): return 'ok'\n")
            with open(os.path.join(repo, "tests", "test_app.py"), "w", encoding="utf-8") as f:
                f.write("def test_hi(): assert True\n")
            subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "add", "."], check=True)

            buf_map = io.StringIO()
            with contextlib.redirect_stdout(buf_map):
                rc_map = cli.main(["map", "--repo", repo, "--stdout"])
            self.assertEqual(rc_map, 0)
            map_out = buf_map.getvalue()
            self.assertIn("pyproject.toml", map_out)
            self.assertLess(len(map_out.splitlines()), 100)

            with contextlib.redirect_stdout(io.StringIO()):
                rc_init = cli.main(["init", "--repo", repo])
            self.assertEqual(rc_init, 0)
            cfg_path = os.path.join(repo, "factory", "config.json")
            checks_path = os.path.join(repo, "factory", "checks.sh")
            gi_path = os.path.join(repo, ".gitignore")
            self.assertTrue(os.path.isfile(cfg_path))
            self.assertTrue(os.access(checks_path, os.X_OK))
            self.assertTrue(os.path.isfile(os.path.join(repo, "factory", "REPO_MAP.md")))
            with open(cfg_path, encoding="utf-8") as f:
                self.assertFalse(json.load(f)["allow_self_host"])
            with open(gi_path, encoding="utf-8") as f:
                gi_txt = f.read()
            self.assertIn("/factory/jobs/*", gi_txt)
            self.assertIn("!/factory/jobs/.gitkeep", gi_txt)
            self.assertIn("/.worktrees/", gi_txt)

            # Preserves existing config without --force, overwrites with --force
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"allow_self_host": True}, f)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["init", "--repo", repo])
            with open(cfg_path, encoding="utf-8") as f:
                self.assertTrue(json.load(f)["allow_self_host"])
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["init", "--repo", repo, "--force"])
            with open(cfg_path, encoding="utf-8") as f:
                self.assertFalse(json.load(f)["allow_self_host"])

            # factory learn extracts & deduplicates PITFALLS.md
            jdir = os.path.join(repo, "factory", "jobs", "job-10")
            os.makedirs(os.path.join(jdir, "round-1"), exist_ok=True)
            with open(os.path.join(jdir, "job.json"), "w", encoding="utf-8") as f:
                json.dump({"id": "job-10", "issue": 10, "stage": "merged", "round": 2, "qa_round": 0}, f)
            with open(os.path.join(jdir, "round-1", "review-code.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "verdict": "FAIL",
                        "summary": ["Mutable default"],
                        "findings": [{"severity": "high", "message": "Avoid mutable default arguments in helpers"}],
                    },
                    f,
                )
            with contextlib.redirect_stdout(io.StringIO()):
                rc_learn1 = cli.main(["learn", "job-10", "--repo", repo])
                rc_learn2 = cli.main(["learn", "job-10", "--repo", repo])
            self.assertEqual(rc_learn1, 0)
            self.assertEqual(rc_learn2, 0)
            pitfalls_path = os.path.join(repo, "factory", "PITFALLS.md")
            with open(pitfalls_path, encoding="utf-8") as f:
                pit_txt = f.read()
            self.assertEqual(pit_txt.count("Avoid mutable default arguments in helpers"), 1)

    def test_stats_logs_and_patrol_cli(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "obs-repo")
            fdir = os.path.join(repo, "factory")
            jdir1 = os.path.join(fdir, "jobs", "job-1")
            jdir2 = os.path.join(fdir, "jobs", "job-2")
            os.makedirs(os.path.join(jdir2, "round-1"), exist_ok=True)
            os.makedirs(jdir1, exist_ok=True)

            with open(os.path.join(jdir1, "job.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": "job-1",
                        "issue": 1,
                        "stage": "merged",
                        "round": 1,
                        "usage": {"input_tokens": 100, "cache_read_tokens": 100, "output_tokens": 20, "duration_seconds": 10.0, "cost_usd": 0.4},
                        "metrics": {"gate_duration_seconds": 2.0, "deploy_duration_seconds": 3.0},
                    },
                    f,
                )
            with open(os.path.join(jdir2, "job.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": "job-2",
                        "issue": 2,
                        "stage": "needs-human",
                        "round": 3,
                        "last_error": "Exceeded retries",
                        "usage": {"input_tokens": 200, "cache_read_tokens": 0, "output_tokens": 50, "duration_seconds": 20.0, "cost_usd": 0.8},
                        "metrics": {"gate_duration_seconds": 5.0, "deploy_duration_seconds": 0.0},
                    },
                    f,
                )
            with open(os.path.join(jdir2, "round-1", "checks.md"), "w", encoding="utf-8") as f:
                f.write("VERDICT: FAIL\nFAILED test_foo\n")
            with open(os.path.join(jdir2, "round-1", "review-code.json"), "w", encoding="utf-8") as f:
                json.dump({"verdict": "FAIL", "findings": [{"severity": "high", "message": "Missing check"}]}, f)
            with open(os.path.join(jdir2, "qa-failure.md"), "w", encoding="utf-8") as f:
                f.write("QA failed on /login\n")

            # factory stats --json and table
            buf_json = io.StringIO()
            with contextlib.redirect_stdout(buf_json):
                rc_s = cli.main(["stats", "--repo", repo, "--json"])
            self.assertEqual(rc_s, 0)
            stats = json.loads(buf_json.getvalue())
            self.assertEqual(stats["counts_by_stage"]["merged"], 1)
            self.assertEqual(stats["counts_by_stage"]["needs-human"], 1)
            self.assertAlmostEqual(stats["merge_rate"], 0.5)
            self.assertAlmostEqual(stats["median_merged_cost_usd"], 0.4)

            buf_tbl = io.StringIO()
            with contextlib.redirect_stdout(buf_tbl):
                rc_t = cli.main(["stats", "--repo", repo])
            self.assertEqual(rc_t, 0)
            self.assertIn("Merge rate", buf_tbl.getvalue())

            # factory logs
            buf_logs = io.StringIO()
            with contextlib.redirect_stdout(buf_logs):
                rc_l = cli.main(["logs", "job-2", "--repo", repo, "--tail", "10"])
            self.assertEqual(rc_l, 0)
            logs_out = buf_logs.getvalue()
            self.assertIn("FAILED test_foo", logs_out)
            self.assertIn("Missing check", logs_out)
            self.assertIn("QA failed on /login", logs_out)

            with contextlib.redirect_stderr(io.StringIO()):
                rc_missing = cli.main(["logs", "job-999", "--repo", repo])
            self.assertNotEqual(rc_missing, 0)

            # factory patrol with hook + built-in + deduplication
            with open(os.path.join(fdir, "patrol_hooks.py"), "w", encoding="utf-8") as f:
                f.write(
                    "def collect_patrol_findings(repo_root):\n"
                    "    return [\n"
                    "        {'fingerprint': 'dup-1', 'title': 'Already open', 'body': 'skip me'},\n"
                    "        {'fingerprint': 'new-1', 'title': 'New finding', 'body': 'open me'},\n"
                    "    ]\n"
                )

            created_issues = []

            class _FakePatrolGH:
                def list_open_issues(self):
                    return [{"number": 50, "title": "Existing", "body": "[patrol:dup-1]"}]
                def create_issue(self, title, body, labels=("factory",)):
                    created_issues.append((title, body, tuple(labels)))
                    return {"number": 51, "url": "https://x/51"}

            from factory.lib import patrol as patrol_mod
            with patch.object(patrol_mod, "GitHubClient", return_value=_FakePatrolGH()):
                buf_dry = io.StringIO()
                with contextlib.redirect_stdout(buf_dry):
                    rc_pd = cli.main(["patrol", "--repo", repo, "--dry-run"])
                self.assertEqual(rc_pd, 0)
                self.assertEqual(created_issues, [])
                self.assertIn("new-1", buf_dry.getvalue())

                buf_live = io.StringIO()
                with contextlib.redirect_stdout(buf_live):
                    rc_pl = cli.main(["patrol", "--repo", repo])
                self.assertEqual(rc_pl, 0)
                self.assertTrue(any("[patrol:new-1]" in body for _, body, _ in created_issues))
                self.assertFalse(any("[patrol:dup-1]" in body for _, body, _ in created_issues))


if __name__ == "__main__":
    unittest.main()
