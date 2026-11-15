import json
import subprocess
import threading

from factory.lib.git_state import GitStateStore


def _init_bare_and_clones(tmp_path, n=2):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:main"], check=True)
    clones = []
    for i in range(n):
        c = tmp_path / f"worker-{i}"
        subprocess.run(["git", "clone", str(bare), str(c)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(c), "config", "user.email", "t@e.com"], check=True)
        subprocess.run(["git", "-C", str(c), "config", "user.name", f"W{i}"], check=True)
        clones.append(str(c))
    return str(bare), clones


def test_concurrent_cas_claim_only_one_winner(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=3)
    stores = [GitStateStore(clones[i], f"machine-{i}") for i in range(3)]
    for s in stores:
        s.write_worker_heartbeat(0, 2, False)

    results = []
    barrier = threading.Barrier(3)

    def _try(store):
        barrier.wait()
        ok, job = store.claim_job_lease("job-42", {"id": "job-42", "issue": 42, "stage": "intake"})
        results.append((ok, job))

    threads = [threading.Thread(target=_try, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    winners = [r for r in results if r[0]]
    assert len(winners) == 1
    assert winners[0][1]["lease"]["fencing_token"] == 1


def test_skew_margin_prevents_early_takeover_and_allows_expired(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    t0 = 1_700_000_000.0
    s0.write_worker_heartbeat(1, 2, False, now_ts=t0)  # expires_at = t0 + 360
    ok, _ = s0.claim_job_lease("job-7", {"id": "job-7", "issue": 7, "stage": "build"}, now_ts=t0)
    assert ok

    # At t0 + 370 (past 360s TTL, inside 60s skew margin) -> must NOT steal
    ok_early, _ = s1.claim_job_lease("job-7", now_ts=t0 + 370)
    assert ok_early is False

    # At t0 + 430 (past 360 + 60s skew margin) -> MUST steal with incremented fencing_token
    ok_late, job = s1.claim_job_lease("job-7", now_ts=t0 + 430)
    assert ok_late is True
    assert job["lease"]["worker_id"] == "m1"
    assert job["lease"]["fencing_token"] == 2


def test_checkpoint_hydration_and_slice_checkpoint(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    s0.write_worker_heartbeat(1, 2, False)
    jdir0 = tmp_path / "j0"
    jdir0.mkdir()
    (jdir0 / "spec.md").write_text("# Spec\n")
    (jdir0 / "plan.json").write_text(json.dumps({"slices": [{"id": 1}, {"id": 2}]}))
    ok, job = s0.claim_job_lease("job-9", {"id": "job-9", "issue": 9, "stage": "build", "completed_slices": 1})
    assert ok
    s0.checkpoint_job("job-9", str(jdir0), job, release_lease=True)

    # Make a slice commit in clone 0 and push slice checkpoint
    (tmp_path / "worker-0" / "slice1.txt").write_text("done\n")
    subprocess.run(["git", "-C", clones[0], "add", "slice1.txt"], check=True)
    subprocess.run(["git", "-C", clones[0], "commit", "-m", "slice 1"], check=True)
    sha = s0.push_slice_checkpoint("job-9", clones[0])

    s1.write_worker_heartbeat(0, 2, False)
    ok2, _ = s1.claim_job_lease("job-9")
    assert ok2
    jdir1 = tmp_path / "j1"
    hydrated = s1.hydrate_job("job-9", str(jdir1))
    assert hydrated["completed_slices"] == 1
    assert (jdir1 / "spec.md").read_text() == "# Spec\n"
    fetched = s1.fetch_slice_checkpoint("job-9", clones[1])
    assert fetched == sha


def test_staging_lock_control_quota_and_prune(tmp_path):
    _, clones = _init_bare_and_clones(tmp_path, n=2)
    s0 = GitStateStore(clones[0], "m0")
    s1 = GitStateStore(clones[1], "m1")
    s0.write_worker_heartbeat(1, 2, False)
    s1.write_worker_heartbeat(0, 2, False)

    assert s0.acquire_staging_lock(["job-1"]) is True
    assert s1.acquire_staging_lock(["job-2"]) is False
    s0.release_staging_lock()
    assert s1.acquire_staging_lock(["job-2"]) is True

    s0.set_halted(True, reason="maintenance")
    assert s1.get_control_state()["halted"] is True
    s0.set_halted(False)
    assert s1.get_control_state()["halted"] is False

    q = s0.record_quota_cooldown("gemini-4-argon", "429 RESOURCE_EXHAUSTED")
    assert q["models"]["gemini-4-argon"]["consecutive_429s"] == 1
    assert s1.get_quota_state()["models"]["gemini-4-argon"]["consecutive_429s"] == 1
    s1.clear_quota_cooldown("gemini-4-argon")
    assert "gemini-4-argon" not in s0.get_quota_state()["models"]
