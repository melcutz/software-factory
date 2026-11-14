"""Unit tests for factory merge-conflict resolution during the merging stage."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from factory.lib import pipeline
from factory.lib.git_ops import (
    conflicted_files,
    is_worktree_clean,
    merge_in_progress,
    sync_with_base,
)
from factory.lib.state import advance_stage, create_or_resume_job


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _setup_conflicting_worktree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create bare origin, repo, jobs_dir, and a conflicting job-901 worktree."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(origin), str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    target = repo / "app.py"
    target.write_text("VALUE = 'base'\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "-u", "origin", "main")

    worktree = repo / ".worktrees" / "job-901"
    worktree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", "factory/job-901", str(worktree), "origin/main")
    _git(worktree, "config", "user.email", "test@example.com")
    _git(worktree, "config", "user.name", "Test")

    # Branch change
    (worktree / "app.py").write_text("VALUE = 'branch'\n", encoding="utf-8")
    _git(worktree, "add", "app.py")
    _git(worktree, "commit", "-m", "branch edit")
    _git(worktree, "push", "-u", "origin", "factory/job-901")

    # Conflicting origin/main change
    (repo / "app.py").write_text("VALUE = 'main'\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "main edit")
    _git(repo, "push", "origin", "main")

    jobs_dir = repo / "factory" / "jobs"
    jobs_dir.mkdir(parents=True)
    create_or_resume_job(
        str(jobs_dir),
        {
            "number": 901,
            "url": "https://github.com/example/repo/issues/901",
            "title": "Test conflict",
        },
        max_rounds=3,
    )
    for next_stage in ("spec", "build", "gate", "review", "approve", "merging"):
        advance_stage(str(jobs_dir), "job-901", next_stage)
    return origin, repo, jobs_dir, worktree


def _base_cfg() -> dict:
    return {
        "agent_timeout_sec": 60,
        "gate_timeout_sec": 60,
        "models": {"builder": "gemini-4-hydrogen-medium"},
    }


def test_sync_with_base_keeps_conflicts_when_abort_disabled(tmp_path: Path) -> None:
    _, _, _, worktree = _setup_conflicting_worktree(tmp_path)

    ok, head_changed, _ = sync_with_base(str(worktree), "main", abort_on_conflict=False)
    assert not ok
    assert not head_changed
    assert merge_in_progress(str(worktree))
    assert conflicted_files(str(worktree)) == ["app.py"]


def test_sync_pr_with_base_resolves_conflict_runs_gate_and_pushes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, repo, jobs_dir, worktree = _setup_conflicting_worktree(tmp_path)
    jdir = jobs_dir / "job-901"
    (jdir / "spec.md").write_text("# Spec\nKeep branch behavior.\n", encoding="utf-8")

    def fake_run_agent(*args, **kwargs):
        assert kwargs.get("log_name") == "r1-merge-conflict"
        assert "app.py" in args[6]
        (worktree / "app.py").write_text("VALUE = 'resolved'\n", encoding="utf-8")
        _git(worktree, "add", "app.py")
        _git(worktree, "commit", "--no-edit")
        return (
            {
                "verdict": "DONE",
                "summary": ["Resolved conflict in app.py"],
                "changes": ["app.py"],
                "tests_added": [],
                "blocked_reason": "",
                "notes": "",
            },
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            1.2,
        )

    gate_mock = MagicMock(return_value=(True, "VERDICT: PASS"))
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "run_gate", gate_mock)

    ok, err = pipeline._sync_pr_with_base(
        str(repo),
        str(repo / "factory"),
        str(jobs_dir),
        str(jdir),
        "job-901",
        str(worktree),
        "factory/job-901",
        "main",
        1,
        _base_cfg(),
    )

    assert ok, err
    assert err == ""
    assert not merge_in_progress(str(worktree))
    assert is_worktree_clean(str(worktree))
    gate_mock.assert_called_once()
    assert gate_mock.call_args.kwargs["tag"] == "pre-merge"
    # Remote branch was pushed with the merge commit
    remote_head = _git(origin, "rev-parse", "refs/heads/factory/job-901")
    local_head = _git(worktree, "rev-parse", "HEAD")
    assert remote_head == local_head


def test_sync_pr_with_base_aborts_merge_when_builder_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repo, jobs_dir, worktree = _setup_conflicting_worktree(tmp_path)
    jdir = jobs_dir / "job-901"

    def fake_run_agent(*args, **kwargs):
        return (
            {
                "verdict": "BLOCKED",
                "summary": [],
                "changes": [],
                "tests_added": [],
                "blocked_reason": "Incompatible refactor on main",
                "notes": "",
            },
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            1.0,
        )

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        pipeline,
        "run_gate",
        MagicMock(side_effect=AssertionError("gate should not run")),
    )

    ok, err = pipeline._sync_pr_with_base(
        str(repo),
        str(repo / "factory"),
        str(jobs_dir),
        str(jdir),
        "job-901",
        str(worktree),
        "factory/job-901",
        "main",
        1,
        _base_cfg(),
    )

    assert not ok
    assert "Incompatible refactor on main" in err
    assert not merge_in_progress(str(worktree))
    assert is_worktree_clean(str(worktree))


def test_sync_pr_with_base_aborts_if_builder_leaves_uncommitted_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repo, jobs_dir, worktree = _setup_conflicting_worktree(tmp_path)
    jdir = jobs_dir / "job-901"

    def fake_run_agent(*args, **kwargs):
        # Resolves file but forgets git add / git commit
        (worktree / "app.py").write_text("VALUE = 'resolved'\n", encoding="utf-8")
        return (
            {
                "verdict": "DONE",
                "summary": ["Edited app.py"],
                "changes": ["app.py"],
                "tests_added": [],
                "blocked_reason": "",
                "notes": "",
            },
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            1.0,
        )

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        pipeline,
        "run_gate",
        MagicMock(side_effect=AssertionError("gate should not run")),
    )

    ok, err = pipeline._sync_pr_with_base(
        str(repo),
        str(repo / "factory"),
        str(jobs_dir),
        str(jdir),
        "job-901",
        str(worktree),
        "factory/job-901",
        "main",
        1,
        _base_cfg(),
    )

    assert not ok
    assert "clean merge commit" in err
    assert not merge_in_progress(str(worktree))
    assert is_worktree_clean(str(worktree))


def test_sync_pr_with_base_fails_when_post_sync_gate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, repo, jobs_dir, worktree = _setup_conflicting_worktree(tmp_path)
    jdir = jobs_dir / "job-901"

    def fake_run_agent(*args, **kwargs):
        (worktree / "app.py").write_text("VALUE = 'resolved'\n", encoding="utf-8")
        _git(worktree, "add", "app.py")
        _git(worktree, "commit", "--no-edit")
        return (
            {
                "verdict": "DONE",
                "summary": ["Resolved"],
                "changes": ["app.py"],
                "tests_added": [],
                "blocked_reason": "",
                "notes": "",
            },
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            1.0,
        )

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        pipeline,
        "run_gate",
        MagicMock(return_value=(False, "VERDICT: FAIL\nruff failed")),
    )

    ok, err = pipeline._sync_pr_with_base(
        str(repo),
        str(repo / "factory"),
        str(jobs_dir),
        str(jdir),
        "job-901",
        str(worktree),
        "factory/job-901",
        "main",
        1,
        _base_cfg(),
    )

    assert not ok
    assert "Post-sync gate failed" in err
