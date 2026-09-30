# Durable resource admission

Orka 1.8.8 implements the `orka.resource-claims/v1` contract in
`contracts/resource-claims-v1.json`. The supervisor applies it before reserving
a controller-authorized ticket.

## Claim model

A claim has five fields:

```json
{
  "kind": "migration",
  "key": "primary",
  "units": 1,
  "capacity": 1,
  "source": "jira-label"
}
```

`kind` and `key` form the resource identity. `units` and `capacity` model both
exclusive and shared resources; an exclusive resource is one unit with capacity
one. Every holder of the same identity must agree on its absolute capacity.

Supported kinds are repository, ticket, PR, worktree, migration, provider
route, visual QA, and heavy process. Orka automatically adds repository,
ticket, selected provider-route, and host-heavy-process claims. Authenticated
controller evidence adds PR and worktree claims when those identities exist.

Repositories can add exclusive claims using Jira labels:

```text
orka-resource-migration-primary
orka-resource-provider_route-private-endpoint
orka-resource-visual_qa-chromium
```

Only the reserved `migration`, `provider_route`, and `visual_qa` prefixes are
accepted. The suffix is the repository-owned resource key. Labels can only add
constraints; they cannot remove automatic claims or increase capacity.

## Admission and replay

For each plan, Orka snapshots claims held by reserved, running, and
launch-uncertain jobs. Candidates retain controller priority order. A candidate
whose requested units exceed a held capacity is skipped without a reservation,
attempt, worker, or queue mutation; evaluation continues with later independent
work. Claims admitted earlier in one fill cycle participate in subsequent
checks immediately.

The normalized claim set and SHA-256 digest are stored with the job. A terminal
transition records one release receipt. Duplicate terminal delivery reuses that
receipt. Launch-uncertain jobs retain claims because their external process may
exist.

After restart, Orka reconstructs held capacity only from durable supervisor
jobs. A changed digest, unknown schema, duplicate identity, conflicting
capacity, or over-capacity checkpoint fails closed. Active jobs created before
1.8.8 receive the deterministic automatic claim set once; Orka does not reset
their attempt or execution identity.

`concurrency_max` remains the outer worker-lane limit. The existing
`max_heavy_processes` value now independently limits the default
`heavy_process:host` claim.
