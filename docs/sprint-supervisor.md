# Host-owned sprint supervisor

Orka 1.8.9 extends the detached Orka 2 supervisor with a deterministic outer
planning loop. The host process owns one repository lease, synchronizes through
the existing authenticated Jira/GitHub/controller/provider adapters, publishes
controller-authorized work, fills available lanes through controller-owned
desktop or API execution units, applies terminal results, and sleeps until an
operator event or durable deadline.

## Runtime boundary

The supervisor stores private runtime evidence under
`.orchestration/.supervisor/` in the repository's shared Git checkout. Linked
worktrees therefore resolve the same lease and state. The control socket is
repository-derived, user-owned, and mode `0600`; the runtime directory and
state are mode `0700` and `0600` respectively.

The state snapshot records:

- the lifecycle contract and runtime fingerprints;
- the resolved repository identity;
- the lease UUID, monotonic generation, lock inode, acquisition, and release;
- the exact host process birth identity and detached session;
- a separate durable digest receipt for the last fully persisted snapshot;
- bounded transition and operator-request history;
- the latest synchronized plan, evidence digests, hard-budget receipt, and next
  wake deadline.
- exact job-to-attempt and execution-unit bindings, accepted terminal-result
  digests, contract events, and resulting job states.
- normalized resource claims, claim-set digests, conflict receipts, and
  exact-once release receipts.

It never writes provider, Jira, GitHub, or application credentials.

## Commands

Run from a configured repository, or pass `--repo <path>`:

```bash
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" start
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" status
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" pause --request-id maintenance-1
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" resume --request-id maintenance-2
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" drain --request-id maintenance-3
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" resolve-decision \
  --ticket PROJ-123 \
  --decision-class product_or_security_policy \
  --decision-receipt "decision-record-id" \
  --reason "approved repository policy"
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" stop \
  --request-id maintenance-4 --reason "operator maintenance"
```

`start` launches a new session with standard streams detached to the private
supervisor log, waits for an authenticated startup handshake, then returns. The
process therefore survives the invoking Claude, Codex, terminal, or SSH
session. A second start cannot acquire the lease and does not disturb the live
control socket.

Operator request IDs are idempotency keys. Repeating the same command with the
same ID returns its prior result. Reusing an ID for another command is rejected.

## Deterministic planning

When `sprint_id` is configured, startup enables deterministic planning. The
supervisor writes the controller's empty inventory template itself, invokes the
controller-owned authenticated Jira adapter, then asks the existing controller
for `plan` and `summary`. It never constructs ticket inventory, dependencies,
provider evidence, or a plan with model inference. Replaying identical evidence
produces the same plan digest and does not append a duplicate plan-change event.

The supervisor wakes for:

- an operator command on its private control socket;
- the next provider cooldown or ticket retry deadline;
- the configured `supervisor_sync_interval_seconds` deadline (default 60);
- its lease and durable-state integrity deadline.

It performs no periodic model turn. A provider probe uses the existing bounded
provider-health adapter and a failed route remains durable, waitable work.

## Lifecycle behavior

- `start` records `starting -> active` only after the repository configuration,
  lifecycle contract, exact process identity, and exclusive lease validate.
- `pause` records `active -> paused` and suspends planning.
- `resume` records `paused -> active` and immediately replans. It cannot bypass
  a hard sprint budget or an all-routes-unavailable pause.
- `drain` records `active -> draining -> paused` and delays `drain_completed`
  until its authenticated running set is empty.
- `stop` records the global `operator_stopped` transition, returns a response,
  closes the control socket, and releases the lease exactly once.
- replacing or unlinking the named lease inode records `lease_lost` and stops
  fail-closed.
- authenticated sprint completion records `sprint_completed` and stops.
  Controller exhaustion without completion remains visible and waits for the
  next authoritative change instead of pretending the sprint is done.
- a hard sprint budget or unavailable execution routes pauses admission. Route
  cooldown deadlines continue to wake and recheck automatically.

Blocked, parked, and decision-bound tickets stay ticket-local. If another ticket
is controller-authorized, it remains in the published plan. This slice reports
and dispatches that work without another captain turn. A worker must return the
`orka.worker-terminal-result/v1` envelope bound to the exact sprint, ticket,
attempt token, and controller invocation. The supervisor verifies completed PRs
against GitHub, maps accepted results through the versioned lifecycle contract,
and immediately replans when a lane exits. Duplicate delivery is a journaled
no-op; stale bindings are rejected; malformed or missing output moves only that
ticket to recovery-ready.

`status` separates active, queued, retrying, parked, blocked, and terminal job
sets. A no-progress timeout enters a durable retry wait for
`supervisor_ticket_retry_seconds` (default 30) and consumes no lane. Once its
deadline arrives, the supervisor requeues only that exact stopped attempt after
verifying its ticket-scoped breaker record, and immediately replans. A deadline
with a missing or mismatched breaker binding remains parked. Controller-authorized
repair and recovery continuations are returned to the launch queue without
holding unrelated capacity.

Planning and status expose deterministic `ticket_breakers` and
`route_breakers`. Ticket records bind the stop source, target state, authority,
and evidence digest to one Jira key. Route records bind a provider incident to
one exact role and route identity. Neither class changes global supervisor
state, consumes admission for a skipped candidate, or prevents a healthy route
from filling another lane. See
[Breaker classification contract](breaker-classification-contract.md).

`pressure_breakers` describe unfinished-PR, lane, and heavy-process pressure.
They move the supervisor to `degraded` while the existing queue and resource
policies reduce only new admission; running workers continue to terminal state.
A route hold by itself never changes global lifecycle state. The supervisor
pauses only when every route required by otherwise eligible work is held, or
when the authenticated absolute sprint budget is exhausted.

Every global breaker activation owns a durable generation. Duplicate delivery
of the same evidence is a no-op, and unclean takeover restores the exact stored
generation. Route recovery clears its matching all-routes generation
automatically. A hard-budget generation remains paused after the ceiling is
raised until an operator resume consumes the new budget receipt against that
generation. Status exposes the active global breaker without exposing
credentials.

Admission is queue-class aware. A single-lane repository remains strictly
finish-first. With two or more lanes, eligible dependency-unlocking work gets
one reserved slot and the remaining slots rotate through weighted repair,
recovery, continuation, and fresh queues. Repair and recovery therefore remain
preferred without becoming a global pause. The cursor and each selection's
class, slot, action, and reason are durable and visible in `status`, so repeated
planning cannot starve a continuously eligible queue.

Before reservation, the supervisor evaluates the ordered controller plan
against resources held by `reserved`, `running`, and `launch_uncertain` jobs.
Conflicting candidates remain in controller order and consume no reservation or
attempt. Jobs admitted earlier in the same fill cycle immediately hold their
claims. `concurrency_max` remains the outer lane ceiling while
`max_heavy_processes` is enforced independently through the default
`heavy_process:host` claim. See
[Durable resource admission](resource-admission.md).

An `external_blocked` result must identify Jira dependency keys already present
in the authenticated ticket relation graph. Fresh Jira synchronization wakes the
ticket only after every named dependency is complete and the prior execution is
proven absent. Other external holds remain parked. `resolve-decision` accepts
only the six decision classes in the lifecycle contract, requires a bounded
operator receipt, preserves the ticket's branch, PR, priority, dependencies,
history, and ledger bindings, and changes no other ticket. Scoping decisions
continue to use the repository-owned `sprint_decisions` registry.
If the prior worker cannot be proven absent from its authenticated tombstone,
pipe a root-issued recovery capability through `--operator-capability-stdin`;
the capability is never placed in the process argument list.

An unclean supervisor exit does not discard work. A new `start` first acquires
the unchanged lease inode, verifies the prior state-digest receipt, proves the
exact predecessor process identity absent, and requires identical config,
runtime, and lifecycle-contract digests. It then records
`takeover_requested -> predecessor_absent`, advances the lease generation, and
preserves the plan, jobs, attempts, terminal receipts, requests, timers, and
pause or drain mode. Live or ambiguous predecessor status, a replaced lease,
tampered state, or changed runtime policy is refused without modifying the prior
checkpoint. Stop cleanly before upgrading Orka or changing configuration.

See [Supervisor restart and failure injection](supervisor-restart-testing.md)
for reproducible operator checks.

## Compatibility and current limitations

Existing Orka 1.x configuration remains valid. A repository without `sprint_id`
continues in lifecycle-only mode. The optional
`supervisor_sync_interval_seconds` and `supervisor_ticket_retry_seconds` values
must each be 5 through 3600 seconds.
Initialization should gitignore `.orchestration/.supervisor/`.

Active jobs written by an earlier 1.x supervisor receive only the deterministic
automatic claim set on first 1.8.8 admission pass; their attempts and execution
identity are not changed.

Restart does not invent missing worker or provider receipts. Recovery evidence
is classified by the
[`orka.recovery-eligibility/v1`](recovery-eligibility-contract.md) contract;
the supervisor now consumes eligible stopped-attempt verdicts transactionally.
The controller repeats the observation while holding its checkpoint lock and
records evidence and preservation digests before clearing only execution-scoped
fields. A replay sees the already-pending ticket and cannot create another
attempt, worker, provider request, or reservation. Ambiguous launches remain
fenced for reconciliation. Clean, quiescent preserved PRs with exact execution,
GitHub, worktree, revision, provider-settlement, and review-history evidence are
reconciled automatically before lane allocation. The supervisor immediately
replans and routes the same PR through the bounded continuation pipeline without
creating a branch, worktree, attempt history, or review generation. Dirty,
active, moved, unknown, or financially unsettled PRs remain visible through
stable recovery reason codes. No review, security, budget, or merge gate is
weakened.
