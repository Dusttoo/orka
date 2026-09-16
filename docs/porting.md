# Porting to a new repo

The harness is built so that adopting it in a new codebase is a configuration
task, not a fork. Nothing project-specific lives in the plugin; it all lives in
two places on the repo side: `.orchestration/config.yaml` (mechanics and, for
schema v2, workflow policy) and the repo's `CLAUDE.md` / `AGENTS.md` (the
knowledge the gate agents enforce).

## The fastest path

From inside the target repo, in a Claude Code session with the plugin installed:

```
/orchestration-init
```

It detects the branch model and CI check names, scaffolds
`.orchestration/config.yaml` for you to review, confirms a `CLAUDE.md` exists,
and gitignores the runtime marker directory. Reusable process docs, scripts, and
conformance tests remain in the plugin. The
rest of this doc is what that command sets up, for when you want to do it by hand
or understand what it produced.

## Checklist

1. **Choose a schema version.** Keep `schema_version: 1` for the legacy ticket
   pipeline. Use `schema_version: 2` only when you are ready to declare a full
   workflow graph with branch roles, states, transitions, evidence, approvals,
   environments, adapters, tags, and cleanup/reconciliation actions. Unsupported
   schema versions fail before mutation.

2. **Copy the config.** `templates/config.yaml` -> `.orchestration/config.yaml`.
   For schema v1, fill in:
   - `integration_branch` / `production_branch` from your branch model.
   - `merge_to_integration` / `merge_to_production` (`merge` keeps per-PR history
     on the integration branch; `squash` gives one commit per release).
   - `ci_checks_*`: the exact GitHub check-run `name:` values from your workflows.
     These must match exactly, since the gate polls them by name.
   - `self_check`: your pre-review commands. `typecheck` / `build` / `unit` for a
     typical setup, plus any repo convention (a hex-color grep, a lint rule, a
     codegen-drift check) as its own named entry. This is where a repo teaches
     the harness its own hard checks without editing any script.
   - `verification`: only if you have a heavy suite (e2e). Gate each entry to a
     target with `when:` (`production` for a release-only gate). Omit entirely
     otherwise.
   - `security_required_when`: diff path/content triggers that make the security
     gate mandatory (auth, migrations, payments, whatever your risk surface is).
   - `security_required_source_branches` / `security_required_target_branches`:
     shell-style branch patterns that force security review from authoritative
     PR metadata. Use these for policies such as every `hotfix/**` source branch
     or every PR targeting `main`, including harmless-looking component-only
     diffs.
   - `ticket`: `jira` / `github` / `none`.

   For schema v2, define branch roles instead of fixed branch names:
   ```yaml
   branches:
     production:
       name: trunk
       protected: true
     topic:
       template: "change/{ticket_key}-{slug}"
       temporary: true
   ```
   Then declare `workflow.states`, `workflow.initial_state`,
   `workflow.terminal_states`, and `workflow.transitions`.

3. **Point `rules_docs` at your knowledge files.** Usually `CLAUDE.md` and
   `AGENTS.md`. The gate agents read these to know the actual rules; this is
   where your project's conventions and past-incident lessons accrue. The plugin
   supplies the discipline; these files supply the knowledge, and they are what
   make the harness get smarter about *your* repo over time.

   Choose the global `llm.execution` route independently. Leave `desktop` for
   native Claude Code/Codex agents, or select `api` with an explicit provider
   and model. Add only the `llm.roles` overrides that differ from that global
   route. Configure a hard per-run budget and an explicit pricing entry for
   every API model; an unknown price fails closed. See `docs/llm-routing.md` and
   `docs/api-agent.md` in the plugin.

4. **Gitignore the runtime dirs.** Add `.orchestration/.gate-status/` (merge
   evidence), `.orchestration/.gate-logs/` (full failure output),
   `.orchestration/.sprint-state/`, `.orchestration/.review-ledger/`,
   `.orchestration/.llm-runs/`, `.orchestration/.llm-usage/`, and your
   worktree base if it is inside the repo. Keep `worktree_cleanup: manual` unless
   you explicitly want the
   orchestrator and trusted lifecycle hook to remove clean, unlocked worktrees.

5. **Optionally confirm hooks are active.** Claude Code and current Codex hosts
   can load the bundled `PreToolUse` merge guard and `Stop` worktree sweep after
   trust review. Host support varies, so hooks are defense in depth. The
   sanctioned merge script validates evidence directly, and configured cleanup
   runs explicitly. Do not vendor plugin hook commands into project settings.

6. **Branch protection (recommended).** The merge-guard governs the agent shell;
   branch protection closes the paths it cannot see (direct pushes, UI merges).
   Apply required status checks (strict) on both branches, disallow direct
   pushes, and enforce admin on production. `/orchestration-init` can show you the
   `gh api` payloads.

7. **Validate and smoke-test.** From the repo root:
   ```
   <plugin>/scripts/orchestration-engine.py validate-config
   ```
   Then:
   ```
   bash <plugin>/scripts/preflight.sh
   ```
   It checks git, `gh` auth, the configured base branch, and (only if present)
   language-specific tooling. Then run one small ticket through `/orchestrate` end
   to end and confirm the guard actually blocks a bare `gh pr merge` before the
   gates record a marker.

   Run the plugin-owned reusable safety suites without copying them into the repo:
   ```
   <plugin>/scripts/run-plugin-conformance.sh
   ```

## What is repo-specific vs harness-generic

| Repo-specific (you provide) | Harness-generic (the plugin provides) |
|---|---|
| Branch roles, state graph, CI categories, merge strategy | The state-machine validator and gate mechanics |
| The self-check and verification commands | The runner that executes them and reports |
| The rules in `CLAUDE.md` / `AGENTS.md` | The agents that read and enforce those rules |
| Which diffs need a security review | The security-review agent and its checklist |
| Provider commands and ticket systems | Adapter seams and fail-closed adapter validation |
| Project-specific acceptance tests | Reusable merge-guard and worktree-cleanup conformance tests |

Do not vendor plugin scripts, process docs, or generic safety tests into the
target repository. Its orchestration footprint is configuration plus the
project rules/acceptance criteria and tests that are genuinely project-specific.

## Language-agnostic notes

The harness assumes only git and the `gh` CLI for the legacy merge path.
`preflight.sh` checks Node tooling only when a `package.json` is present and
Playwright only when a Playwright config is present, so a non-Node repo is not
forced into either. `self_check`, `verification`, environment, deployment,
promotion, and ticket adapter commands are whatever your stack uses; the core
does not parse provider-specific responses.

For schema v2 details and examples, see
[workflow-configuration.md](workflow-configuration.md).
