# Configurable Workflow Engine

The orchestration harness has three layers:

| Layer | Responsibility |
|---|---|
| Generic engine | Validates schema versions, branch roles, states, transitions, evidence, CI categories, approvals, artifact identity, tags, environment roles, adapters, and fail-closed transition attempts. |
| Workflow configuration | Declares the branch topology, state graph, guards, approval policy, environments, provider adapters, ticket behavior, reconciliation, cleanup, and rollback behavior for one repository. |
| Claude and Codex adapters | Expose the configured workflow through slash commands or skills. They call the same engine and scripts; they do not define release policy themselves. |

The engine entry point is:

```bash
scripts/orchestration-engine.py validate-config
scripts/orchestration-engine.py adapter-plan --host claude <transition>
scripts/orchestration-engine.py adapter-plan --host codex <transition>
scripts/orchestration-engine.py transition <candidate-id> <transition> ...
```

## YAML Subset And Formatters

Orka parses `.orchestration/config.yaml` without a YAML dependency, and every
reader (the engine, `lib-config.sh`, the context pipeline, and the sprint
controller) shares one parser for lists. Prettier output is supported, so a
repository can format the config with the same tooling as the rest of its YAML.
These list forms parse identically:

```yaml
llm:
  roles:
    implementer:
      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]
    code-reviewer:
      allowed_tools:
        [read_file, search, git_diff, git_status, run_check, apply_patch]
    sprint-worker:
      allowed_tools:
        [
          read_file,
          search, # comments and a trailing comma are allowed
          git_diff,
        ]
security_required_when:
  - migrations/
  - "SECURITY DEFINER"
security_required_source_branches: ["hotfix/**", "release/*"]
```

Continuation lines of a wrapped flow list must be indented deeper than its key
(a lone closing `]` may sit at the key's indentation). Quote items that contain
`,`, `[`, `]`, or ` #`. `validate-config` refuses what the subset does not
support, naming the line and printing a `hint:` with the supported spelling:
non-empty flow mappings such as `{block_squash: true}` (write a block mapping;
`{}` is accepted), nested flow lists, block scalars (`|`, `>`), multi-line quoted
or plain values, an unterminated `[`, and text after a closing `]` (quote a
string value that starts with `[`).

## Schema Versioning

Configs without `schema_version`, or with `schema_version: 1`, use the legacy
ticket pipeline keys such as `integration_branch`, `production_branch`,
`ci_checks_*`, `self_check`, and `verification`. Existing repositories keep that
behavior and do not automatically adopt release-candidate states.

`schema_version: 2` opts into the configurable state-machine engine. Unsupported
schema versions fail before mutation. Deprecated legacy fields may be read for
compatibility, but v2 workflow policy must be expressed in v2 blocks.

## Branch Roles

Branches are roles, not built-in topology:

```yaml
branches:
  production:
    name: trunk
    protected: true
  topic:
    template: "change/{ticket_key}-{slug}"
    temporary: true
```

A transition references roles:

```yaml
source_role: topic
destination_role: production
branch_operation: merge
```

If a workflow does not need a role, omit it. The engine rejects only roles that
are referenced but undefined.

Resolve a role for scripts or adapters with:

```bash
scripts/orchestration-engine.py branch-name topic \
  --var ticket_key=ABC-1 \
  --var slug=thing
```

## States And Transitions

The workflow graph is fully configured:

```yaml
workflow:
  state_dir: .orchestration/candidates
  states:
    - created
    - verified
    - merged
    - closed
  initial_state: created
  terminal_states:
    - closed
  transitions:
    - name: verify
      from: created
      to: verified
      required_evidence:
        - review_notes
      required_ci:
        - review
```

The engine rejects unknown states, undefined transitions, attempts to run a
transition from the wrong current state, missing required evidence, missing CI
categories, and state files that appear to have been manually edited to skip
the engine event history.

## Evidence And CI

Evidence is transition-specific. A transition can require different records than
another transition:

```yaml
required_evidence:
  - candidate_branch
  - artifact_recorded
required_ci:
  - candidate
```

Run the transition with concrete files and CI outcomes:

```bash
scripts/orchestration-engine.py transition rc1 start-verification \
  --evidence candidate_branch=.orchestration/evidence/rc1-branch.txt \
  --evidence artifact_recorded=.orchestration/evidence/rc1-artifact.txt \
  --ci candidate=green
```

## Approvals

Approvals are separate records, not state-file edits:

```yaml
approvals:
  qa:
    actor_types:
      - human
    human_required: true
    bind_to:
      - candidate_sha
      - artifact_id
```

Record approval through the engine or a configured external adapter:

```bash
scripts/orchestration-engine.py record-approval rc1 qa-approve qa alice human \
  --candidate-sha <sha> \
  --artifact-id <artifact>
```

When a transition requires approval, the engine checks the approval class, actor
type, candidate id, candidate commit identity, artifact identity, transition or
target state, and optional age limit. If the candidate identity changes after
approval, the old approval is stale and the transition fails closed.

## Artifacts And Tags

Transitions can bind to exact candidate and artifact identities:

```yaml
candidate_identity_required: true
artifact_identity_required: true
tag_required: true
```

`candidate_identity_updates: true` is available for configured transitions that
intentionally replace the candidate identity, such as returning fixes through a
source branch and rebuilding the candidate. This is explicit so approval
staleness is mechanical.

Immutable tag policy is configured in the workflow and release blocks. The core
checks for an attempted tag name and refuses to reuse an existing tag; signing or
annotation requirements should be enforced by the configured tag adapter or
repository branch/tag protection.

## Environments And Adapters

Environment roles are generic:

```yaml
environments:
  candidate:
    deploy_adapter: candidate-deploy
    verify_adapter: candidate-verify
  production:
    promote_adapter: production-promote
    verify_adapter: production-verify
```

Adapters are command or provider seams:

```yaml
adapters:
  candidate-deploy:
    command: "./ops/deploy candidate"
  production-promote:
    command: "./ops/promote exact-artifact"
```

The engine validates that referenced adapters exist. It does not parse provider
responses or contain provider-specific names. A repository with no deployment
integration can omit environments and adapters and still use branch/candidate
orchestration.

## Tickets

Ticket integration is optional:

```yaml
ticket:
  kind: none
```

or:

```yaml
ticket:
  kind: adapter
  adapter: ticket-provider
  release_membership: required_for_candidate
```

The engine handles generic concepts such as ticket identity, readiness evidence,
dependencies, release membership, and transition records. Provider details live
behind adapters.

## Migration

The safe migration path is opt-in:

1. Keep existing configs on schema v1.
2. Add tests around the legacy behavior that must remain.
3. Create a schema v2 fixture for the repository workflow.
4. Validate it with `orchestration-engine.py validate-config`.
5. Move Claude/Codex release and gate procedures to engine plans.
6. Enable v2 in the downstream repository only after the fixture passes.

The bundled `tests/fixtures/workflows/legacy-v1.yaml` proves legacy configs keep
their old branch and guard behavior. The v2 fixtures prove the engine supports
materially different topologies.

## Workflow Topologies

Mainline-only:

```text
created -> verified -> merged -> closed
```

Uses one protected branch role and a temporary topic role. There is no persistent
integration branch and no release-candidate environment.

Simple integration:

```text
created -> verified -> merged -> closed
```

Uses an integration role and a production role, but no candidate branch, artifact
promotion, or candidate environment.

## Example: Two-Branch Integration With Frozen Batch-QA Candidates

This is a worked example, not the plugin default. See
`tests/fixtures/workflows/gecktopia-adr-008.yaml`.

It declares protected integration and production-history branches, ticket
branches targeting integration, temporary frozen candidate branches, explicit
human QA approval, candidate merge into the production-history branch, production
promotion as a separate exact-artifact transition, release reconciliation and
cleanup, hotfixes that originate from production history and reconcile
immediately, immutable release identity and tags, and preview/integration/
candidate/production environment roles.
