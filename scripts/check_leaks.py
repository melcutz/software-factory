#!/usr/bin/env python3
"""Scan tracked repository files (and optional git history) against a local leak denylist."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys


def _repo_root(start_dir: str | None = None) -> str:
    cwd = os.path.abspath(start_dir or os.getcwd())
    res = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        return os.path.abspath(res.stdout.strip())
    return cwd


def resolve_denylist_path(repo_root: str, cli_denylist: str | None = None) -> str | None:
    """Return the first existing denylist path according to precedence, or None."""
    candidates = [
        cli_denylist,
        os.environ.get("FACTORY_LEAK_DENYLIST"),
        os.path.join(repo_root, "factory", "leak-denylist.local.json"),
        os.path.expanduser("~/.config/factory/leak-denylist.json"),
    ]
    for raw in candidates:
        if not raw:
            continue
        expanded = os.path.abspath(os.path.expanduser(raw))
        if os.path.isfile(expanded):
            return expanded
    return None


def _load_denylist(path: str) -> tuple[list[re.Pattern[str]], list[re.Pattern[str]], list[str]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Denylist must be a JSON object: {path}")
    patterns = [re.compile(str(p), re.IGNORECASE) for p in (data.get("patterns") or [])]
    allow = [re.compile(str(a), re.IGNORECASE) for a in (data.get("allow") or [])]
    exclude_paths = [str(g) for g in (data.get("exclude_paths") or []) if str(g).strip()]
    return patterns, allow, exclude_paths


def _is_excluded(rel_path: str, exclude_globs: list[str]) -> bool:
    norm = rel_path.replace(os.sep, "/")
    return any(fnmatch.fnmatch(norm, glob) for glob in exclude_globs)


def _line_hits(
    line: str,
    patterns: list[re.Pattern[str]],
    allow: list[re.Pattern[str]],
) -> list[int]:
    allow_spans = [
        (m.start(), m.end())
        for areg in allow
        for m in areg.finditer(line)
    ]
    hits: list[int] = []
    for idx, preg in enumerate(patterns, start=1):
        for m in preg.finditer(line):
            if not any(a_start <= m.start() and m.end() <= a_end for a_start, a_end in allow_spans):
                hits.append(idx)
                break
    return hits


def _is_binary_bytes(raw: bytes) -> bool:
    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def scan_tracked_files(
    repo_root: str,
    patterns: list[re.Pattern[str]],
    allow: list[re.Pattern[str]],
    exclude_paths: list[str],
) -> list[str]:
    res = subprocess.run(
        ["git", "-C", repo_root, "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git ls-files failed (exit {res.returncode})")

    findings: list[str] = []
    for raw_path in res.stdout.split(b"\x00"):
        if not raw_path:
            continue
        rel_path = raw_path.decode("utf-8", errors="replace")
        if _is_excluded(rel_path, exclude_paths):
            continue
        full_path = os.path.join(repo_root, rel_path)
        if not os.path.isfile(full_path):
            continue
        with open(full_path, "rb") as fh:
            raw = fh.read()
        if _is_binary_bytes(raw):
            continue
        text = raw.decode("utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat_num in _line_hits(line, patterns, allow):
                findings.append(f"{rel_path}:{lineno}: matched denylist pattern #{pat_num}")
    return findings


def scan_history(
    repo_root: str,
    rev_range: str,
    patterns: list[re.Pattern[str]],
    allow: list[re.Pattern[str]],
    exclude_paths: list[str],
) -> list[str]:
    revs = subprocess.run(
        ["git", "-C", repo_root, "rev-list", rev_range],
        capture_output=True,
        text=True,
        check=False,
    )
    if revs.returncode != 0:
        raise RuntimeError(f"git rev-list {rev_range!r} failed: {revs.stderr.strip()}")

    findings: list[str] = []
    for sha in [line.strip() for line in revs.stdout.splitlines() if line.strip()]:
        short_sha = sha[:12]
        seen_patterns: set[int] = set()

        msg_res = subprocess.run(
            ["git", "-C", repo_root, "log", "-1", "--format=%B", sha],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        for line in msg_res.stdout.splitlines():
            for pat_num in _line_hits(line, patterns, allow):
                seen_patterns.add(pat_num)

        diff_res = subprocess.run(
            ["git", "-C", repo_root, "show", "--format=", "--unified=0", sha],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        cur_path: str | None = None
        for line in diff_res.stdout.splitlines():
            if line.startswith("+++ "):
                target = line[4:].strip()
                if target == "/dev/null":
                    cur_path = None
                elif target.startswith("b/"):
                    cur_path = target[2:]
                else:
                    cur_path = target
                continue
            if line.startswith("+") and not line.startswith("+++ "):
                if cur_path and _is_excluded(cur_path, exclude_paths):
                    continue
                for pat_num in _line_hits(line[1:], patterns, allow):
                    seen_patterns.add(pat_num)

        for pat_num in sorted(seen_patterns):
            findings.append(f"{short_sha}: matched denylist pattern #{pat_num}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check tracked files against a local leak denylist.")
    parser.add_argument("--denylist", default=None, help="Path to denylist JSON file")
    parser.add_argument("--history", default=None, help="Optional git rev-range to scan commit messages and diffs")
    parser.add_argument("--repo", default=None, help="Repository root (default: auto-detect from cwd)")
    args = parser.parse_args(argv)

    repo_root = _repo_root(args.repo)
    denylist_path = resolve_denylist_path(repo_root, args.denylist)
    if not denylist_path:
        print("leak check: no denylist configured, skipped")
        return 0

    patterns, allow, exclude_paths = _load_denylist(denylist_path)
    findings = scan_tracked_files(repo_root, patterns, allow, exclude_paths)
    if args.history:
        findings.extend(scan_history(repo_root, args.history, patterns, allow, exclude_paths))

    for item in findings:
        print(item)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
