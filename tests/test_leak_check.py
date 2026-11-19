"""Hermetic tests for scripts/check_leaks.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_leaks.py"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    return repo


def _run_check(
    repo: Path,
    *args: str,
    env_overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("FACTORY_LEAK_DENYLIST", None)
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_no_denylist_configured_skips_cleanly(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")

    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    res = _run_check(repo, env_overrides={"HOME": str(empty_home), "FACTORY_LEAK_DENYLIST": None})
    assert res.returncode == 0
    assert "leak check: no denylist configured, skipped" in res.stdout


def test_hit_allow_exclude_and_binary_skip(tmp_path: Path):
    repo = _init_repo(tmp_path)
    denylist = tmp_path / "denylist.json"
    denylist.write_text(
        json.dumps(
            {
                "patterns": [r"secret-host-zz", r"private-token-\d+"],
                "allow": [r"https://example\.org/secret-host-zz/docs"],
                "exclude_paths": ["docs/legacy/*.md"],
            }
        ),
        encoding="utf-8",
    )

    (repo / "app.txt").write_text(
        "allowed: https://example.org/secret-host-zz/docs\n"
        "bad: SECRET-HOST-ZZ here\n",
        encoding="utf-8",
    )
    (repo / "docs" / "legacy").mkdir(parents=True)
    (repo / "docs" / "legacy" / "old.md").write_text("secret-host-zz\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\x00\x01secret-host-zz\x02\xff")

    _git(repo, "add", "app.txt", "docs/legacy/old.md", "blob.bin")
    _git(repo, "commit", "-m", "add files")

    res = _run_check(repo, "--denylist", str(denylist))
    assert res.returncode == 1
    out = res.stdout.strip()
    assert out == "app.txt:2: matched denylist pattern #1"
    assert "secret-host-zz" not in out.lower()
    assert "private-token" not in out.lower()


def test_clean_repo_exits_zero(tmp_path: Path):
    repo = _init_repo(tmp_path)
    denylist = tmp_path / "denylist.json"
    denylist.write_text(json.dumps({"patterns": [r"secret-host-zz"]}), encoding="utf-8")

    (repo / "clean.py").write_text("HOST = 'host-a'\n", encoding="utf-8")
    _git(repo, "add", "clean.py")
    _git(repo, "commit", "-m", "clean commit")

    res = _run_check(repo, env_overrides={"FACTORY_LEAK_DENYLIST": str(denylist)})
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_denylist_precedence_cli_env_repo_local(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "file.txt").write_text("alpha-leak\nbeta-leak\ngamma-leak\n", encoding="utf-8")
    (repo / "factory").mkdir()
    (repo / "factory" / "leak-denylist.local.json").write_text(
        json.dumps({"patterns": ["gamma-leak"]}), encoding="utf-8"
    )
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "add file")

    env_denylist = tmp_path / "env.json"
    env_denylist.write_text(json.dumps({"patterns": ["beta-leak"]}), encoding="utf-8")
    cli_denylist = tmp_path / "cli.json"
    cli_denylist.write_text(json.dumps({"patterns": ["alpha-leak"]}), encoding="utf-8")

    # Repo-local used when CLI and env unset
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    r1 = _run_check(repo, env_overrides={"HOME": str(empty_home)})
    assert r1.returncode == 1
    assert r1.stdout.strip() == "file.txt:3: matched denylist pattern #1"

    # Env overrides repo-local
    r2 = _run_check(repo, env_overrides={"HOME": str(empty_home), "FACTORY_LEAK_DENYLIST": str(env_denylist)})
    assert r2.returncode == 1
    assert r2.stdout.strip() == "file.txt:2: matched denylist pattern #1"

    # CLI overrides env and repo-local
    r3 = _run_check(
        repo,
        "--denylist",
        str(cli_denylist),
        env_overrides={"HOME": str(empty_home), "FACTORY_LEAK_DENYLIST": str(env_denylist)},
    )
    assert r3.returncode == 1
    assert r3.stdout.strip() == "file.txt:1: matched denylist pattern #1"


def test_history_scan_detects_commit_message_and_added_diff(tmp_path: Path):
    repo = _init_repo(tmp_path)
    denylist = tmp_path / "denylist.json"
    denylist.write_text(
        json.dumps({"patterns": [r"secret-host-zz", r"secret-msg-token"]}),
        encoding="utf-8",
    )

    (repo / "notes.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-m", "initial")

    (repo / "notes.txt").write_text("base\nsecret-host-zz\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-m", "leak in diff")
    sha1 = _git(repo, "rev-parse", "HEAD").strip()[:12]

    (repo / "notes.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-m", "cleanup secret-msg-token")
    sha2 = _git(repo, "rev-parse", "HEAD").strip()[:12]

    res = _run_check(repo, "--denylist", str(denylist), "--history", "HEAD~2..HEAD")
    assert res.returncode == 1
    out = res.stdout
    assert f"{sha1}: matched denylist pattern #1" in out
    assert f"{sha2}: matched denylist pattern #2" in out
    assert "secret-host-zz" not in out.lower()
    assert "secret-msg-token" not in out.lower()
