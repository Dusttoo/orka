# Host-owned sprint supervisor

Orka 1.8.5 extends the detached Orka 2 supervisor with a deterministic outer
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
- bounded transition and operator-request history;
- the latest synchronized plan, evidence digests, hard-budget receipt, and next
  wake deadline.
- exact job-to-attempt and execution-unit bindings, accepted terminal-result
  digests, contract events, and resulting job states.

It never writes provider, Jira, GitHub, or application credentials.

## Commands

Run from a configured repository, or pass `--repo <path>`:

```bash
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" start
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" status
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" pause --request-id maintenance-1
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" resume --request-id maintenance-2
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" drain --request-id maintenance-3
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
- `drain` records `active -> draining -> paused`. This slice owns no jobs, so
  the active job count is mechanically zero; dispatch will later delay
  `drain_completed` until its authenticated running set is empty.
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

An unclean process death leaves a nonterminal state. Automatic takeover is
intentionally refused until issue #77 adds heartbeat, predecessor-absence, and
split-brain proofs. This is safer than silently inventing a clean stop.

## Compatibility and current limitations

Existing Orka 1.x configuration remains valid. A repository without `sprint_id`
continues in lifecycle-only mode. The optional
`supervisor_sync_interval_seconds` value must be 5 through 3600 seconds.
Initialization should gitignore `.orchestration/.supervisor/`.

This slice does not recover a crashed supervisor, add preserved-PR recovery or
resource exclusions, or weaken existing review, security, budget, and merge
gates.
