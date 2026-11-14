"""Deterministic gate runner: protected_paths diff check + factory/checks.sh execution."""
from __future__ import annotations

import fnmatch
import os
import subprocess

from factory.lib.git_ops import changed_files_against_base

HARDCODED_PROTECTED: tuple[str, ...] = (
    "factory/checks.sh",
    "factory/config.json",
    "factory/bin/",
    "factory/lib/",
    ".agents/",
)

MAX_CHECKS_MD_LINES = 120


def _normalize_relpath(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = os.path.normpath(cleaned).replace("\\", "/")
    if cleaned == ".":
        return ""
    return cleaned.lstrip("/")


def _matches_pattern(relpath: str, pattern: str) -> bool:
    pat = pattern.strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    if not pat:
        return False
    if pat.endswith("/"):
        prefix = pat.rstrip("/")
        return relpath == prefix or relpath.startswith(prefix + "/")
    if any(ch in pat for ch in "*?["):
        return fnmatch.fnmatch(relpath, pat)
    return relpath == pat or relpath.startswith(pat + "/")


def find_protected_violations(changed_files: list[str], configured_patterns: list[str]) -> list[str]:
    patterns = list(HARDCODED_PROTECTED) + list(configured_patterns or [])
    violations: list[str] = []
    for raw in changed_files:
        norm = _normalize_relpath(raw)
        if not norm:
            continue
        if any(_matches_pattern(norm, pat) for pat in patterns):
            violations.append(norm)
    return sorted(set(violations))


def run_gate(
    repo_root: str,
    job_id: str,
    worktree: str,
    round_num: int,
    config: dict,
    tag: str | None = None,
    scope: str = "full",
) -> tuple[bool, str]:
    """Check protected_paths and execute main checkout's factory/checks.sh inside worktree."""
    factory_dir = os.path.join(repo_root, "factory")
    round_dir = os.path.join(factory_dir, "jobs", job_id, f"round-{round_num}")
    os.makedirs(round_dir, exist_ok=True)

    prefix = f"{tag}-" if tag else ""
    log_path = os.path.join(round_dir, f"{prefix}checks.log")
    md_path = os.path.join(round_dir, f"{prefix}checks.md")

    base_branch = config.get("base_branch", "main")
    changed = changed_files_against_base(worktree, base_branch)
    violations = find_protected_violations(changed, config.get("protected_paths", []))

    if violations:
        msg = "Protected path violation(s) detected on branch:\n" + "\n".join(f"- {v}" for v in violations) + "\n"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        md = "\n".join(
            [
                "VERDICT: FAIL",
                "",
                "## Summary",
                "- Gate rejected modifications to protected paths.",
                "",
                "## Violations",
                *(f"- `{v}`" for v in violations),
                "",
            ]
        )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        if tag:
            # Keep canonical round-level checks.md updated with latest gate result
            with open(os.path.join(round_dir, "checks.md"), "w", encoding="utf-8") as f:
                f.write(md)
        return False, md

    checks_script = os.path.join(factory_dir, "checks.sh")
    timeout_sec = int(config.get("gate_timeout_sec", 600))
    gate_env = {
        **os.environ,
        "FACTORY_GATE_SCOPE": scope,
        "FACTORY_BASE_BRANCH": str(base_branch),
    }
    try:
        proc = subprocess.run(
            ["bash", checks_script],
            cwd=worktree,
            env=gate_env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        exit_code = proc.returncode
        full_output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        full_output = f"checks.sh timed out after {timeout_sec}s\n{exc.stdout or ''}\n{exc.stderr or ''}"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(full_output)

    lines = full_output.splitlines()
    truncated = len(lines) > MAX_CHECKS_MD_LINES
    tail_text = "\n".join(lines[-MAX_CHECKS_MD_LINES:])
    verdict = "PASS" if exit_code == 0 else "FAIL"
    trunc_note = f" (showing last {MAX_CHECKS_MD_LINES} of {len(lines)} lines; full log in `{os.path.basename(log_path)}`)" if truncated else ""

    md = "\n".join(
        [
            f"VERDICT: {verdict}",
            "",
            "## Summary",
            f"- Ran `factory/checks.sh` in `{worktree}`",
            f"- Exit code: `{exit_code}`{trunc_note}",
            "",
            "## Output Tail",
            "```text",
            tail_text,
            "```",
            "",
        ]
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    if tag:
        with open(os.path.join(round_dir, "checks.md"), "w", encoding="utf-8") as f:
            f.write(md)
        with open(os.path.join(round_dir, "checks.log"), "w", encoding="utf-8") as f:
            f.write(full_output)

    return exit_code == 0, md
