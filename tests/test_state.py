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


if __name__ == "__main__":
    unittest.main()
