# Durable-state liveness contract

Contract: `orka.durable-liveness` schema version 1

Machine-readable source:
[`contracts/durable-liveness-v1.json`](../contracts/durable-liveness-v1.json)

The contract is a bounded, repository-agnostic model of the state dimensions
that have historically trapped Orka work even though every individual safety
check behaved correctly. It proves that each modeled reachable nonterminal
state has at least one supported exit without weakening review, security,
spending, merge, or destructive-action gates.

This is a design and conformance boundary. It does not yet authorize production
supervisor decisions. Production integration remains part of issue #129.

## What is modeled

The model deliberately includes only state needed to distinguish liveness:

- ticket state and attempt identity;
- execution-unit liveness;
- PR recovery and moved-head state;
- review generation state;
- usage reservation settlement;
- provider-route and sprint breakers; and
- one independent peer ticket, used to prove that a ticket-local hold does not
  become a sprint-global stop.

Each transition has one of five exit classes:

1. `automatic` — controller or supervisor work requiring no new authority;
2. `timer_driven` — a durable deadline or scheduled health probe;
3. `authenticated_external_evidence` — a provider, Jira, GitHub, worker, review,
   or preflight receipt;
4. `operator_authorized` — a named command backed by a named authority class;
5. `terminal_failure` — an explicit, observable terminal outcome.

An operator action is not accepted merely because prose says to ask a human.
The contract must name both the supported command and the authority that grants
it. Terminal states cannot mutate back into active work unless a transition is
marked `explicit_recovery`.

## Derivation from authoritative contracts

`scripts/durable_liveness.py` builds a source-symbol registry directly from the
existing supervisor lifecycle, breaker, recovery, and phase-worker contracts.
It also reads the declared review actions and literal CLI commands from the
review ledger, sprint controller, API usage reconciler, and supervisor parsers.
Every liveness transition cites those source symbols. Removing or renaming an authoritative
event, breaker class, outcome, action, or command therefore breaks conformance
instead of leaving a second copy of policy silently stale.

The liveness model does not replace those sources. They continue to define
evidence schemas, gate behavior, transition mechanics, and authority checks.

## Bounded reachability

The verifier performs deterministic breadth-first exploration from the normal
queued state and the sanitized regression fixtures. For every reached state it
checks:

- a nonterminal state has at least one exit;
- terminal mutations are explicit recovery transitions;
- a queued peer can advance around a ticket-local hold;
- every transition and every exit class is exercised; and
- the exploration remains below its declared finite bound.

When it finds a sink, it reports the seed, state, and shortest transition trace
that reproduces it. A state directly supplied by a regression fixture has an
empty trace, while a defect reached after admission reports the exact preceding
steps.

Run the conformance suite without provider or repository credentials:

```bash
python3 scripts/durable_liveness.py \
  contracts/durable-liveness-v1.json \
  --fixtures tests/fixtures/durable-liveness-v1.json
bash tests/durable-liveness.test.sh
```

## Historical regression fixtures

The fixture set includes sanitized shapes for:

- tokenless repair with a preserved PR;
- an escalated repaired head still awaiting its required review;
- a PR whose exact head moved after a completed generation;
- stale attempt identity with ambiguous execution evidence; and
- preserved-PR recovery after the original execution ended.

It also contains a two-ticket case proving independent work can dispatch while
the first ticket awaits an operator decision.

Ambiguous provider reservations are not cleared by operator recovery. The
model requires authenticated provider reconciliation first and only then permits
the separately authorized stale-attempt recovery transition.

A hard sprint-budget pause similarly has two distinct exits: root first raises
the repository's standing budget policy, then the normal supervisor operator
resumes the exact breaker generation after synchronization records a resolution
receipt. A ticket-scoped budget grant is never modeled as sprint authority.

## Extending the model

When a production change adds a durable state or exit:

1. add the value to the smallest applicable dimension;
2. add transitions that cite the existing authoritative source symbols;
3. use a named operator command and authority for every human-authorized exit;
4. add a sanitized fixture for the new boundary or historical failure;
5. run the verifier and inspect any shortest counterexample; and
6. only then wire the accepted transition into the production supervisor.

Do not add a generic “ask the operator” edge, treat unknown execution state as
death, erase history to make a state reachable, or mark a state terminal only to
silence the verifier. Such changes hide deadlocks instead of providing a safe
exit.
