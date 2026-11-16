import concurrent.futures
import tempfile
import unittest

from factory.lib.state import (
    IllegalTransitionError,
    advance_stage,
    create_or_resume_job,
    list_jobs,
    load_job,
    record_usage,
    update_job,
)


class StateTests(unittest.TestCase):
    def test_monotonic_rounds_and_idempotent_mid_slice_resume(self):
        with tempfile.TemporaryDirectory() as d:
            issue = {"number": 42, "title": "Complex auth", "url": "https://x/42"}
            job, action = create_or_resume_job(d, issue, max_rounds=2)
            self.assertEqual((action, job["stage"], job["round"]), ("new", "intake", 0))
            advance_stage(d, "job-42", "spec")
            job = advance_stage(d, "job-42", "build")
            self.assertEqual(job["round"], 1)
            update_job(d, "job-42", total_slices=3, completed_slices=2)
            resumed, action = create_or_resume_job(d, issue, max_rounds=2)
            self.assertEqual((action, resumed["round"], resumed["completed_slices"]), ("resume", 1, 2))

    def test_needs_human_resume_grants_one_extra_round_beyond_exhausted_limit(self):
        with tempfile.TemporaryDirectory() as d:
            issue = {"number": 7, "title": "Feat", "url": "https://x/7"}
            create_or_resume_job(d, issue, max_rounds=1)
            advance_stage(d, "job-7", "spec")
            advance_stage(d, "job-7", "build")   # round=1 == round_limit=1
            advance_stage(d, "job-7", "gate")
            with self.assertRaises(IllegalTransitionError):
                advance_stage(d, "job-7", "build")  # exceeds round_limit=1
            advance_stage(d, "job-7", "needs-human")
            resumed, action = create_or_resume_job(d, issue, max_rounds=1)
            self.assertEqual((action, resumed["stage"], resumed["round"], resumed["round_limit"]), ("resume", "build", 2, 2))

    def test_needs_info_resume_and_terminal_skip(self):
        with tempfile.TemporaryDirectory() as d:
            issue = {"number": 10, "title": "Clarify", "url": "https://x/10"}
            create_or_resume_job(d, issue, max_rounds=2)
            advance_stage(d, "job-10", "needs-info")
            resumed, action = create_or_resume_job(d, issue, max_rounds=2)
            self.assertEqual((action, resumed["stage"]), ("resume", "intake"))
            advance_stage(d, "job-10", "spec")
            advance_stage(d, "job-10", "build")
            advance_stage(d, "job-10", "gate")
            advance_stage(d, "job-10", "review")
            advance_stage(d, "job-10", "approve")
            advance_stage(d, "job-10", "ready")
            _, skip_action = create_or_resume_job(d, issue, max_rounds=2)
            self.assertEqual(skip_action, "skip")
            self.assertEqual(len(list_jobs(d)), 1)

    def test_concurrent_record_usage_preserves_all_updates(self):
        with tempfile.TemporaryDirectory() as d:
            create_or_resume_job(d, {"number": 1, "title": "T", "url": "u"}, max_rounds=3)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(
                    pool.map(
                        lambda _: record_usage(
                            d,
                            "job-1",
                            {"total_tokens": 100, "input_tokens": 60, "output_tokens": 40, "thinking_tokens": 10},
                            1.5,
                        ),
                        range(20),
                    )
                )
            job = load_job(d, "job-1")
            self.assertEqual(job["usage"]["total_tokens"], 2000)
            self.assertAlmostEqual(job["usage"]["duration_seconds"], 30.0)

    def test_needs_human_resumes_at_prerequisite_stage_and_preserves_last_error(self):
        import json, os

        with tempfile.TemporaryDirectory() as d:
            # 1) Escalated during intake (no intake.json) -> must resume at intake, not build
            issue1 = {"number": 11, "title": "Intake fail", "url": "u11"}
            create_or_resume_job(d, issue1, max_rounds=2)
            advance_stage(d, "job-11", "needs-human", last_error="gh timeout during intake")
            r1, act1 = create_or_resume_job(d, issue1, max_rounds=2)
            self.assertEqual((act1, r1["stage"], r1["round"]), ("resume", "intake", 0))

            # 2) Escalated during spec (intake.json exists, no plan.json) -> must resume at spec
            issue2 = {"number": 12, "title": "Spec fail", "url": "u12"}
            create_or_resume_job(d, issue2, max_rounds=2)
            with open(os.path.join(d, "job-12", "intake.json"), "w", encoding="utf-8") as f:
                json.dump({"decision": "ACCEPT"}, f)
            advance_stage(d, "job-12", "spec")
            advance_stage(d, "job-12", "needs-human", last_error="agy timeout during spec")
            r2, act2 = create_or_resume_job(d, issue2, max_rounds=2)
            self.assertEqual((act2, r2["stage"], r2["round"]), ("resume", "spec", 0))

            # 3) Escalated post-spec (plan.json exists) -> resumes at build AND preserves last_error
            issue3 = {"number": 13, "title": "Approve escalate", "url": "u13"}
            create_or_resume_job(d, issue3, max_rounds=2)
            with open(os.path.join(d, "job-13", "intake.json"), "w", encoding="utf-8") as f:
                json.dump({"decision": "ACCEPT"}, f)
            with open(os.path.join(d, "job-13", "plan.json"), "w", encoding="utf-8") as f:
                json.dump({"slices": [{"id": "s1"}]}, f)
            advance_stage(d, "job-13", "spec")
            advance_stage(d, "job-13", "build")
            advance_stage(d, "job-13", "gate")
            advance_stage(d, "job-13", "review")
            advance_stage(d, "job-13", "approve")
            advance_stage(d, "job-13", "needs-human", last_error="Should session rotation revoke mobile tokens?")
            r3, act3 = create_or_resume_job(d, issue3, max_rounds=2)
            self.assertEqual((act3, r3["stage"], r3["round"]), ("resume", "build", 2))
            self.assertIn("revoke mobile tokens", r3["last_error"] or "")

    def test_merging_escalation_with_pr_resumes_at_merging_without_burning_round(self):
        with tempfile.TemporaryDirectory() as d:
            issue = {"number": 516, "title": "Delete tenant", "url": "https://x/516"}
            create_or_resume_job(d, issue, max_rounds=3)
            advance_stage(d, "job-516", "spec")
            advance_stage(d, "job-516", "build")
            advance_stage(d, "job-516", "gate")
            advance_stage(d, "job-516", "review")
            advance_stage(d, "job-516", "approve")
            advance_stage(d, "job-516", "merging")
            update_job(d, "job-516", pr_number=539)
            advance_stage(d, "job-516", "needs-human", last_error="Transient gh error after merge")
            resumed, action = create_or_resume_job(d, issue, max_rounds=3)
            self.assertEqual((action, resumed["stage"], resumed["round"]), ("resume", "merging", 1))

    def test_record_usage_accumulates_cache_read_tokens_and_cost_usd(self):
        with tempfile.TemporaryDirectory() as d:
            job, _ = create_or_resume_job(d, {"number": 88, "title": "Cost", "url": "u"}, max_rounds=2)
            self.assertEqual(job["usage"]["cache_read_tokens"], 0)
            self.assertEqual(job["usage"]["cost_usd"], 0.0)
            record_usage(
                d,
                "job-88",
                {"input_tokens": 100, "cache_read_tokens": 400, "output_tokens": 50, "thinking_tokens": 10, "total_tokens": 150},
                2.5,
                cost_usd=0.125,
            )
            updated = record_usage(
                d,
                "job-88",
                {"input_tokens": 50, "cache_read_tokens": 600, "output_tokens": 25, "thinking_tokens": 5, "total_tokens": 75},
                1.5,
                cost_usd=0.075,
            )
            self.assertEqual(updated["usage"]["cache_read_tokens"], 1000)
            self.assertAlmostEqual(updated["usage"]["cost_usd"], 0.2)

    def test_create_or_resume_job_backfills_minimal_lease_stub(self):
        import json
        import os

        with tempfile.TemporaryDirectory() as d:
            jdir = os.path.join(d, "job-99")
            os.makedirs(jdir, exist_ok=True)
            with open(os.path.join(jdir, "job.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "id": "job-99",
                        "issue": 99,
                        "stage": "intake",
                        "last_worker_id": "m1",
                    },
                    f,
                )
            issue = {"number": 99, "title": "Lease stub issue", "url": "https://x/99"}
            job, action = create_or_resume_job(d, issue, max_rounds=4, max_qa_rounds=2)
            self.assertEqual(action, "resume")
            self.assertEqual(job["id"], "job-99")
            self.assertEqual(job["issue"], 99)
            self.assertEqual(job["issue_title"], "Lease stub issue")
            self.assertEqual(job["issue_url"], "https://x/99")
            self.assertEqual(job["branch"], "factory/job-99")
            self.assertEqual(job["stage"], "intake")
            self.assertEqual(job["round"], 0)
            self.assertEqual(job["round_limit"], 4)
            self.assertEqual(job["qa_round"], 0)
            self.assertEqual(job["qa_round_limit"], 2)
            self.assertEqual(job["risk"], "low")
            self.assertEqual(job["surfaces"], [])
            self.assertEqual(job["total_slices"], 0)
            self.assertEqual(job["completed_slices"], 0)
            self.assertIsNone(job["pr_number"])
            self.assertIsNone(job["pr_url"])
            self.assertEqual(job["prior_prs"], [])
            self.assertIsNone(job["activity"])
            self.assertIsNone(job["pid"])
            self.assertIsNone(job["last_error"])
            self.assertEqual(job["last_worker_id"], "m1")
            self.assertTrue(job["created"])
            self.assertTrue(job["updated"])
            self.assertEqual(
                job["usage"],
                {
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "total_tokens": 0,
                    "duration_seconds": 0.0,
                    "cost_usd": 0.0,
                },
            )

    def test_collect_checkpoint_files_includes_round_artifacts(self):
        import os
        from factory.lib.git_state import GitStateStore

        with tempfile.TemporaryDirectory() as d:
            jdir = os.path.join(d, "job-5")
            rdir = os.path.join(jdir, "round-1")
            os.makedirs(rdir, exist_ok=True)
            expected_rel = [
                "round-1/slice-1-build.json",
                "round-1/slice-1-build.md",
                "round-1/build.json",
                "round-1/build.md",
                "round-1/checks.md",
                "round-1/review-code.json",
                "round-1/review-code.md",
                "round-1/approve.json",
                "round-1/approve.md",
            ]
            for rel in expected_rel:
                with open(os.path.join(jdir, rel), "w", encoding="utf-8") as f:
                    f.write(f"content of {rel}\n")
            store = GitStateStore(d, "m1")
            files = store._collect_checkpoint_files(jdir, {"id": "job-5", "stage": "review"})
            for rel in expected_rel:
                self.assertIn(rel, files)


if __name__ == "__main__":
    unittest.main()
