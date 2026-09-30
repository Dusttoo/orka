# Orka 2: durable supervisor runtime

Status: accepted architectural direction; implementation in independently
releasable slices. Orka 1.8.14 provides the detached supervisor, repository
lease lifecycle, deterministic synchronization/planning loop, controller-owned
desktop/API worker dispatch, versioned terminal-result transitions, ticket-local
parking, durable retry wakeups, classified operator-decision resolution, and
authenticated restart after a mechanically proven supervisor death,
capacity-aware durable resource admission, starvation-bounded fair lane
allocation, a versioned recovery eligibility contract, automatic recovery of
exact mechanically proven-dead worker attempts and authenticated preserved PRs,
and recovery-specific crash/replay proofs.

Orka's controller has durable checkpoints and mechanical gates, but a
model-driven captain still performs the outer `plan -> launch -> wait -> finish
-> replan` loop. Operational ambiguity can therefore end an orchestration turn
even when unrelated work remains. Orka 2 moves that loop into deterministic,
long-lived host code.

## Goal

A sprint should continue making safe progress until it is complete, explicitly
paused, globally unable to proceed, or out of its hard budget. A ticket-level
failure should park or recover that ticket without stopping independent work.

## Target architecture

```text
CLI / future web console
          |
          v
durable supervisor process
  |-- transactional scheduler and event journal
  |-- dependency, priority, exclusion, and capacity policy
  |-- provider health and budget circuit breakers
  |-- worker lifecycle and recovery
  `-- status and decision queue
          |
          v
disposable phase workers with fresh model context
  scope -> design -> implement -> review -> repair -> merge
```

The supervisor never implements tickets or reviews its own workers. It owns
only deterministic lifecycle operations. AI workers remain disposable and
receive fresh context for each ticket or phase.

## Required properties

### Durable supervisor

- One repository lease elects the active supervisor.
- A heartbeat and process identity permit safe takeover after proven death.
- Closing an interactive Claude or Codex session does not stop the sprint.
- Restart reconstructs work from the event journal and authenticated external
  state rather than model memory.

### Explicit post-job transitions

Every worker completion maps to one mechanical transition:

- `completed`
- `continue_same_ticket`
- `repair_existing_pr`
- `retry_after_cooldown`
- `decompose_and_queue_children`
- `park_for_decision`
- `park_for_external_dependency`
- `available`

Malformed worker output is a classified worker failure, not an undefined
controller state.

### Durable queues and exclusions

Skipped work remains queued. Jobs declare resource exclusions such as
repository, ticket, PR, worktree, migration sequence, provider route, visual QA,
or heavy-process capacity. A blocked exclusion delays only conflicting jobs.
The implemented v1 claim contract is documented in
[Durable resource admission](resource-admission.md). Multi-lane admission uses
durable repair, recovery, continuation, dependency-unlocking, and fresh queue
classes. One dependency-unlocking lane is reserved when eligible; remaining
capacity follows a weighted rotating cursor. A one-lane repository stays
strictly finish-first.

### Bounded autonomy

- Soft ticket breakers park, recover, or decompose one ticket and continue.
- Hard ticket breakers require authority only for that ticket.
- Provider breakers pause only routes that depend on that provider.
- A sprint hard budget, lease loss, corrupt state, or explicit pause stops new
  admission globally.
- Review, security, merge, and destructive-action gates remain fail-closed.

### Fair throughput

Repair and recovery remain preferred, but they do not consume every lane
indefinitely. Capacity policy reserves room for dependency-unlocking fresh work
while bounding unfinished PRs and heavy processes.

### Operator decisions

Human action is reserved for product/security choices, accepting known gate
failures, destructive operations, increasing hard cost ceilings, and genuinely
ambiguous external side effects. Ordinary process death, validated timeouts,
malformed reviewer output, and mechanically verified PR recovery are runtime
concerns.

## Proposed delivery sequence

1. Specify lifecycle transitions, invariants, and event semantics in the
   [versioned supervisor contract](supervisor-lifecycle-contract.md).
2. Add a host-owned supervisor loop around the existing controller, beginning
   with the [detached lease and planning process](sprint-supervisor.md).
3. Connect controller-owned worker dispatch and terminal-result handling.
4. Add durable resource exclusions and fair queue admission. **Complete in
   1.8.8 and 1.8.9.**
5. Automate evidence-backed worker and preserved-PR recovery. **The eligibility
   contract is complete in 1.8.10, proven-dead worker recovery is automatic in
   1.8.11, authenticated preserved-PR reconciliation is automatic in 1.8.12,
   and the recovery crash-boundary/idempotency matrix is complete in 1.8.13.**
6. Add soft/hard breakers and ticket parking.
   **The canonical breaker taxonomy and legacy-state mapping are complete in
   1.8.14. Ticket/route runtime enforcement, sprint pressure/global breakers,
   and migration/status integration remain separate slices.**
7. Introduce a transactional event store with checkpoint import/export.
8. Add supervisor lease takeover and crash recovery.
9. Expose stable status, pause, resume, and decision CLI commands.
10. Prove the design with restart, timeout, provider-loss, and malformed-output
   chaos tests.

Each step must be independently releasable and keep existing Orka 1.x
configuration working until a documented migration is available.

## Non-goals

- Removing independent review or security gates.
- Allowing the supervisor to write product code.
- Reusing one unbounded model conversation across tickets.
- Treating green CI, worker agreement, or Jira status alone as merge authority.
- Hiding parked or failed work to make completion metrics look better.

## Contribution boundary

Cross-cutting state-machine or persistence changes require an accepted
architecture issue before implementation. Focused tests, documentation,
adapters, observability, and isolated transition handlers can proceed as
separate issues when their contracts are already accepted.
