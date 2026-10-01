# Breaker classification contract

Orka 1.8.14 introduces the versioned
[`orka.breaker-classification/v1`](../contracts/breaker-classification-v1.json)
contract. It is an inventory and safety contract; later #75 slices apply it to
runtime transitions.

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
