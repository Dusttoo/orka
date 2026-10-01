# Breaker classification contract

Orka 1.8.14 introduced the versioned
[`orka.breaker-classification/v1`](../contracts/breaker-classification-v1.json)
contract. Orka 1.8.15 applies its ticket and route classes to controller plans,
worker terminal transitions, retry wakeups, and supervisor status. Orka 1.8.16
enforces sprint pressure and hard global transitions. Orka 1.8.17 completes
deterministic checkpoint migration, stable status categories, and the
cross-version restart and unsafe-reclassification matrix.

Every stop source has exactly one class along two independent axes:

- scope: `ticket`, `route`, or `sprint`;
- strength: `soft` or `hard`.

Each class declares its durable state, wake condition, authority, evidence,
and whether it may change global supervisor state. Ticket and route classes
may never do so. Skipping them remains non-consuming and may not stop unrelated
work.

## Current classes

| Scope | Soft | Hard |
| --- | --- | --- |
| Ticket | retry wait, recovery, decomposition, external wait | scoped decision, irrecoverable block |
| Route | transient provider hold | authentication or incompatible-route hold |
| Sprint | capacity pressure, all-routes wait | sprint budget, preflight/lease/state integrity stop |

“Hard” does not mean “discard work.” It means controller evidence alone cannot
authorize more of the prohibited action. Ticket and route hard breakers still
preserve the queue, attempt, spend, review, PR, worktree, and dependency state.

## Protected controls

The validator fixes budget ceilings, run/review/repair limits, failed review,
security and merge gates, destructive actions, permanent launch/worker
failures, authentication/incompatibility holds, preflight failure, lease loss,
and durable-state corruption to immutable hard classes at their accepted
scope. A repository cannot relabel these as soft or move a sprint budget into
ticket scope.

## Validation

Run:

```bash
python3 scripts/breaker_contract.py \
  contracts/breaker-classification-v1.json \
  --controller scripts/sprint-controller.py
bash tests/breaker-contract.test.sh
```

Validation fails closed for missing, extra, duplicated, ambiguous, undefined,
or unsafe mappings. Current controller ticket states must also have an exact
compatibility entry. No validation uses live Jira, GitHub, provider, or email
credentials.

## Runtime records

`scripts/breaker_runtime.py` turns a classified stop into a deterministic
record containing the source, class, scope, strength, durable state, authority,
subject, and evidence digest. Ticket records bind to one Jira key. Route records
bind to one exact route identity and role. These records cross the controller,
planning snapshot, and supervisor status boundary without granting new
authority or erasing existing state.

Worker terminal outcomes use the contract as the target-state authority. A
timeout without progress, for example, may enter `retry_wait` only with a
`ticket_retry_wait` record for that exact ticket. The deadline alone is
insufficient to wake it: the supervisor also verifies the ticket binding and
durable state before requeueing the stopped attempt. Malformed or mismatched
records fail closed.

Controller decisions preserve their ticket-local breaker records, while
provider holds publish route-local records. Healthy roles and unrelated
tickets remain eligible, and skipping a held candidate consumes no lane,
attempt, or reservation.

Sprint pressure records are also deterministic, but only their class may move
the supervisor to `degraded`. Unfinished-PR, lane-capacity, and heavy-process
pressure reduce new admission through their existing queue/capacity policy;
they never cancel active work. Route records cannot change supervisor state.

`all_routes_unavailable` and the authenticated absolute sprint budget are the
only breaker-driven pauses. Lease loss, invalid state, and preflight failure are
the hard integrity stops. Each activation receives a durable generation derived
from the repository lease generation, monotonic breaker sequence, and evidence
record. Restart restores that exact generation. A hard-budget resume requires a
new authoritative budget receipt and an operator request bound to the stored
generation; duplicate activation or request delivery is a no-op.

## Checkpoint migration and status

Supervisor state schema v2 imports an authenticated schema-v1 checkpoint once.
The migration keeps the entire dispatch journal, attempt and execution-unit
bindings, spend snapshot, review and PR references, dependency graph, request
history, and lifecycle history. Its content-addressed receipt binds the old and
new runtime and lifecycle-contract fingerprints, allowing an unclean restart
to cross this one declared compatibility boundary without weakening ordinary
runtime-fingerprint checks.

Existing ticket and route records are revalidated against the canonical
contract. A protected hard source that was stored as soft or at another scope
is rejected. Unknown ticket stops, provider holds without an exact route, and
paused global state without its required receipt fail closed with an actionable
migration error. A legacy route-only `degraded` state returns to global
`active` while retaining the local route hold; it cannot silently preserve an
obsolete global pause.

`sprint-supervisor.py status` exposes the stable categories `queued`, `active`,
`retrying`, `parked`, `route_held`, `pressure_limited`, `globally_paused`,
`blocked`, and `terminal`. Parked, retrying, blocked, repair, and recovery work
is never counted as terminal or hidden as completed/exhausted.

## Epic acceptance trace

| #75 criterion | Contract and proof |
| --- | --- |
| Every existing stop is classified | `stop_sources` is an exact validated inventory; missing or extra sources fail `breaker-contract.test.sh`. |
| Ticket stops remain local | Controller and dispatch fixtures keep unrelated launch candidates eligible. |
| Route holds remain local | Planning requires every role needed by the current plan to be held before a global wait. |
| Only explicit global conditions pause/stop | The lifecycle validator rejects every non-global supervisor state change. |
| Parked work remains visible and resumable | Status categories, recovery bindings, and migration fixtures preserve the exact ticket and attempt. |
| Transitions survive replay/restart | Ticket wakeups and global generations are idempotent; unclean takeover restores their exact evidence. |
| Hard controls cannot be bypassed | Contract mutation and schema-v1 migration tests reject softened budget, review, security, and merge sources. |
| Status separates every work class | The schema-v2 status fixture covers all nine stable categories and excludes parked work from terminal metrics. |

All fixtures are offline and require no Jira, GitHub, model-provider, email, or
production credential.
