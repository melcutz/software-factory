import subprocess

import pytest

from factory.lib.agy_runner import QuotaExhaustedError, run_agent
from factory.lib.git_state import GitStateStore
from factory.lib.governor import (
    HostResources,
    check_host_admission,
    parse_linux_meminfo_gb,
    parse_macos_vm_stat_gb,
)
from factory.lib.pipeline import process_job
from factory.lib.worker import HostConfig


def test_linux_and_macos_memory_parsers():
    meminfo = "MemTotal:       16384000 kB\nMemFree:         1024000 kB\nMemAvailable:    4194304 kB\n"
    assert abs(parse_linux_meminfo_gb(meminfo) - 4.0) < 0.01

    vm_stat = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               10000.\n"
        "Pages inactive:                           20000.\n"
        "Pages speculative:                        10000.\n"
    )
    # 40000 * 16384 = 655,360,000 bytes (~0.61 GB)
    assert abs(parse_macos_vm_stat_gb(vm_stat, page_size=16384) - 0.61) < 0.02


def test_check_host_admission_thresholds():
    cfg = HostConfig(machine_id="m0", min_free_ram_gb=2.0, max_cpu_load_pct=85.0)
    ok, _ = check_host_admission(cfg, resources=HostResources(free_ram_gb=4.0, cpu_load_pct=50.0))
    assert ok is True

    ok_ram, msg_ram = check_host_admission(cfg, resources=HostResources(free_ram_gb=1.2, cpu_load_pct=20.0))
    assert ok_ram is False
    assert "RAM" in msg_ram

    ok_cpu, msg_cpu = check_host_admission(cfg, resources=HostResources(free_ram_gb=8.0, cpu_load_pct=90.0))
    assert ok_cpu is False
    assert "CPU" in msg_cpu


def test_agy_runner_raises_quota_exhausted_without_retry(tmp_path, monkeypatch):
    fdir = tmp_path / "factory"
    (fdir / "agents").mkdir(parents=True)
    (fdir / "agents" / "intake.md").write_text("Intake agent\n")
    jdir = tmp_path / "job"
    jdir.mkdir()

    calls = {"n": 0}

    def fake_invoke(*args, **kwargs):
        calls["n"] += 1
        return 1, "", "Error 429 RESOURCE_EXHAUSTED: Retry after 45s", None

    monkeypatch.setattr("factory.lib.agy_runner._invoke_agy_process", fake_invoke)
    monkeypatch.setattr("factory.lib.agy_runner.head_sha", lambda *a: "deadbeef")

    with pytest.raises(QuotaExhaustedError) as exc_info:
        run_agent(
            str(fdir),
            str(jdir),
            str(tmp_path),
            "intake",
            "intake",
            "gemini-4-argon:medium",
            "prompt",
            {"type": "object"},
            60,
            sandbox=False,
        )
    assert calls["n"] == 1
    assert exc_info.value.model_spec == "gemini-4-argon:medium"
    assert exc_info.value.retry_after_sec == 45


def test_pipeline_quota_exhausted_checkpoints_without_needs_human(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "T"], check=True)
    (seed / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:main"], check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "W"], check=True)
    (repo / "factory").mkdir()
    (repo / "factory" / "config.json").write_text(
        '{"base_branch": "main", "models": {"intake": "gemini-4-argon"}}'
    )

    class FakeGH:
        def fetch_issue(self, n):
            return {"number": n, "title": "Fix bug", "body": "body", "url": "https://example/1"}, "# Fix bug"

        def edit_labels(self, *a, **kw):
            pass

        def comment_issue(self, *a, **kw):
            pass

    def fake_run_agent(*args, **kwargs):
        raise QuotaExhaustedError("gemini-4-argon:medium", retry_after_sec=180, message="429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)
    store = GitStateStore(str(repo), "m0")
    store.write_worker_heartbeat(1, 2, False)

    job = process_job(str(repo), 77, gh=FakeGH(), state_store=store)
    assert job["stage"] == "intake"
    assert job["round"] == 0
    assert job["activity"] == "cooldown: quota"
    q = store.get_quota_state()
    assert "gemini-4-argon" in q["models"]
    assert q["models"]["gemini-4-argon"]["consecutive_429s"] == 1
