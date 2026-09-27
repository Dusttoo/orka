# Host-owned sprint supervisor lifecycle

Orka 1.8.3 introduces the first independently releasable Orka 2 runtime slice:
a detached, deterministic host process that owns one repository supervisor
lease and records the accepted supervisor lifecycle. It does not plan tickets
or dispatch workers yet; those capabilities remain in issues #84 and #85.

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
- bounded transition and operator-request history.

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

## Lifecycle behavior

- `start` records `starting -> active` only after the repository configuration,
  lifecycle contract, exact process identity, and exclusive lease validate.
- `pause` records `active -> paused` and prevents later admission work from
  treating the supervisor as active.
- `resume` records `paused -> active`.
- `drain` records `active -> draining -> paused`. This slice owns no jobs, so
  the active job count is mechanically zero; later dispatch work will delay
  `drain_completed` until its authenticated running set is empty.
- `stop` records the global `operator_stopped` transition, returns a response,
  closes the control socket, and releases the lease exactly once.
- replacing or unlinking the named lease inode records `lease_lost` and stops
  fail-closed.

An unclean process death leaves a nonterminal state. Automatic takeover is
intentionally refused until issue #77 adds heartbeat, predecessor-absence, and
split-brain proofs. This is safer than silently inventing a clean stop.

## Compatibility and current limitations

Existing Orka 1.x configuration remains unchanged. The supervisor requires the
canonical `.orchestration/config.yaml`, but adds no mandatory configuration
keys. Initialization should gitignore `.orchestration/.supervisor/`.

This slice does not synchronize Jira or GitHub, plan jobs, launch workers,
recover a crashed supervisor, or weaken existing review, security, budget, and
merge gates.
