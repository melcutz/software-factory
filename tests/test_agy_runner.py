import os
import subprocess
import tempfile
import unittest

from factory.lib.agy_runner import (
     SUBAGENT_PREAMBLE,
    AgentExecutionError,
    _extract_json_envelope,
    load_agent_instructions,
    run_agent,
)
from factory.lib.git_ops import is_worktree_clean
from factory.lib.schemas import SCHEMAS


class AgyRunnerTests(unittest.TestCase):
    def _init_wt(self, tmp: str) -> str:
        wt = os.path.join(tmp, "wt")
        os.makedirs(wt)
        subprocess.run(["git", "init", "-b", "main", wt], check=True, capture_output=True)
        subprocess.run(["git", "-C", wt, "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)
        return wt

    def test_run_agent_recovers_from_leading_stdout_noise_and_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            counter = os.path.join(tmp, "calls")
            fake_agy = os.path.join(bin_dir, "agy")
            with open(fake_agy, "w", encoding="utf-8") as f:
                f.write(
                    f"""#!/usr/bin/env python3
import json, os, sys
assert "--mode" not in sys.argv
assert "--sandbox" in sys.argv
n = int(open({counter!r}).read()) + 1 if os.path.exists({counter!r}) else 1
open({counter!r}, "w").write(str(n))
if n == 1:
    print("broken output")
    sys.exit(1)
print("⠋ Thinking...")
print(json.dumps({{"status": "SUCCESS", "duration_seconds": 2.5,
    "usage": {{"total_tokens": 50, "input_tokens": 30, "output_tokens": 20, "thinking_tokens": 5}},
    "structured_output": {{"decision": "APPROVE", "summary": ["ok"], "reasoning": "safe", "merge_danger": "low", "question": ""}}}}))
"""
                )
            os.chmod(fake_agy, 0o755)
            wt = self._init_wt(tmp)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{old_path}"
            try:
                out, usage, dur = run_agent(
                    os.path.abspath("factory"),
                    os.path.join(tmp, "job-1"),
                    wt,
                    "approve",
                    "approver",
                    "gemini-4-argon-high",
                    "Check PR",
                    SCHEMAS["approve"],
                    30,
                    sandbox=True,
                    log_name="r1-approve",
                )
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual(out["decision"], "APPROVE")
            self.assertEqual(usage["total_tokens"], 50)
            self.assertAlmostEqual(dur, 2.5)
            self.assertTrue(os.path.exists(os.path.join(tmp, "job-1", "logs", "r1-approve.json")))

    def test_sandbox_cleans_stray_files_and_rejects_head_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            fake_agy = os.path.join(bin_dir, "agy")
            with open(fake_agy, "w", encoding="utf-8") as f:
                f.write(
                    """#!/usr/bin/env python3
import json, subprocess
open("stray.txt", "w").write("oops")
subprocess.run(["git", "add", "stray.txt"], check=True)
subprocess.run(["git", "commit", "-m", "illegal commit"], check=True, capture_output=True)
print(json.dumps({"status": "SUCCESS", "duration_seconds": 1.0, "usage": {},
    "structured_output": {"decision": "APPROVE", "summary": ["ok"], "reasoning": "r", "merge_danger": "m", "question": ""}}))
"""
                )
            os.chmod(fake_agy, 0o755)
            wt = self._init_wt(tmp)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}:{old_path}"
            try:
                with self.assertRaises(AgentExecutionError):
                    run_agent(
                        os.path.abspath("factory"),
                        os.path.join(tmp, "job-2"),
                        wt,
                        "approve",
                        "approver",
                        "gemini-4-argon-high",
                        "Check PR",
                        SCHEMAS["approve"],
                        30,
                        sandbox=True,
                        log_name="r1-approve",
                    )
            finally:
                os.environ["PATH"] = old_path
            self.assertTrue(is_worktree_clean(wt))

    def test_all_headless_agent_prompts_avoid_interactive_questions_and_file_writes(self):
        self.assertIn("<SUBAGENT-STOP>", SUBAGENT_PREAMBLE)
        for role in [
            "intake",
            "spec-writer",
            "builder",
            "reviewer-code",
            "reviewer-security",
            "reviewer-ux",
            "reviewer-ui",
            "approver",
        ]:
            body = load_agent_instructions(os.path.abspath("factory"), role)
            self.assertTrue(len(body) > 80, role)
            self.assertNotIn("Which is it?", body)
            self.assertNotIn("Reply with exactly one line:", body)

    def test_sandbox_detects_head_mutation_across_timeout_retry(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            wt = self._init_wt(tmp)
            real_run = subprocess.run
            calls = {"agy": 0}

            def side_effect(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd and cmd[0] == "agy":
                    calls["agy"] += 1
                    if calls["agy"] == 1:
                        with open(os.path.join(wt, "sneaky.txt"), "w", encoding="utf-8") as f:
                            f.write("pwn\n")
                        real_run(["git", "-C", wt, "add", "sneaky.txt"], check=True)
                        real_run(["git", "-C", wt, "commit", "-m", "sneaky"], check=True, capture_output=True)
                        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout='{"status":"SUCCESS","duration_seconds":0.1,"usage":{},"structured_output":{"decision":"APPROVE","summary":["ok"],"reasoning":"r","merge_danger":"m","question":""}}',
                        stderr="",
                    )
                return real_run(cmd, *args, **kwargs)

            with patch("factory.lib.agy_runner.subprocess.run", side_effect=side_effect):
                with self.assertRaises(AgentExecutionError):
                    run_agent(
                        os.path.abspath("factory"),
                        os.path.join(tmp, "job-9"),
                        wt,
                        "approve",
                        "approver",
                        "gemini-4-argon-high",
                        "Check PR",
                        SCHEMAS["approve"],
                        1,
                        sandbox=True,
                        log_name="r1-approve",
                    )
            self.assertFalse(os.path.exists(os.path.join(wt, "sneaky.txt")))

    def test_extract_multiline_pretty_json_envelope_after_spinner_noise(self):
        raw = (
            "⠋ Thinking about architecture...\n"
            "{\n"
            '  "status": "SUCCESS",\n'
            '  "duration_seconds": 1.2,\n'
            '  "structured_output": {\n'
            '    "decision": "APPROVE"\n'
            "  }\n"
            "}\n"
        )
        parsed = _extract_json_envelope(raw)
        self.assertEqual(parsed["status"], "SUCCESS")
        self.assertEqual(parsed["structured_output"]["decision"], "APPROVE")


if __name__ == "__main__":
    unittest.main()
