"""Repository onboarding (`init`, `map`) and deterministic `PITFALLS.md` distillation (`learn`)."""
from __future__ import annotations

import glob
import json
import os
import subprocess

from factory.lib.config import DEFAULT_CONFIG

REQUIRED_GITIGNORE = (
    "/factory/jobs/*",
    "!/factory/jobs/.gitkeep",
    "/.worktrees/",
)


def _list_repo_files(repo_root: str) -> list[str]:
    res = subprocess.run(
        ["git", "-C", repo_root, "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        return sorted(line.strip() for line in res.stdout.splitlines() if line.strip())
    found: list[str] = []
    skip_dirs = {".git", ".worktrees", "node_modules", "__pycache__", ".venv", "venv"}
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for fn in sorted(files):
            rel = os.path.relpath(os.path.join(root, fn), repo_root).replace(os.sep, "/")
            found.append(rel)
    return found


def detect_stack(repo_root: str) -> tuple[str, str]:
    """Detect primary stack and default checks.sh body."""
    if os.path.isfile(os.path.join(repo_root, "pyproject.toml")) or os.path.isfile(
        os.path.join(repo_root, "pytest.ini")
    ) or os.path.isfile(os.path.join(repo_root, "setup.py")):
        return "python", "#!/usr/bin/env bash\nset -euo pipefail\npython3 -m pytest -q\n"
    if os.path.isfile(os.path.join(repo_root, "package.json")):
        return "node", "#!/usr/bin/env bash\nset -euo pipefail\nnpm test\n"
    if os.path.isfile(os.path.join(repo_root, "Cargo.toml")):
        return "rust", "#!/usr/bin/env bash\nset -euo pipefail\ncargo test\n"
    if os.path.isfile(os.path.join(repo_root, "go.mod")):
        return "go", "#!/usr/bin/env bash\nset -euo pipefail\ngo test ./...\n"
    return "generic", "#!/usr/bin/env bash\nset -euo pipefail\necho 'No test suite configured yet'\n"


def generate_repo_map(repo_root: str) -> str:
    """Deterministically generate a concise (<100 lines) REPO_MAP.md for `repo_root`."""
    repo_root = os.path.abspath(repo_root)
    name = os.path.basename(repo_root) or "repository"
    files = _list_repo_files(repo_root)
    stack, _ = detect_stack(repo_root)
    manifests = [m for m in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod") if m in files or os.path.isfile(os.path.join(repo_root, m))]

    top_dirs: dict[str, int] = {}
    root_files: list[str] = []
    for rel in files:
        if "/" in rel:
            top = rel.split("/", 1)[0]
            top_dirs[top] = top_dirs.get(top, 0) + 1
        else:
            root_files.append(rel)

    lines = [
        f"# Repository Map: {name}",
        "",
        f"- **Detected Stack:** {stack}",
        f"- **Manifests:** {', '.join(manifests) if manifests else 'none'}",
        f"- **Tracked Files:** {len(files)}",
        "",
        "## Top-Level Directories",
    ]
    for d, count in sorted(top_dirs.items())[:25]:
        lines.append(f"- `{d}/` ({count} files)")
    if not top_dirs:
        lines.append("- (no subdirectories)")
    lines.extend(["", "## Key Root Files"])
    for rf in root_files[:20]:
        lines.append(f"- `{rf}`")
    if not root_files:
        lines.append("- (none)")
    lines.extend(["", "## Sample Paths"])
    for rel in files[:30]:
        lines.append(f"- `{rel}`")
    return "\n".join(lines[:95]) + "\n"


def init_repository(repo_root: str, *, force: bool = False) -> list[str]:
    """Scaffold factory/config.json, checks.sh, REPO_MAP.md, jobs/.gitkeep, and .gitignore."""
    repo_root = os.path.abspath(repo_root)
    fdir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(fdir, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    written: list[str] = []

    gitkeep = os.path.join(jobs_dir, ".gitkeep")
    if not os.path.exists(gitkeep):
        with open(gitkeep, "w", encoding="utf-8") as f:
            f.write("")
        written.append(gitkeep)

    cfg_path = os.path.join(fdir, "config.json")
    if force or not os.path.exists(cfg_path):
        cfg = {
            "allow_self_host": False,
            "base_branch": DEFAULT_CONFIG["base_branch"],
            "auto_merge": DEFAULT_CONFIG["auto_merge"],
            "protected_paths": list(DEFAULT_CONFIG["protected_paths"]),
            "models": dict(DEFAULT_CONFIG["models"]),
        }
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        written.append(cfg_path)

    _, checks_body = detect_stack(repo_root)
    checks_path = os.path.join(fdir, "checks.sh")
    if force or not os.path.exists(checks_path):
        with open(checks_path, "w", encoding="utf-8") as f:
            f.write(checks_body)
        os.chmod(checks_path, 0o755)
        written.append(checks_path)

    map_path = os.path.join(fdir, "REPO_MAP.md")
    if force or not os.path.exists(map_path):
        with open(map_path, "w", encoding="utf-8") as f:
            f.write(generate_repo_map(repo_root))
        written.append(map_path)

    gi_path = os.path.join(repo_root, ".gitignore")
    existing_lines: list[str] = []
    if os.path.isfile(gi_path):
        with open(gi_path, encoding="utf-8") as f:
            existing_lines = [ln.rstrip("\n") for ln in f]
    added_gi = False
    for entry in REQUIRED_GITIGNORE:
        if entry not in existing_lines:
            existing_lines.append(entry)
            added_gi = True
    if added_gi or not os.path.exists(gi_path):
        with open(gi_path, "w", encoding="utf-8") as f:
            f.write("\n".join(existing_lines) + "\n")
        written.append(gi_path)

    return written


def _extract_job_pitfalls(jdir: str) -> list[str]:
    bullets: list[str] = []
    for path in sorted(glob.glob(os.path.join(jdir, "round-*", "review-*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if data.get("verdict") == "PASS":
            continue
        for item in data.get("findings") or []:
            msg = item.get("message") if isinstance(item, dict) else str(item)
            clean = " ".join(str(msg or "").split()).strip()
            if clean:
                bullets.append(clean[:180])

    for path in sorted(glob.glob(os.path.join(jdir, "qa-*", "qa.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if data.get("verdict") == "PASS":
            continue
        for item in data.get("failures") or []:
            if isinstance(item, dict):
                sc = " ".join(str(item.get("scenario") or "").split())
                act = " ".join(str(item.get("actual") or "").split())
                msg = f"{sc}: {act}".strip(": ")
            else:
                msg = " ".join(str(item or "").split())
            if msg:
                bullets.append(msg[:180])

    for path in sorted(glob.glob(os.path.join(jdir, "round-*", "checks.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read()
        except OSError:
            continue
        if "VERDICT: FAIL" not in txt:
            continue
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("FAILED ") or s.startswith("ERROR ") or "AssertionError" in s:
                bullets.append(" ".join(s.split())[:180])
                break
    return bullets


def learn_from_jobs(repo_root: str, job_id: str | None = None) -> list[str]:
    """Distill deduplicated guardrails from multi-round/QA jobs into factory/PITFALLS.md."""
    repo_root = os.path.abspath(repo_root)
    fdir = os.path.join(repo_root, "factory")
    jobs_dir = os.path.join(fdir, "jobs")
    pitfalls_path = os.path.join(fdir, "PITFALLS.md")

    existing_bullets: list[str] = []
    seen_norm: set[str] = set()
    if os.path.isfile(pitfalls_path):
        with open(pitfalls_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("- "):
                    body = s[2:].strip()
                    existing_bullets.append(body)
                    seen_norm.add(body.lower())

    if not os.path.isdir(jobs_dir):
        return []

    target_ids = [job_id] if job_id else sorted(os.listdir(jobs_dir))
    newly_added: list[str] = []
    for jid in target_ids:
        jdir = os.path.join(jobs_dir, jid)
        jfile = os.path.join(jdir, "job.json")
        if not os.path.isfile(jfile):
            continue
        try:
            with open(jfile, encoding="utf-8") as f:
                job = json.load(f)
        except (OSError, ValueError):
            continue
        if not job_id and job.get("learned"):
            continue
        if int(job.get("round") or 0) < 2 and int(job.get("qa_round") or 0) < 1:
            continue

        for bullet in _extract_job_pitfalls(jdir):
            norm = bullet.lower()
            if norm not in seen_norm:
                seen_norm.add(norm)
                newly_added.append(bullet)
                existing_bullets.append(bullet)

        if not job.get("learned"):
            job["learned"] = True
            with open(jfile, "w", encoding="utf-8") as f:
                json.dump(job, f, indent=2)

    if newly_added:
        os.makedirs(fdir, exist_ok=True)
        with open(pitfalls_path, "w", encoding="utf-8") as f:
            f.write("# Repository Pitfalls & Guardrails\n\n")
            for b in existing_bullets:
                f.write(f"- {b}\n")

    return newly_added
