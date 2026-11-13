import json
import os
import shutil
import subprocess
import tempfile
import unittest

from factory.lib.git_ops import is_worktree_clean
from factory.lib.pipeline import format_status_table, process_job, run_doctor, run_factory
from factory.lib.state import load_job


FAKE_GH_SCRIPT = r"""#!/usr/bin/env python3
import json, os, sys, fcntl

STATE_FILE = os.environ["FAKE_GH_STATE"]

def with_state(fn):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    lock_path = STATE_FILE + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        data = json.load(open(STATE_FILE))
        res = fn(data)
        with open(STATE_FILE, "w") as sf:
            json.dump(data, sf, indent=2)
        return res

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print("Logged in")
    sys.exit(0)

if args[:2] == ["issue", "list"]:
    lbl = args[args.index("--label") + 1]
    def _list(d):
        out = []
        for iss in d["issues"].values():
            if iss["state"] == "open" and lbl in iss["labels"]:
                out.append({
                    "number": iss["number"],
                    "title": iss["title"],
                    "url": iss["url"],
                    "labels": [{"name": x} for x in iss["labels"]],
                })
        return out
    print(json.dumps(with_state(_list)))
    sys.exit(0)

if args[:2] == ["issue", "view"]:
    num = args[2]
    def _view(d):
        iss = d["issues"][num]
        return {
            "number": iss["number"],
            "title": iss["title"],
            "body": iss.get("body"),
            "url": iss["url"],
            "state": iss["state"],
            "labels": [{"name": x} for x in iss["labels"]],
            "comments": iss.get("comments", []),
        }
    print(json.dumps(with_state(_view)))
    sys.exit(0)

if args[:2] == ["issue", "comment"]:
    num = args[2]
    body = args[args.index("--body") + 1]
    def _comment(d):
        d["issues"][num].setdefault("comments", []).append({"author": {"login": "factory-bot"}, "body": body})
    with_state(_comment)
    sys.exit(0)

if args[:2] == ["issue", "edit"]:
    num = args[2]
    add, rem = [], []
    i = 3
    while i < len(args):
        if args[i] == "--add-label":
            add.append(args[i + 1]); i += 2
        elif args[i] == "--remove-label":
            rem.append(args[i + 1]); i += 2
        else:
            i += 1
    def _edit(d):
        cur = set(d["issues"][num]["labels"])
        cur -= set(rem)
        cur |= set(add)
        d["issues"][num]["labels"] = sorted(cur)
    with_state(_edit)
    sys.exit(0)

if args[:2] == ["issue", "close"]:
    num = args[2]
    def _close(d):
        d["issues"][num]["state"] = "closed"
    with_state(_close)
    sys.exit(0)

if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    def _pr_list(d):
        return [{"number": p["number"], "url": p["url"]} for p in d["prs"].values() if p["head"] == head and p["state"] == "open"]
    print(json.dumps(with_state(_pr_list)))
    sys.exit(0)

if args[:2] == ["pr", "create"]:
    head = args[args.index("--head") + 1]
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]
    draft = "--draft" in args
    def _pr_create(d):
        n = len(d["prs"]) + 100
        pr = {"number": n, "url": f"https://example.test/pr/{n}", "head": head, "title": title, "body": body, "draft": draft, "state": "open", "merged": False}
        d["prs"][str(n)] = pr
        return pr
    pr = with_state(_pr_create)
    print(pr["url"])
    sys.exit(0)

if args[:2] == ["pr", "view"]:
    ref = args[2]
    def _pr_view(d):
        if ref in d["prs"]:
            return d["prs"][ref]
        for p in d["prs"].values():
            if p["head"] == ref:
                return p
        raise KeyError(ref)
    p = with_state(_pr_view)
    print(json.dumps({"number": p["number"], "url": p["url"]}))
    sys.exit(0)

if args[:2] == ["pr", "edit"]:
    num = args[2]
    title = args[args.index("--title") + 1]
    body = args[args.index("--body") + 1]
    def _pr_edit(d):
        d["prs"][num]["title"] = title
        d["prs"][num]["body"] = body
    with_state(_pr_edit)
    sys.exit(0)

if args[:2] == ["pr", "ready"]:
    num = args[2]
    def _pr_ready(d):
        d["prs"][num]["draft"] = False
    with_state(_pr_ready)
    sys.exit(0)

if args[:2] == ["pr", "merge"]:
    num = args[2]
    def _pr_merge(d):
        assert not d["prs"][num]["draft"], "Cannot merge draft PR!"
        d["prs"][num]["merged"] = True
        d["prs"][num]["state"] = "merged"
    with_state(_pr_merge)
    sys.exit(0)

raise SystemExit(f"Unhandled gh args: {args}")
"""


FAKE_AGY_SCRIPT = r"""#!/usr/bin/env python3
import json, os, re, subprocess, sys, fcntl

if sys.argv[1:] == ["--version"]:
    print("agy 2.0.0")
    sys.exit(0)

STATE_FILE = os.environ["FAKE_AGY_STATE"]
prompt = sys.argv[sys.argv.index("-p") + 1]

def bump(key):
    lock_path = STATE_FILE + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        data = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
        data[key] = data.get(key, 0) + 1
        with open(STATE_FILE, "w") as sf:
            json.dump(data, sf, indent=2)
        return data[key], data

m_job = re.search(r"JOB_ID:\s*(job-\d+)", prompt)
job_id = m_job.group(1) if m_job else "unknown"

def respond(obj):
    print(json.dumps({
        "status": "SUCCESS",
        "duration_seconds": 1.0,
        "usage": {"input_tokens": 100, "output_tokens": 50, "thinking_tokens": 10, "total_tokens": 150},
        "structured_output": obj,
    }))
    sys.exit(0)

if "# Role: Factory Intake" in prompt:
    c, _ = bump(f"{job_id}:intake")
    if job_id == "job-40" and "Human answer:" not in prompt:
        respond({
            "decision": "NEEDS_INFO",
            "risk": "low",
            "surfaces": [],
            "summary": ["Need retention policy"],
            "scope_in": "",
            "scope_out": "",
            "questions": ["Should expired sessions be hard-deleted or archived?"],
            "reasoning": "Product retention policy unspecified.",
        })
    if job_id == "job-10":
        respond({
            "decision": "ACCEPT",
            "risk": "medium",
            "surfaces": ["ui", "security"],
            "summary": ["Build 3-slice complex dashboard auth feature"],
            "scope_in": "Auth service, UI view, audit log",
            "scope_out": "SSO",
            "questions": [],
            "reasoning": "Clear multi-part feature.",
        })
    respond({
        "decision": "ACCEPT",
        "risk": "low",
        "surfaces": [],
        "summary": ["Small deterministic fix"],
        "scope_in": "Single module fix",
        "scope_out": "None",
        "questions": [],
        "reasoning": "Low risk fix.",
    })

if "# Role: Factory Spec Writer" in prompt:
    bump(f"{job_id}:spec")
    if job_id == "job-10":
        slices = [
            {"id": f"s{i}", "title": f"Slice {i}", "goal": f"Deliver slice {i}", "files": [f"slice_{i}.py"], "test_seams": [f"run_s{i}()"], "acceptance_criteria": [f"slice {i} works"]}
            for i in (1, 2, 3)
        ]
    elif job_id == "job-30":
        slices = [
            {"id": f"s{i}", "title": f"Slice {i}", "goal": f"Deliver slice {i}", "files": [f"part_{i}.py"], "test_seams": [f"part_{i}()"], "acceptance_criteria": [f"part {i} works"]}
            for i in (1, 2)
        ]
    else:
        slices = [
            {"id": "s1", "title": "Slice 1", "goal": "Single fix", "files": ["fix.py"], "test_seams": ["fix()"], "acceptance_criteria": ["passes"]}
        ]
    respond({
        "summary": ["Spec ready"],
        "problem": "Problem",
        "solution": "Solution",
        "user_stories": ["As a user, it works"],
        "implementation_decisions": ["Keep modules deep"],
        "testing_decisions": ["Test public seams"],
        "glossary_updates": ["Session: authenticated user context"],
        "out_of_scope": ["Extras"],
        "slices": slices,
    })

if "# Role: Factory Builder" in prompt:
    c, _ = bump(f"{job_id}:build")
    # Simulate mid-slice crash on job-30 when starting slice 2 for the first time
    if job_id == "job-30" and "SLICE_INDEX: 2/2" in prompt and c == 2:
        sys.stderr.write("Simulated transient crash on slice 2\n")
        # Fail both retry attempts in run_agent so process_job halts/raises or check crash behavior
    if job_id == "job-30" and "SLICE_INDEX: 2/2" in prompt:
        crash_flag = os.environ.get("CRASH_JOB_30_SLICE_2", "")
        if crash_flag == "1":
            sys.exit(2)

    # For job-10 slice 2 first attempt, write a file that fails checks.sh ("FAIL_CHECK")
    if job_id == "job-10" and "SLICE_INDEX: 2/3" in prompt and c == 2:
        open("bad_check.txt", "w").write("break checks.sh\n")
        subprocess.run(["git", "add", "bad_check.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "wip broken slice 2"], check=True, capture_output=True)
        respond({"verdict": "DONE", "summary": ["Slice 2 attempt 1"], "changes": ["bad_check.txt"], "tests_added": ["t"], "blocked_reason": "", "notes": ""})

    if os.path.exists("bad_check.txt"):
        os.remove("bad_check.txt")

    if job_id == "job-50":
        # Modify ok.txt so we can trigger a merge conflict with origin/main during merging!
        open("ok.txt", "w").write("branch-edit-50\n")
        subprocess.run(["git", "add", "ok.txt"], check=True)
        subprocess.run(["git", "commit", "-m", "edit ok.txt on job-50"], check=True, capture_output=True)
        respond({"verdict": "DONE", "summary": ["Edited ok.txt"], "changes": ["ok.txt"], "tests_added": ["t"], "blocked_reason": "", "notes": ""})

    fname = f"work_{job_id}_{c}.py"
    open(fname, "w").write(f"VALUE = {c}\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"build step {c} for {job_id}"], check=True, capture_output=True)
    respond({
        "verdict": "DONE",
        "summary": [f"Completed build turn {c}"],
        "changes": [fname],
        "tests_added": [f"test_{fname}"],
        "blocked_reason": "",
        "notes": "",
    })

if "# Role: Factory Code Reviewer" in prompt:
    c, _ = bump(f"{job_id}:review-code")
    if job_id == "job-10" and c == 1:
        respond({
            "verdict": "CHANGES",
            "summary": ["Missing edge-case guard in round 1"],
            "findings": [{"severity": "must_fix", "file": "work_job-10_1.py", "line": 1, "issue": "missing guard", "fix": "add guard"}],
            "checked": ["spec", "standards"],
        })
    if job_id == "job-60":
        respond({
            "verdict": "CHANGES",
            "summary": ["Still failing review"],
            "findings": [{"severity": "must_fix", "file": "work.py", "line": 1, "issue": "persistent issue", "fix": "rewrite"}],
            "checked": ["spec"],
        })
    respond({"verdict": "PASS", "summary": ["Code clean"], "findings": [], "checked": ["spec", "standards"]})

if "# Role: Factory Security Reviewer" in prompt:
    bump(f"{job_id}:review-security")
    respond({"verdict": "PASS", "summary": ["Security clean"], "findings": [], "checked": ["auth", "injection"]})

if "# Role: Factory UX Reviewer" in prompt:
    bump(f"{job_id}:review-ux")
    respond({"verdict": "PASS", "summary": ["UX clean"], "findings": [], "checked": ["flows"]})

if "# Role: Factory UI Reviewer" in prompt:
    bump(f"{job_id}:review-ui")
    respond({"verdict": "PASS", "summary": ["UI clean"], "findings": [], "checked": ["layout"]})

if "# Role: Factory Approver" in prompt:
    bump(f"{job_id}:approve")
    # If job-50 is about to enter merging, simulate a concurrent conflicting push to origin/main!
    if job_id == "job-50":
        main_clone = os.environ["MAIN_CLONE_DIR"]
        open(os.path.join(main_clone, "ok.txt"), "w").write("concurrent-main-edit\n")
        subprocess.run(["git", "-C", main_clone, "commit", "-am", "concurrent main change"], check=True, capture_output=True)
        subprocess.run(["git", "-C", main_clone, "push", "origin", "main"], check=True, capture_output=True)
    respond({
        "decision": "APPROVE",
        "summary": ["Verified and ready"],
        "reasoning": "All checks and reviews passed.",
        "merge_danger": "Two-way door; isolated module.",
        "question": "",
    })

raise SystemExit(f"Unknown agent role in prompt: {prompt[:200]}")
"""


class PipelineE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = self.tmp_obj.name
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "repo")
        self.bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin_dir)

        for name, body in [("gh", FAKE_GH_SCRIPT), ("agy", FAKE_AGY_SCRIPT)]:
            p = os.path.join(self.bin_dir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(p, 0o755)

        self.gh_state_path = os.path.join(self.tmp, "gh_state.json")
        self.agy_state_path = os.path.join(self.tmp, "agy_state.json")
        with open(self.gh_state_path, "w", encoding="utf-8") as f:
            json.dump({"issues": {}, "prs": {}}, f)

        subprocess.run(["git", "init", "--bare", "-b", "main", self.origin], check=True, capture_output=True)
        subprocess.run(["git", "clone", self.origin, self.repo], check=True, capture_output=True)

        # Copy template files into test repo
        ws_root = os.path.abspath(".")
        shutil.copytree(os.path.join(ws_root, "factory", "agents"), os.path.join(self.repo, "factory", "agents"))
        shutil.copy(os.path.join(ws_root, "factory", "config.json"), os.path.join(self.repo, "factory", "config.json"))
        shutil.copy(os.path.join(ws_root, ".gitignore"), os.path.join(self.repo, ".gitignore"))
        os.makedirs(os.path.join(self.repo, "factory", "jobs"), exist_ok=True)
        open(os.path.join(self.repo, "factory", "jobs", ".gitkeep"), "w").close()

        checks_path = os.path.join(self.repo, "factory", "checks.sh")
        with open(checks_path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nset -euo pipefail\ntest ! -f bad_check.txt\ntest -f ok.txt\n")
        os.chmod(checks_path, 0o755)

        with open(os.path.join(self.repo, "ok.txt"), "w", encoding="utf-8") as f:
            f.write("initial\n")

        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-m", "initial template"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "push", "-u", "origin", "main"], check=True, capture_output=True)

        self._old_env = os.environ.copy()
        os.environ["PATH"] = f"{self.bin_dir}:{self._old_env.get('PATH', '')}"
        os.environ["FAKE_GH_STATE"] = self.gh_state_path
        os.environ["FAKE_AGY_STATE"] = self.agy_state_path
        os.environ["MAIN_CLONE_DIR"] = self.repo

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        self.tmp_obj.cleanup()

    def _seed_issue(self, number: int, title: str, labels: list[str] | None = None) -> None:
        with open(self.gh_state_path, encoding="utf-8") as f:
            data = json.load(f)
        data["issues"][str(number)] = {
            "number": number,
            "title": title,
            "body": f"Please implement {title}",
            "url": f"https://example.test/issues/{number}",
            "state": "open",
            "labels": labels or ["factory"],
            "comments": [],
        }
        with open(self.gh_state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def test_complex_3_slice_medium_risk_feature_stops_at_ready_pr(self):
        self._seed_issue(10, "Complex dashboard auth")
        job = process_job(self.repo, 10)
        self.assertEqual(job["stage"], "ready")
        self.assertEqual(job["round"], 2)
        self.assertEqual(job["completed_slices"], 3)
        self.assertIsNotNone(job["pr_number"])
        with open(self.agy_state_path, encoding="utf-8") as f:
            counts = json.load(f)
        # All 4 routed reviewers ran in round 1 and round 2
        for rev in ("review-code", "review-security", "review-ux", "review-ui"):
            self.assertEqual(counts[f"job-10:{rev}"], 2)
        with open(self.gh_state_path, encoding="utf-8") as f:
            gh_data = json.load(f)
        pr = gh_data["prs"][str(job["pr_number"])]
        self.assertFalse(pr["draft"])
        self.assertFalse(pr["merged"])
        self.assertNotIn("factory", gh_data["issues"]["10"]["labels"])

    def test_low_risk_fix_auto_merges(self):
        self._seed_issue(20, "Small typo fix")
        results = run_factory(self.repo)
        self.assertEqual(len(results), 1)
        job = results[0]
        self.assertEqual(job["stage"], "merged")
        with open(self.gh_state_path, encoding="utf-8") as f:
            gh_data = json.load(f)
        self.assertTrue(gh_data["prs"][str(job["pr_number"])]["merged"])
        table = format_status_table([job])
        self.assertIn("job-20", table)
        self.assertIn("merged", table)

    def test_mid_slice_crash_resumes_from_completed_slice(self):
        self._seed_issue(30, "Two-part feature")
        os.environ["CRASH_JOB_30_SLICE_2"] = "1"
        crashed = process_job(self.repo, 30)
        self.assertEqual(crashed["stage"], "needs-human")
        self.assertEqual(crashed["completed_slices"], 1)
        with open(self.agy_state_path, encoding="utf-8") as f:
            counts_after_crash = json.load(f)
        self.assertEqual(counts_after_crash["job-30:intake"], 1)
        self.assertEqual(counts_after_crash["job-30:spec"], 1)

        # Human clears needs-human and re-queues factory; slice 2 now succeeds without re-running intake/spec/slice 1!
        os.environ.pop("CRASH_JOB_30_SLICE_2", None)
        with open(self.gh_state_path, encoding="utf-8") as f:
            gh_data = json.load(f)
        gh_data["issues"]["30"]["labels"] = ["factory"]
        with open(self.gh_state_path, "w", encoding="utf-8") as f:
            json.dump(gh_data, f, indent=2)

        resumed = process_job(self.repo, 30)
        self.assertEqual(resumed["stage"], "merged")
        self.assertEqual(resumed["completed_slices"], 2)
        with open(self.agy_state_path, encoding="utf-8") as f:
            counts_final = json.load(f)
        self.assertEqual(counts_final["job-30:intake"], 1)
        self.assertEqual(counts_final["job-30:spec"], 1)

    def test_needs_info_pause_skip_and_human_reply_resume(self):
        self._seed_issue(40, "Session cleanup")
        paused = process_job(self.repo, 40)
        self.assertEqual(paused["stage"], "needs-info")
        # Next queue run skips issue 40 while factory:needs-info is set
        self.assertEqual(run_factory(self.repo), [])

        # Human comments and swaps label back to factory
        with open(self.gh_state_path, encoding="utf-8") as f:
            gh_data = json.load(f)
        gh_data["issues"]["40"]["comments"].append({"author": {"login": "owner"}, "body": "Human answer: archive for 30 days"})
        gh_data["issues"]["40"]["labels"] = ["factory"]
        with open(self.gh_state_path, "w", encoding="utf-8") as f:
            json.dump(gh_data, f, indent=2)

        done = run_factory(self.repo)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["stage"], "merged")

    def test_merge_conflict_and_max_rounds_escalate_cleanly(self):
        self._seed_issue(50, "Conflicting edit")
        conflict_job = process_job(self.repo, 50)
        self.assertEqual(conflict_job["stage"], "needs-human")
        wt50 = os.path.join(self.repo, ".worktrees", "job-50")
        self.assertTrue(is_worktree_clean(wt50))

        self._seed_issue(60, "Endless review changes")
        exhausted = process_job(self.repo, 60)
        self.assertEqual(exhausted["stage"], "needs-human")
        self.assertEqual(exhausted["round"], 3)

        ok, checks = run_doctor(self.repo)
        self.assertTrue(ok, checks)


if __name__ == "__main__":
    unittest.main()
