# Codex plugin support

This repository is a Codex plugin folder. The Codex manifest lives at
`.codex-plugin/plugin.json` and exposes the natural-language skills in `skills/`.

Codex does not ingest the Claude Code slash-command files in `commands/`.
Current Codex hosts can discover lifecycle hooks from `hooks/hooks.json` after
the user reviews and trusts them, but that capability is host/version-dependent.
The plugin never relies on it: natural-language skills call the same controller,
merge, and cleanup scripts used by the Claude Code commands.

## What Codex gets

After install, Codex can invoke these skills by natural language:

| Skill | Purpose |
|---|---|
| `orchestrate-ticket` | Run one ticket end to end: implement, review, security, verify, merge |
| `orchestrate-sprint` | Run or resume a configured Jira sprint with dependency-aware bounded concurrency |
| `gate-pr` | Gate an existing PR and merge only after all green proof exists |
| `release-integration` | Advance a configured release/candidate transition through the shared engine |
| `orchestration-init` | Bootstrap `.orchestration/` config in a target repo |
| `scope-ticket` | Turn a thin ticket into testable acceptance criteria |
| `recover-agent-work` | Recover work from a stopped or interrupted agent worktree |

The skills reference role briefs in `agents/` and scripts in `scripts/`. A Codex
agent should resolve those plugin-relative paths to absolute paths, then run the
scripts with the target repository as the working directory. Configured
workflow policy is planned and enforced through `scripts/orchestration-engine.py`.
Sprint scheduling and recovery are enforced through
`scripts/sprint-controller.py`, the same state machine used by Claude Code's
`/orchestrate-sprint` command. Codex reserves each lane before launching a fresh
per-ticket task, reconciles uncertain running references after a restart, and
continues independent tickets past blockers.

## Hook behavior

When supported, Codex can discover `hooks/hooks.json`. Open `/hooks` to inspect,
trust, disable, or re-enable plugin-bundled hooks. A host that does not expose
them remains supported.

The hook commands use `${CLAUDE_PLUGIN_ROOT}` because Claude Code sets that
variable and Codex sets it for compatibility with existing plugin hooks. Codex
also provides `${PLUGIN_ROOT}` for Codex-specific hooks.

The `PreToolUse` merge guard is inert until the target repository opts into the
harness with `.orchestration/config.yaml`. In uninitialized repositories it
exits successfully without creating `.orchestration/` or blocking commands.
After initialization, it blocks raw `gh pr merge` commands unless a fresh
all-green marker matches the active plugin version and exact PR head/base
identity. `merge-on-green.sh` performs the same validation directly before the
sanctioned merge, so disabling or lacking the hook cannot bypass the gate.

The `Stop` worktree sweep is disabled by the safe default
`worktree_cleanup: manual`. With `auto`, both the skill's explicit post-merge
cleanup and the optional hook remove only clean, unlocked `agent-*` worktrees
under `worktree_base`; dirty and locked worktrees remain recoverable.

## Local install

Codex installs plugins from marketplace roots. The default personal marketplace
file is `~/.agents/plugins/marketplace.json`, and its plugin entries resolve
`./plugins/<name>` relative to your home directory.

For local development, clone or symlink this repository to:

```bash
~/plugins/orka
```

Then ensure `~/.agents/plugins/marketplace.json` contains this entry:

```json
{
  "name": "orka",
  "source": {
    "source": "local",
    "path": "./plugins/orka"
  },
  "policy": {
    "installation": "AVAILABLE",
    "authentication": "ON_INSTALL"
  },
  "category": "Developer Tools"
}
```

If the file does not exist yet, seed it as:

```json
{
  "name": "personal",
  "interface": {
    "displayName": "Personal"
  },
  "plugins": [
    {
      "name": "orka",
      "source": {
        "source": "local",
        "path": "./plugins/orka"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Developer Tools"
    }
  ]
}
```

Install it from the default personal marketplace:

```bash
codex plugin add orka@personal
```

The default personal marketplace is discovered implicitly by Codex; it does not
need `codex plugin marketplace add`.

## Team or Git marketplace

A non-default marketplace root should use this shape:

```text
<marketplace-root>/
  .agents/plugins/marketplace.json
  plugins/orka/
    .codex-plugin/plugin.json
    skills/
    agents/
    scripts/
```

The marketplace entry should point at `./plugins/orka`. Then add
and install from that marketplace:

```bash
codex plugin marketplace add <marketplace-root-or-git-url>
codex plugin add orka@<marketplace-name>
```

Use the `name` field from `.agents/plugins/marketplace.json` as
`<marketplace-name>`.

## Scripted merge path

For Codex and Claude workflows, keep merges on the scripted path:

```bash
scripts/merge-guard.sh --record-green <pr> [result_file]
scripts/merge-on-green.sh <pr> <branch> all-green <verify_path>
```

The hook is an optional local guardrail around agent tool calls. The sanctioned
scripted path enforces the same proof without it. Branch protection remains
the required out-of-band backstop for direct pushes, GitHub UI merges, or any
shell that does not run through trusted hooks.

## Native CLI budget enforcement

Controller-managed direct `codex exec` workers can use the shared admission ledger
through the Responses gateway. This requires API credentials and explicit model
pricing. See [native worker spending and supervision](sprint-controller.md#native-worker-spending-and-supervision)
for supported requests, nested launcher behavior, and compatibility limits.

### Compaction under the metered gateway

Codex compacts a long context in one of three ways. Only the first two are
metered.

- **Local compaction.** A normal streamed `POST /v1/responses` request whose
  turn metadata says `request_kind: compaction`. It is metered like any other
  turn, with the output cap enforced. Offline runs of codex-cli 0.154.0 against
  a loopback gateway always used this path for automatic mid-turn compaction.
  That covered `remote_compaction_v2` on and off, `gpt-5.5` and `gpt-5.6-sol`,
  and runs with and without the model catalog cache.
- **Remote compaction** (`POST /v1/responses/compact`). The 0.154.0 binary
  contains this client. The gateway meters it through the same path as
  Responses: stateless, standard-tier, client-tools-only checks; an input token
  count; a worst-case reservation; submission with the reservation as the
  idempotency key; and settlement from the returned `usage`. A response without
  valid `usage` keeps its reservation instead of being settled on a guess, and
  so does an ambiguous submission. Rejections (400, 401, 403, 404, 413, 422,
  429) release it. The compact endpoint receives only the fields Codex sent, so
  the output cap is applied only if the client supplied `max_output_tokens`.
  Settlement always records the provider's actual usage.
- **Responses compaction v2** (`context_management` on `/v1/responses`). This is
  stateful and is not metered. `launch_arguments` keeps
  `features.remote_compaction_v2=false` so Codex does not choose it.

Codex does not document an option that forces local compaction. Orka therefore
meters remote compaction instead of relying on client heuristics.

Any other endpoint or unmeterable request shape is rejected before provider
traffic. The rejection stops only the lane that sent it, with a
`client_incompatible: native gateway does not meter POST <path>` stop reason.
It never becomes a provider-wide hold. See
[provider health](sprint-controller.md) for the route-scoped hold and for using
`health-check --role sprint-worker --after-repair` to clear it.
