# Breaker classification contract

Orka 1.8.14 introduced the versioned
[`orka.breaker-classification/v1`](../contracts/breaker-classification-v1.json)
contract. Orka 1.8.15 applies its ticket and route classes to controller plans,
worker terminal transitions, retry wakeups, and supervisor status. Orka 1.8.16
enforces sprint pressure and hard global transitions. Checkpoint migration and
the final cross-version integration matrix remain a separate delivery slice.

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
