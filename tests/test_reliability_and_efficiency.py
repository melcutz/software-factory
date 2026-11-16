import json
import os
import subprocess
import tempfile
import unittest

from factory.lib.agy_runner import AgentExecutionError, build_agent_prompt, run_agent
from factory.lib.config import load_config
from factory.lib.gate_runner import (
    auto_polish_worktree,
    compute_pytest_workers,
    select_pre_merge_gate_scope,
)
from factory.lib.git_ops import is_generated_tailwind_path, try_resolve_generated_asset_conflicts
from factory.lib.pipeline import build_auto_approve_payload, should_auto_approve_clean
from factory.lib.schemas import SCHEMAS, validate_stage_output


class ReliabilityAndEfficiencyTests(unittest.TestCase):
    def test_config_validates_new_flags(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(d)
            self.assertTrue(cfg["auto_polish"])
            self.assertTrue(cfg["auto_approve_clean"])
            self.assertEqual(cfg["idle_bg_grace_sec"], 600)

            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"idle_bg_grace_sec": 2}, f)
            with self.assertRaises(ValueError):
                load_config(d)

            with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"auto_polish": "yes"}, f)
            with self.assertRaises(ValueError):
                load_config(d)

    def test_repo_map_injected_except_for_plan_stage(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            agents_dir = os.path.join(d, "agents")
            os.makedirs(agents_dir)
            for name in ("planner", "spec-writer"):
                with open(os.path.join(agents_dir, f"{name}.md"), "w", encoding="utf-8") as f:
                    f.write(f"# Role: {name}\n")
            with open(os.path.join(d, "REPO_MAP.md"), "w", encoding="utf-8") as f:
                f.write("KEY_MODULES: src/app.py\n")

            p_spec = build_agent_prompt(d, "spec", "spec-writer", "Task body")
            self.assertIn("## Repository Architecture Map", p_spec)
            self.assertIn("KEY_MODULES: src/app.py", p_spec)

            p_plan = build_agent_prompt(d, "plan", "planner", "Queue body")
            self.assertNotIn("## Repository Architecture Map", p_plan)

    def test_zombie_background_task_watchdog_kills_process_group_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            factory_dir = os.path.join(d, "factory")
            os.makedirs(os.path.join(factory_dir, "agents"))
            with open(os.path.join(factory_dir, "agents", "intake.md"), "w", encoding="utf-8") as f:
                f.write("# Role: Intake\n")

            wt = os.path.join(d, "wt")
            os.makedirs(wt)
            subprocess.run(["git", "-C", wt, "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", wt, "config", "user.email", "t@e.com"], check=True)
            subprocess.run(["git", "-C", wt, "config", "user.name", "T"], check=True)
            with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
                f.write("ok\n")
            subprocess.run(["git", "-C", wt, "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", wt, "commit", "-m", "init"], check=True, capture_output=True)

            bin_dir = os.path.join(d, "bin")
            os.makedirs(bin_dir)
            state_file = os.path.join(d, "attempt.txt")
            bg_pid_file = os.path.join(d, "bg.pid")
            agy_script = os.path.join(bin_dir, "agy")
            with open(agy_script, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env python3\n"
                    "import json, os, subprocess, sys, time\n"
                    f"sf = {state_file!r}\n"
                    f"pf = {bg_pid_file!r}\n"
                    "n = int(open(sf).read()) + 1 if os.path.exists(sf) else 1\n"
                    "open(sf, 'w').write(str(n))\n"
                    "if n == 1:\n"
                    "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                    "    open(pf, 'w').write(str(child.pid))\n"
                    "    sys.stderr.write('root agent idle; waiting up to 30m0s for 1 background task(s)\\n')\n"
                    "    sys.stderr.flush()\n"
                    "    time.sleep(60)\n"
                    "print(json.dumps({'status': 'SUCCESS', 'structured_output': {\n"
                    "    'decision': 'ACCEPT', 'risk': 'low', 'surfaces': [],\n"
                    "    'summary': ['ok'], 'scope_in': 'a', 'scope_out': '',\n"
                    "    'questions': [], 'reasoning': 'clear'\n"
                    "}, 'usage': {'total_tokens': 10}, 'duration_seconds': 0.1}))\n"
                )
            os.chmod(agy_script, 0o755)

            env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
            job_dir = os.path.join(d, "job-1")
            os.makedirs(job_dir)
            out, _, _ = run_agent(
                factory_dir,
                job_dir,
                wt,
                "intake",
                "intake",
                "gemini-3.8-flash-medium",
                "Check issue",
                SCHEMAS["intake"],
                30,
                sandbox=True,
                extra_env=env,
                idle_bg_grace_sec=0.4,
            )
            self.assertEqual(out["decision"], "ACCEPT")
            with open(bg_pid_file, encoding="utf-8") as pf:
                bg_pid = int(pf.read().strip())
            try:
                with self.assertRaises(ProcessLookupError):
                    os.kill(bg_pid, 0)
            finally:
                try:
                    os.killpg(bg_pid, 9)
                except OSError:
                    pass

    def test_idle_watchdog_unlatches_when_background_task_completes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            factory_dir = os.path.join(d, "factory")
            os.makedirs(os.path.join(factory_dir, "agents"))
            with open(os.path.join(factory_dir, "agents", "intake.md"), "w", encoding="utf-8") as f:
                f.write("# Role: Intake\n")

            wt = os.path.join(d, "wt")
            os.makedirs(wt)
            subprocess.run(["git", "-C", wt, "init", "-b", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", wt, "config", "user.email", "t@e.com"], check=True)
            subprocess.run(["git", "-C", wt, "config", "user.name", "T"], check=True)
            with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
                f.write("ok\n")
            subprocess.run(["git", "-C", wt, "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", wt, "commit", "-m", "init"], check=True, capture_output=True)

            bin_dir = os.path.join(d, "bin")
            os.makedirs(bin_dir)
            state_file = os.path.join(d, "attempt.txt")
            agy_script = os.path.join(bin_dir, "agy")
            with open(agy_script, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env python3\n"
                    "import json, os, subprocess, sys, time\n"
                    f"sf = {state_file!r}\n"
                    "n = int(open(sf).read()) + 1 if os.path.exists(sf) else 1\n"
                    "open(sf, 'w').write(str(n))\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.12)'], start_new_session=True)\n"
                    "sys.stderr.write('root agent idle; waiting up to 30m0s for 1 background task(s)\\n')\n"
                    "sys.stderr.flush()\n"
                    "child.wait()\n"
                    "# Root agent resumes silently on stderr and thinks longer than grace.\n"
                    "time.sleep(0.55)\n"
                    "print(json.dumps({'status': 'SUCCESS', 'structured_output': {\n"
                    "    'decision': 'ACCEPT', 'risk': 'low', 'surfaces': [],\n"
                    "    'summary': ['ok'], 'scope_in': 'a', 'scope_out': '',\n"
                    "    'questions': [], 'reasoning': 'clear'\n"
                    "}, 'usage': {'total_tokens': 10}, 'duration_seconds': 0.7}))\n"
                )
            os.chmod(agy_script, 0o755)

            env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
            job_dir = os.path.join(d, "job-2")
            os.makedirs(job_dir)
            out, _, _ = run_agent(
                factory_dir,
                job_dir,
                wt,
                "intake",
                "intake",
                "gemini-3.8-flash-medium",
                "Check issue",
                SCHEMAS["intake"],
                30,
                sandbox=True,
                extra_env=env,
                idle_bg_grace_sec=0.35,
            )
            self.assertEqual(out["decision"], "ACCEPT")
            with open(state_file, encoding="utf-8") as sf:
                self.assertEqual(int(sf.read().strip()), 1)

    def test_gate_workers_pre_merge_scope_and_fast_approve(self) -> None:
        self.assertEqual(compute_pytest_workers(1, cpu_count=8), 8)
        self.assertEqual(compute_pytest_workers(2, cpu_count=8), 4)
        self.assertEqual(compute_pytest_workers(4, cpu_count=8), 2)

        self.assertEqual(
            select_pre_merge_gate_scope(["src/feature.py"], had_code_conflicts=False),
            "slice",
        )
        self.assertEqual(
            select_pre_merge_gate_scope(["pyproject.toml"], had_code_conflicts=False),
            "full",
        )
        self.assertEqual(
            select_pre_merge_gate_scope(["src/feature.py"], had_code_conflicts=True),
            "full",
        )
        self.assertTrue(is_generated_tailwind_path("src/pkg/static/tailwind-abc123.css"))
        self.assertFalse(is_generated_tailwind_path("src/pkg/app.py"))

        cfg = load_config("/nonexistent-dir-uses-defaults")
        job = {"round": 1, "risk": "low", "surfaces": [], "last_error": None}
        reviews = {"code": {"verdict": "PASS", "findings": [], "summary": ["ok"]}}
        self.assertTrue(should_auto_approve_clean(cfg, job, "VERDICT: PASS\n", reviews))
        payload = build_auto_approve_payload()
        self.assertEqual(validate_stage_output("approve", payload)["decision"], "APPROVE")

        job_r2 = {**job, "round": 2}
        self.assertFalse(should_auto_approve_clean(cfg, job_r2, "VERDICT: PASS\n", reviews))

        job_high = {**job, "risk": "high", "surfaces": []}
        reviews_high = {
            "code": {"verdict": "PASS", "findings": [], "summary": ["ok"]},
            "security": {"verdict": "PASS", "findings": [], "summary": ["ok"]},
            "ux": {"verdict": "PASS", "findings": [], "summary": ["ok"]},
            "ui": {"verdict": "PASS", "findings": [], "summary": ["ok"]},
        }
        self.assertFalse(
            should_auto_approve_clean(cfg, job_high, "VERDICT: PASS\n", reviews_high)
        )

    def test_tailwind_conflict_auto_resolver_and_auto_polish_guard(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bare = os.path.join(d, "origin.git")
            wt = os.path.join(d, "wt")
            subprocess.run(["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True)
            subprocess.run(["git", "clone", bare, wt], check=True, capture_output=True)
            subprocess.run(["git", "-C", wt, "config", "user.email", "t@e.com"], check=True)
            subprocess.run(["git", "-C", wt, "config", "user.name", "T"], check=True)

            os.makedirs(os.path.join(wt, "scripts"), exist_ok=True)
            os.makedirs(os.path.join(wt, "src", "web", "static"), exist_ok=True)
            with open(os.path.join(wt, "scripts", "build_tailwind.py"), "w", encoding="utf-8") as f:
                f.write(
                    "import glob, hashlib, os\n"
                    "for p in glob.glob('src/web/static/tailwind-*.css'): os.unlink(p)\n"
                    "h = hashlib.sha256(open('input.txt', 'rb').read()).hexdigest()[:8]\n"
                    "open(f'src/web/static/tailwind-{h}.css', 'w').write('body{}\\n')\n"
                )
            with open(os.path.join(wt, "input.txt"), "w", encoding="utf-8") as f:
                f.write("base\n")
            with open(os.path.join(wt, "src", "web", "static", "tailwind-0000.css"), "w", encoding="utf-8") as f:
                f.write("base\n")
            subprocess.run(["git", "-C", wt, "add", "."], check=True)
            subprocess.run(["git", "-C", wt, "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", wt, "push", "-u", "origin", "main"], check=True, capture_output=True)

            subprocess.run(["git", "-C", wt, "checkout", "-b", "feat"], check=True, capture_output=True)
            with open(os.path.join(wt, "input.txt"), "w", encoding="utf-8") as f:
                f.write("base\nfeat\n")
            with open(os.path.join(wt, "src", "web", "static", "tailwind-0000.css"), "w", encoding="utf-8") as f:
                f.write("feat-css\n")
            subprocess.run(["git", "-C", wt, "commit", "-am", "feat"], check=True, capture_output=True)

            other = os.path.join(d, "other")
            subprocess.run(["git", "clone", bare, other], check=True, capture_output=True)
            subprocess.run(["git", "-C", other, "config", "user.email", "t@e.com"], check=True)
            subprocess.run(["git", "-C", other, "config", "user.name", "T"], check=True)
            with open(os.path.join(other, "src", "web", "static", "tailwind-0000.css"), "w", encoding="utf-8") as f:
                f.write("main-css\n")
            subprocess.run(["git", "-C", other, "commit", "-am", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", other, "push", "origin", "main"], check=True, capture_output=True)

            subprocess.run(["git", "-C", wt, "fetch", "origin", "main"], check=True, capture_output=True)
            mres = subprocess.run(["git", "-C", wt, "merge", "--no-edit", "origin/main"], capture_output=True)
            self.assertNotEqual(mres.returncode, 0)
            self.assertTrue(try_resolve_generated_asset_conflicts(wt))
            self.assertFalse(auto_polish_worktree(wt, "main", ["factory/"]))

    def test_config_max_job_cost_usd_validation(self) -> None:
        from factory.lib.config import DEFAULT_CONFIG, load_config

        self.assertEqual(DEFAULT_CONFIG.get("max_job_cost_usd"), 0.0)
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write('{"max_job_cost_usd": 2.5}')
            loaded = load_config(d)
            self.assertEqual(loaded["max_job_cost_usd"], 2.5)
            for bad in ('{"max_job_cost_usd": -0.5}', '{"max_job_cost_usd": true}', '{"max_job_cost_usd": "1"}'):
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(bad)
                with self.assertRaises(ValueError):
                    load_config(d)

    def test_select_builder_model_tiering(self) -> None:
        from factory.lib.pipeline import select_builder_model

        cfg = {"models": {"builder": "gemini-3.8-flash-medium", "builder_low": "gemini-3.8-flash-low"}}
        low_job = {"risk": "low"}
        med_job = {"risk": "medium"}
        self.assertEqual(select_builder_model(cfg, low_job, attempt=0), "gemini-3.8-flash-low")
        self.assertEqual(select_builder_model(cfg, low_job, attempt=1), "gemini-3.8-flash-medium")
        self.assertEqual(select_builder_model(cfg, low_job, attempt=0, is_fix_round=True), "gemini-3.8-flash-medium")
        self.assertEqual(select_builder_model(cfg, low_job, attempt=0, is_conflict=True), "gemini-3.8-flash-medium")
        self.assertEqual(select_builder_model(cfg, med_job, attempt=0), "gemini-3.8-flash-medium")
        cfg_no_low = {"models": {"builder": "gemini-3.8-flash-medium"}}
        self.assertEqual(select_builder_model(cfg_no_low, low_job, attempt=0), "gemini-3.8-flash-medium")

    def test_spec_protected_path_check_and_cost_circuit_breaker(self) -> None:
        from unittest.mock import patch
        from factory.lib.pipeline import process_job

        with tempfile.TemporaryDirectory() as d:
            bare = os.path.join(d, "origin.git")
            repo = os.path.join(d, "repo")
            subprocess.run(["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True)
            subprocess.run(["git", "clone", bare, repo], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "config", "user.email", "t@e.com"], check=True)
            subprocess.run(["git", "-C", repo, "config", "user.name", "T"], check=True)
            os.makedirs(os.path.join(repo, "factory"), exist_ok=True)
            with open(os.path.join(repo, "factory", "config.json"), "w", encoding="utf-8") as f:
                f.write(
                    '{"allow_self_host": true, "protected_paths": ["factory/checks.sh"], '
                    '"max_job_cost_usd": 0.50}'
                )
            with open(os.path.join(repo, "factory", "checks.sh"), "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(os.path.join(repo, "factory", "checks.sh"), 0o755)
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", repo, "push", "-u", "origin", "main"], check=True, capture_output=True)

            class _FakeGH:
                def __init__(self):
                    self.comments = []
                def fetch_issue(self, num):
                    return (
                        {"number": num, "title": "T", "url": f"https://x/{num}", "body": "b", "comments": [], "labels": []},
                        f"# Issue #{num}\nb\n",
                    )
                def comment_issue(self, num, body):
                    self.comments.append(body)
                def edit_labels(self, *a, **kw):
                    pass

            gh = _FakeGH()
            calls = []

            def fake_agent(_fdir, _jdir, _wt, step, _role, _model, prompt, *_a, **_kw):
                calls.append((step, prompt))
                usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
                if step == "intake":
                    return ({"decision": "ACCEPT", "risk": "low", "surfaces": [], "summary": ["ok"], "scope_in": "in", "scope_out": "out", "questions": [], "reasoning": "ok"}, usage, 0.01)
                if step == "spec":
                    return (
                        {
                            "summary": ["s"], "problem": "p", "solution": "s", "user_stories": ["u"],
                            "implementation_decisions": ["d"], "testing_decisions": ["t"],
                            "glossary_updates": [], "out_of_scope": [],
                            "slices": [{"id": "s1", "title": "S1", "goal": "g", "files": ["factory/checks.sh"], "test_seams": ["t"], "acceptance_criteria": ["a"]}],
                        },
                        usage,
                        0.01,
                    )
                raise AssertionError(f"unexpected step {step}")

            with patch("factory.lib.pipeline.run_agent", side_effect=fake_agent), \
                 patch("factory.lib.pipeline.ProjectBoardSync.discover", return_value=None):
                res = process_job(repo, 41, gh=gh)
            self.assertEqual(res["stage"], "needs-human")
            spec_calls = [p for s, p in calls if s == "spec"]
            self.assertEqual(len(spec_calls), 2)
            self.assertIn("factory/checks.sh", spec_calls[1])
            self.assertFalse(any(s == "build" for s, _ in calls))

            # Now test max_job_cost_usd circuit breaker before launching a new agent step
            from factory.lib.state import create_or_resume_job, record_usage
            create_or_resume_job(
                os.path.join(repo, "factory", "jobs"),
                {"number": 42, "title": "Cost test", "url": "https://x/42"},
                3,
            )
            record_usage(os.path.join(repo, "factory", "jobs"), "job-42", {"total_tokens": 100}, 1.0, cost_usd=0.50)
            calls.clear()
            with patch("factory.lib.pipeline.run_agent", side_effect=fake_agent), \
                 patch("factory.lib.pipeline.ProjectBoardSync.discover", return_value=None):
                res42 = process_job(repo, 42, gh=gh)
            self.assertEqual(res42["stage"], "needs-human")
            self.assertEqual(calls, [])
            self.assertIn("0.50", res42.get("last_error") or "")


if __name__ == "__main__":
    unittest.main()
