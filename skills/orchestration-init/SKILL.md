---
name: orchestration-init
description: Bootstrap the orchestration harness in a target repo by creating configuration, checking project acceptance criteria, and wiring plugin-owned guardrails. Use when the user asks to initialize, install, bootstrap, or set up orchestration in a repository, including the equivalent of the Claude /orchestration-init command.
---

# Initialize orchestration in a target repo

Set up the orchestration harness in the current repository. Be conservative:
detect the repo's mechanics, show the inferred config, and get confirmation
before writing repo files when running interactively.

## Plugin paths

The templates and scripts below live in this plugin, not necessarily in the
target repository. Resolve these paths from this skill file before reading or
executing them:

- `../../templates/config.yaml`
- `../../hooks/hooks.json`
- `../../scripts/orchestration-engine.py`
- `../../scripts/preflight.sh`
- `../../scripts/merge-guard.sh`
- `../../scripts/sweep-agent-worktrees.sh`
- `../../scripts/run-plugin-conformance.sh`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

1. Detect the target repo's stack:
   - branches from `git branch -r`, inferring configured branch roles
   - CI check names from `.github/workflows/*.yml`
   - self-check commands from `package.json` or the repo's language equivalent
   - ticket system from branch names, recent commits, or repo docs, defaulting
     to `none` when unclear
2. Scaffold `.orchestration/config.yaml` from `templates/config.yaml`, filled
   with the detected values. Show the filled config and let the user correct
   branch names, checks, ticket settings, and `worktree_cleanup` policy before
   writing. Keep its safe `manual` default unless the user explicitly opts into
   automatic removal of clean, unlocked worktrees. After writing, run
   `orchestration-engine.py validate-config`.
   If any role uses API execution, instruct the user to put the relevant
   `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in `.orchestration/.env` beside
   the config. Container environment values take precedence. Never display or
   copy secret values. If `ticket.kind: jira`, the same file (in the shared
   repository root) may hold `JIRA_API_TOKEN` and, for Jira Cloud, `JIRA_EMAIL`;
   tell the user to `chmod 600 .orchestration/.env`, because sprint preflight and
   sync refuse a group- or world-readable credential file.
3. Confirm a repo rules document exists. Prefer both `CLAUDE.md` and `AGENTS.md`
   in `rules_docs`; if missing, offer to create a starter so the project's real
   conventions have a place to live.
4. Gitignore `.orchestration/.gate-status/`, `.orchestration/.gate-logs/`,
   `.orchestration/.sprint-state/`,
   `.orchestration/.review-ledger/`, `.orchestration/.review-results/`, `.orchestration/.env`, `.orchestration/.llm-runs/`,
   `.orchestration/.llm-usage/`, and the configured worktree base.
5. Do not copy hook commands into project settings. Claude Code and current
   Codex hosts may discover `hooks/hooks.json` after user review/trust, but
   support varies by host/version. Show the host's hook-review path when
   available. Never require registration: `merge-on-green.sh` validates
   all-green evidence directly and the skill performs configured cleanup.
6. Optionally propose branch protection via `gh api`: strict required status
   checks, no direct pushes, and admin enforcement on production. Show the
   payload before applying it.
7. Validate config and smoke-test the hooks in the target repo. Then run
   `run-plugin-conformance.sh`, which exercises the reusable merge-guard and
   worktree-cleanup suites from the plugin itself. Do not copy plugin tests,
   scripts, or process docs into the target repository. The target owns only its
   configuration, rules/acceptance criteria, and project-specific tests.

Report the files created or changed and any manual review left, especially
config values and rules-doc content.
