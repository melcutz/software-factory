#!/usr/bin/env bash
# factory/checks.sh — deterministic verification gate run in every job worktree.
# Receives FACTORY_GATE_SCOPE=slice|full and FACTORY_BASE_BRANCH=<branch>.
# When installing into a target project, replace the test runner below with your
# project's non-interactive test, typecheck, and lint suite (must not rewrite files).
set -euo pipefail

if [[ "${FACTORY_GATE_SCOPE:-full}" == "slice" ]]; then
  mapfile -t scoped_tests < <(python3 -c '
import glob
import os
import subprocess

base = os.environ.get("FACTORY_BASE_BRANCH", "main")
res = subprocess.run(["git", "diff", "--name-only", f"origin/{base}...HEAD"], capture_output=True, text=True, check=False)
if res.returncode != 0:
    res = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True, check=False)
changed = [p.strip() for p in res.stdout.splitlines() if p.strip()]

targets: set[str] = set()
tokens: set[str] = set()
for path in changed:
    if path.startswith("tests/") and path.endswith(".py") and os.path.isfile(path):
        bname = os.path.basename(path)
        if bname.startswith("test_") or bname.endswith("_test.py"):
            targets.add(path)
    elif path.endswith(".py"):
        stem = os.path.basename(path)[:-3]
        if stem and stem not in {"__init__", "utils", "models"}:
            tokens.add(stem)

for tok in sorted(tokens):
    for match in glob.glob(f"tests/**/test_*{tok}*.py", recursive=True):
        if os.path.isfile(match):
            targets.add(match)

for t in sorted(targets):
    print(t)
')
  if [[ ${#scoped_tests[@]} -gt 0 ]]; then
    echo "    (slice scope: running ${#scoped_tests[@]} targeted test file(s): ${scoped_tests[*]})"
    PYTHONPATH=".:tests${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest -v "${scoped_tests[@]}"
  else
    python3 -m unittest discover -s tests -v
  fi
else
  python3 -m unittest discover -s tests -v
fi
