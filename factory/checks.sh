#!/usr/bin/env bash
# factory/checks.sh — deterministic verification gate run in every job worktree.
# When installing into a target project, replace the command below with your
# project's non-interactive test, typecheck, and lint suite (must not rewrite files).
set -euo pipefail

python3 -m unittest discover -s tests -v
