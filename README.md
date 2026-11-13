# Software Factory for Google Antigravity (agy) (V2)

A software factory that works through your GitHub issues. Label an issue `factory`, run `/factory run`, and agents decide whether it's worth building, write a spec, build it, test it with your own checks, review it, and open a pull request that closes the issue.

## Adapted from

[ZazenCodes' Software Factory V2](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2) — adapted for Google Antigravity (agy) with Herdr for subagent spawning.

## What's different from the OpenCode version

| OpenCode version | agy (Antigravity) version |
|---|---|
| `.opencode/agents/*.md` (project-level agents) | `factory/agents/*.md` (agent definitions passed via prompt) |
| `.opencode/skills/` | `.agents/skills/` |
| `opencode run --agent X` | `agy -p` with agent definition prepended |
| OpenCode Go models | Google Gemini models (via agy) |
| `--auto` for permissions | `--dangerously-skip-permissions` |

## Model routing (Google Gemini via agy)

| Role | Model | Notes |
|---|---|---|
| Intake | gemini-3.8-flash-medium | Good reasoning, moderate cost |
| Spec writer | gemini-4-argon-high | Strong reasoning |
| Builder | gemini-4-argon-high | Strong coding |
| Code reviewer (low risk) | gemini-3.6-flash-low | Cheapest |
| Code reviewer (med/high) | gemini-3.8-flash-low/medium | Moderate |
| Security reviewer | gemini-4-argon-high | Uses ponytail-audit skill for thorough security audit |
| UX reviewer | gemini-3.8-flash-medium | Moderate |
| UI reviewer | gemini-3.6-flash-low | Cheapest |
| Approver | gemini-4-argon-high | Strong reasoning |

## Install

See INSTALL.md.

## Incorporated skills from mattpocock/skills

The factory agents now incorporate principles from [mattpocock/skills](https://github.com/mattpocock/skills):

- **TDD** (builder): Red-green-refactor, test at seams, anti-patterns
- **Code review** (reviewer-code): Two-axis review (Standards + Spec)
- **to-spec** (spec-writer): Spec template with user stories, implementation decisions, testing decisions
- **Grilling** (intake): Relentless interview discipline for NEEDS-INFO
- **Domain modeling** (spec-writer): Build/sharpen domain model, update GLOSSARY.md, offer ADRs
- **Implement** (builder): TDD at seams, regular checks, code-review at end
- **Improve codebase architecture** (architect): Survey for deepening opportunities, HTML report
- **Research** (researcher): Investigate against primary sources, write findings to Markdown
- **PR body** (builder): Summary + evidence + merge-danger call

## License

GPLv3 (same as original).