# Install the Software Factory (Google Antigravity / agy edition)

## Prerequisites

- Git repo with `origin` on GitHub; `gh auth status` shows push access.
- `agy --version` available.
- `herdr --version` available (0.9+).
- `python3 --version` 3.10+.
- Running inside a herdr pane (`HERDR_ENV=1`).
- None of the target files already exist.

## 1. Copy

```
.agents/skills/factory/SKILL.md
factory/agents/intake.md
factory/agents/spec-writer.md
factory/agents/builder.md
factory/agents/reviewer-code.md
factory/agents/reviewer-security.md
factory/agents/reviewer-ux.md
factory/agents/reviewer-ui.md
factory/agents/approver.md
factory/agents/researcher.md
factory/agents/architect.md
factory/config.json
factory/checks.sh
factory/bin/job
factory/bin/gate
factory/bin/route
factory/bin/spawn-agent
```

## 2. Adapt

- **`factory/checks.sh`**: Replace placeholder with real checks.
- **`factory/config.json`**: Set `base_branch`, `merge_method`, adjust models.
- **`factory/agents/*.md`**: Tailor checklists to your stack (except reviewer-security which uses ponytail-audit).

Note: Security review uses the ponytail-audit skill (already installed via the ponytail plugin in agy). The reviewer-security agent instructs agy to invoke /ponytail-audit for a thorough security audit of the codebase.
- **`AGENTS.md`** (or `GEMINI.md`): Add software factory section.
- **`.gitignore`**: Append `/factory/jobs/` and `/.worktrees/`.
- **Labels**: Create `factory`, `factory:needs-info`, `factory:needs-human` on GitHub.

## 3. Verify

1. `bash factory/checks.sh` exits 0.
2. `factory/bin/job queue` runs without error.
3. `python3 -m json.tool factory/config.json` succeeds.
4. `git check-ignore factory/jobs/x .worktrees/x` prints both paths.
5. `HERDR_ENV=1` is set.

## 4. Hand over

Commit the install. Ask before pushing.

Tell the user:
- Label one or two small issues `factory`, then run `/factory run` in agy from the project root (inside a herdr pane).
- Every job ends as a PR.
- Watch subagents work in real-time in separate herdr panes.
- To start carefully, set `concurrency` to 1.