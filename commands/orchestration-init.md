---
description: Bootstrap the orchestration harness in the current repo (scaffold configuration, expose optional plugin hooks, check project acceptance criteria).
---

Set up the orchestration harness in THIS repository. Be conservative: detect,
propose, confirm before writing.

1. **Detect the stack.**
   - Branches: `git branch -r` -> infer the repository's configured branch roles.
   - CI check names: read `.github/workflows/*.yml` -> the `name:` of each job
     that gates merges (e.g. TypeScript, Vitest, Build, Playwright).
   - Pre-commit commands: read `package.json` scripts (typecheck/build/test) or
     the equivalent for the repo's language.
   - Ticket system: look for a tracker convention in branch names, recent
     commits, or repo docs. Default to `none` if unclear.

2. **Scaffold `.orchestration/config.yaml`** from the plugin's
   `templates/config.yaml`, filled in with what you detected. Show the user the
   filled config and let them correct it before writing. Keep
   `worktree_cleanup: manual` unless the user explicitly opts into automatic
   removal of clean, unlocked agent worktrees.
   After writing, validate it:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py validate-config
   ```
   If any role uses `execution: api`, instruct the user to create the gitignored
   `.orchestration/.env` beside the config and add only the needed
   `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY`. Container environment variables
   may provide the same names and take precedence. Never print or copy key values.
   If `ticket.kind: jira`, the same file (in the shared repository root) may hold
   `JIRA_API_TOKEN` and, for Jira Cloud, `JIRA_EMAIL`; tell the user to
   `chmod 600 .orchestration/.env`, because sprint preflight and sync refuse a
   group- or world-readable credential file.

3. **Project contract.** Confirm a `CLAUDE.md` (and ideally `AGENTS.md`) exists at the
   repo root -- the gate agents read it for the actual rules. If missing, offer
   to generate a starter (project summary, stack, hard rules, git workflow) and
   tell the user this is where the repo's KNOWLEDGE accrues over time. The plugin
   provides discipline; CLAUDE.md provides knowledge.

4. **Optional host hooks.** Do not copy hook commands into project settings.
   If this host exposes plugin hooks, show the user how to review/trust the
   bundled `hooks/hooks.json`. The scripted merge and configured cleanup paths
   remain authoritative without hooks. Gitignore `.orchestration/.gate-status/`,
   `.orchestration/.gate-logs/`,
   `.orchestration/.sprint-state/`, `.orchestration/.review-ledger/`,
   `.orchestration/.review-results/`, `.orchestration/.env`,
   `.orchestration/.llm-runs/`, `.orchestration/.llm-usage/`, and the worktree
   base.

5. **Branch protection (optional, recommended).** Offer to apply protection via
   `gh api`: required status checks on both branches (strict), no direct pushes,
   admin enforcement on production. Show the payload first; only apply on
   confirmation.

6. **Smoke and conformance test.** Validate the target configuration and smoke
   the hooks in this repo. Run
   `${CLAUDE_PLUGIN_ROOT}/scripts/run-plugin-conformance.sh` for the reusable
   merge-guard and worktree-cleanup suites. These tests remain in the plugin;
   never copy them (or plugin scripts/process docs) into the target repository.

Report what was created/changed and the one manual step left (usually: review
the generated config + CLAUDE.md).
