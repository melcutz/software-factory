import subprocess
import time

import pytest

from factory.lib.agy_runner import QuotaExhaustedError, run_agent
from factory.lib.git_state import GitStateStore
from factory.lib.governor import (
    HostResources,
    check_host_admission,
    is_model_in_cooldown,
    parse_linux_meminfo_gb,
    parse_macos_vm_stat_gb,
    quota_bucket_for_model,
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


def _setup_test_repo(tmp_path, name: str, models: dict) -> tuple:
    import json

    bare = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    seed = tmp_path / f"{name}-seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "T"], check=True)
    (seed / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:main"], check=True)

    repo = tmp_path / name
    subprocess.run(["git", "clone", str(bare), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "W"], check=True)
    (repo / "factory").mkdir()
    (repo / "factory" / "config.json").write_text(
        json.dumps({"base_branch": "main", "models": models}),
        encoding="utf-8",
    )
    return repo, bare


def test_run_worker_gates_claims_on_intake_spec_writer_or_builder_cooldown(tmp_path, monkeypatch):
    import json
    import sys

    from factory.lib.worker import run_worker

    repo, _ = _setup_test_repo(
        tmp_path,
        "cd-repo",
        {
            "intake": "gemini-3.8-flash-medium",
            "spec_writer": "gemini-4-hydrogen-high",
            "builder": "gemini-4-argon:high",
        },
    )
    host_dir = tmp_path / "host_cfg"
    host_dir.mkdir()
    (host_dir / "host.json").write_text(
        json.dumps({"machine_id": "m0", "max_workers": 2, "ramp_up_delay_sec": 0.0}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FACTORY_HOST_DIR", str(host_dir))

    hc_log = tmp_path / "hc_cd.log"
    fake_hc = tmp_path / "hc"
    fake_hc.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(hc_log)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t99')\n",
        encoding="utf-8",
    )
    fake_hc.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_hc))

    monkeypatch.setattr("factory.lib.worker.run_doctor", lambda _r: (True, ["ok"]))
    monkeypatch.setattr(
        "factory.lib.governor.probe_host_resources",
        lambda: HostResources(free_ram_gb=12.5, cpu_load_pct=18.0),
    )
    monkeypatch.setattr("factory.lib.worker._plan_queue_batch", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(
        "factory.lib.worker.GitHubClient.list_queue_issues",
        lambda self, cfg: [{"number": 88, "title": "Issue 88", "body": ""}],
    )

    store = GitStateStore(str(repo), "m0")

    # 1. intake base model in cooldown -> skips claim
    store.record_quota_cooldown("gemini-3.8-flash", "429 intake", retry_after_sec=300)
    assert run_worker([str(repo)], once=True) == 0
    assert not hc_log.exists()
    assert store.list_remote_jobs() == []
    hb = store.list_worker_heartbeats()["m0"]
    assert hb["free_ram_gb"] == 12.5
    assert hb["cpu_load_pct"] == 18.0

    # 2. spec_writer base model in cooldown -> skips claim
    store.clear_quota_cooldown("gemini-3.8-flash")
    store.record_quota_cooldown("gemini-4-hydrogen", "429 spec_writer", retry_after_sec=300)
    assert run_worker([str(repo)], once=True) == 0
    assert not hc_log.exists()
    assert store.list_remote_jobs() == []

    # 3. builder base model in cooldown -> skips claim
    store.clear_quota_cooldown("gemini-4-hydrogen")
    store.record_quota_cooldown("gemini-4-argon", "429 builder", retry_after_sec=300)
    assert run_worker([str(repo)], once=True) == 0
    assert not hc_log.exists()
    assert store.list_remote_jobs() == []

    # 4. all three roles clear -> claims job
    store.clear_quota_cooldown("gemini-4-argon")
    assert run_worker([str(repo)], once=True) == 0
    assert hc_log.exists()
    jobs = store.list_remote_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "job-88"


def test_pipeline_clears_quota_cooldown_on_successful_agent_call(tmp_path, monkeypatch):
    repo, _ = _setup_test_repo(
        tmp_path,
        "clear-cd-repo",
        {
            "intake": "gemini-4-argon:medium",
            "spec_writer": "gemini-4-hydrogen-high",
            "builder": "gemini-4-argon-high",
        },
    )

    class FakeGH:
        def fetch_issue(self, n):
            return {"number": n, "title": "Clarify spec", "body": "body", "url": "https://example/89"}, "# Clarify spec"

        def edit_labels(self, *a, **kw):
            pass

        def comment_issue(self, *a, **kw):
            pass

    def fake_run_agent(*args, **kwargs):
        return (
            {
                "decision": "NEEDS_INFO",
                "risk": "low",
                "surfaces": [],
                "summary": "Need info",
                "reasoning": "Missing details",
                "questions": ["What input format?"],
            },
            {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            1.2,
        )

    monkeypatch.setattr("factory.lib.pipeline.run_agent", fake_run_agent)
    monkeypatch.setattr("factory.lib.pipeline.ProjectBoardSync.discover", lambda *a, **kw: None)

    store = GitStateStore(str(repo), "m0")
    store.write_worker_heartbeat(1, 2, False)
    # Record an expired cooldown for gemini-4-argon (stale entry still in refs/factory/quota)
    # and an active cooldown for gemini-4-hydrogen.
    store.record_quota_cooldown("gemini-4-argon", "prior 429", retry_after_sec=60, now_ts=time.time() - 300)
    store.record_quota_cooldown("gemini-4-hydrogen", "other model 429", retry_after_sec=60)
    assert "gemini-4-argon" in store.get_quota_state()["models"]

    job = process_job(str(repo), 89, gh=FakeGH(), state_store=store)
    assert job["stage"] == "needs-info"

    models_after = store.get_quota_state()["models"]
    assert "gemini-4-argon" not in models_after
    assert "gemini-4-hydrogen" in models_after


def test_quota_bucket_for_model_normalizes_providers():
    from factory.lib.governor import quota_cooldown_for_model

    assert quota_bucket_for_model("opencode/deepseek-v4.1-flash-free") == "opencode-free"
    assert quota_bucket_for_model("opencode/mimo-v2.6-flash-free") == "opencode-free"
    assert quota_bucket_for_model("opencode/big-pickle") == "opencode-free"
    assert quota_bucket_for_model("openrouter/cohere/north-mini-code:free") == "openrouter-free"
    assert quota_bucket_for_model("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free") == "openrouter-free"
    assert quota_bucket_for_model("opencode-go/glm-5.3") == "opencode-go"
    assert quota_bucket_for_model("opencode-go/kimi-k3") == "opencode-go"
    assert quota_bucket_for_model("gpu:decision-model") == "gpu"
    assert quota_bucket_for_model("gemini-4-argon-high") == "gemini-4-argon"
    assert quota_bucket_for_model("gemini-4-hydrogen:medium") == "gemini-4-hydrogen"

    custom_cfg = {
        "quota": {
            "default_cooldown_sec": 900,
            "buckets": [
                {"match": "custom-provider/*:free", "bucket": "custom-free", "cooldown_sec": 600},
                {"match": "openrouter/*:free", "bucket": "openrouter-free", "cooldown_sec": None},
                {"match": "opencode-go/*", "bucket": "opencode-go", "cooldown_sec": 1200},
            ],
        }
    }
    assert quota_bucket_for_model("custom-provider/foo:free", custom_cfg) == "custom-free"
    assert quota_cooldown_for_model("custom-provider/foo:free", custom_cfg) == ("custom-free", 600)
    assert quota_cooldown_for_model("openrouter/foo:free", custom_cfg, retry_after_sec=45) == ("openrouter-free", 45)
    assert quota_cooldown_for_model("openrouter/foo:free", custom_cfg) == ("openrouter-free", 900)
    assert quota_cooldown_for_model("opencode-go/glm-5.3", custom_cfg) == ("opencode-go", 1200)
    assert quota_cooldown_for_model("unmatched/model-x", custom_cfg) == ("unmatched/model-x", 900)


def test_is_model_in_cooldown_and_record_quota_cooldown_use_provider_buckets(tmp_path):
    repo, _ = _setup_test_repo(
        tmp_path,
        "bucket-cd-repo",
        {"intake": "gemini-4-hydrogen-low"},
    )
    store = GitStateStore(str(repo), "m0")

    # Recording a single opencode Zen free model puts ALL opencode free models in cooldown
    store.record_quota_cooldown(
        "opencode/deepseek-v4.1-flash-free",
        "Free usage limit reached",
        retry_after_sec=600,
        now_ts=1000.0,
    )
    q = store.get_quota_state()
    assert "opencode-free" in q["models"]
    assert q["models"]["opencode-free"]["cooldown_until_ts"] == 1000.0 + 600

    in_cd, rem = is_model_in_cooldown(q, "opencode/mimo-v2.6-flash-free", now_ts=1100.0)
    assert in_cd is True
    assert rem == 500.0
    in_cd_bp, _ = is_model_in_cooldown(q, "opencode/big-pickle", now_ts=1100.0)
    assert in_cd_bp is True
    in_cd_or, _ = is_model_in_cooldown(q, "openrouter/cohere/north-mini-code:free", now_ts=1100.0)
    assert in_cd_or is False

    # OpenRouter free and opencode-go buckets respect retry_after_sec
    store.record_quota_cooldown(
        "openrouter/cohere/north-mini-code:free",
        "429 free-models-per-day",
        retry_after_sec=600,
        now_ts=1000.0,
    )
    store.record_quota_cooldown(
        "opencode-go/glm-5.3",
        "weekly limit reached",
        retry_after_sec=1200,
        now_ts=1000.0,
    )
    q2 = store.get_quota_state()
    assert is_model_in_cooldown(q2, "openrouter/google/gemma-4-31b-it:free", now_ts=1500.0)[0] is True
    in_cd_go, rem_go = is_model_in_cooldown(q2, "opencode-go/kimi-k3", now_ts=2000.0)
    assert in_cd_go is True
    assert rem_go == 200.0

    # Clearing via any model in the bucket clears the shared bucket
    store.clear_quota_cooldown("opencode/hy4-flash-free")
    q3 = store.get_quota_state()
    assert "opencode-free" not in q3["models"]


