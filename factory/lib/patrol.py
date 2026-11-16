"""Repository health patrol sensors and deduplicated GitHub issue creation."""
from __future__ import annotations

import importlib.util
import os
import time

from factory.lib.git_ops import GitHubClient
from factory.lib.state import list_jobs


def _load_patrol_hooks(repo_root: str):
    hook_path = os.path.join(repo_root, "factory", "patrol_hooks.py")
    if not os.path.isfile(hook_path):
        return None
    spec = importlib.util.spec_from_file_location("factory_repo_patrol_hooks", hook_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect_builtin_findings(repo_root: str) -> list[dict]:
    """Collect built-in repo health findings (repeated gate retries, stale REPO_MAP.md)."""
    findings: list[dict] = []
    fdir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(fdir, "jobs")

    repo_map = os.path.join(fdir, "REPO_MAP.md")
    if os.path.isdir(fdir) and os.path.isfile(repo_map):
        age_days = (time.time() - os.path.getmtime(repo_map)) / 86400.0
        if age_days > 14.0:
            findings.append(
                {
                    "fingerprint": "stale-repo-map",
                    "title": "Refresh stale factory/REPO_MAP.md",
                    "body": f"`factory/REPO_MAP.md` has not been updated in {age_days:.1f} days. Run `factory map` to refresh it.",
                }
            )

    if os.path.isdir(jobs_dir):
        multi_round = [j for j in list_jobs(jobs_dir) if int(j.get("round") or 0) >= 3]
        if len(multi_round) >= 3:
            ids = ", ".join(str(j.get("id")) for j in multi_round[:10])
            findings.append(
                {
                    "fingerprint": "repeated-gate-retries",
                    "title": "Investigate repeated multi-round gate/review retries",
                    "body": f"{len(multi_round)} jobs required >= 3 rounds ({ids}). Run `factory learn` and inspect `factory logs`.",
                }
            )
    return findings


def run_patrol(repo_root: str, *, dry_run: bool = False, gh: GitHubClient | None = None) -> list[dict]:
    """Run repo patrol sensors, deduplicate against open issues, and file new factory issues."""
    repo_root = os.path.abspath(repo_root)
    findings = collect_builtin_findings(repo_root)
    hooks = _load_patrol_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "collect_patrol_findings"):
        custom = hooks.collect_patrol_findings(repo_root) or []
        for item in custom:
            if isinstance(item, dict) and item.get("fingerprint") and item.get("title"):
                findings.append(item)

    if not findings:
        return []

    client = gh or GitHubClient(repo_root)
    open_issues = client.list_open_issues()
    open_blobs = [f"{iss.get('title') or ''}\n{iss.get('body') or ''}" for iss in open_issues]

    actions: list[dict] = []
    for f in findings:
        fp = str(f["fingerprint"]).strip()
        marker = f"[patrol:{fp}]"
        if any(marker in blob for blob in open_blobs):
            continue
        title = str(f["title"]).strip()
        body = f"{str(f.get('body') or '').strip()}\n\n{marker}".strip()
        if not dry_run:
            created = client.create_issue(title, body, labels=("factory",))
            actions.append({"fingerprint": fp, "title": title, "created": True, "url": created.get("url", "")})
        else:
            actions.append({"fingerprint": fp, "title": title, "created": False})
    return actions
