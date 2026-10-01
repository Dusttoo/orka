# Supervisor restart and failure injection

Orka 1.8.13 can resume the host-owned sprint supervisor after an unclean process
exit without reconstructing work from an AI transcript. Takeover is automatic
only when the repository lease is free, the exact predecessor process is
absent, the durable state receipt matches, and the config, runtime, contract,
and lease inode are unchanged.

## Safe operator procedure

Use a disposable test repository or pause a real sprint before intentional
failure injection. Record the current status:

```bash
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" status \
  --repo /absolute/path/to/repository > /tmp/orka-before.json
```

Read the supervisor PID from that status file, terminate only that exact PID,
and verify it is absent before restarting:

```bash
SUPERVISOR_PID="$(python3 -c 'import json; print(json.load(open("/tmp/orka-before.json"))["process"]["pid"])')"
kill -9 "$SUPERVISOR_PID"
kill -0 "$SUPERVISOR_PID" 2>/dev/null && exit 1 || true
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" start \
  --repo /absolute/path/to/repository
python3 "$ORKA_ROOT/scripts/sprint-supervisor.py" status \
  --repo /absolute/path/to/repository
```

The restarted status must show a higher lease generation. The durable history
must contain `takeover_requested` and `predecessor_absent`. Running, reserved,
retrying, repair, recovery, decomposition, parked, and blocked jobs retain their
exact ticket, attempt, execution-unit, branch, worktree, PR, and review-ledger
bindings. A paused supervisor remains paused; a draining supervisor remains
draining until its authenticated active set is empty.

## Expected refusal cases

Restart must fail without changing the prior checkpoint when any of these are
true:

- the predecessor PID is live or its identity cannot be verified;
- another process still owns the repository lease;
- the lease file was replaced;
- before transactional cutover, `.orchestration/.supervisor/state.json`
  differs from its digest receipt;
- after transactional cutover, the authoritative supervisor document is
  missing, corrupt, or belongs to another activation;
- Orka, the lifecycle contract, or repository orchestration config changed.

Do not edit supervisor runtime files to force recovery. Stop the supervisor
cleanly before an Orka upgrade or configuration change. If a worker launch or
provider write remains ambiguous, reconcile that exact ticket through the
existing controller recovery path; restarting never repeats an unverified
external write.

## Automated coverage

Run the integration and contract suites:

```bash
python3 tests/sprint_supervisor_test.py
python3 tests/authoritative_supervisor_state_test.py
python3 tests/supervisor_dispatch_test.py
python3 tests/sprint_controller_resilience_test.py
python3 scripts/supervisor_contract.py \
  contracts/supervisor-lifecycle-v1.json \
  --controller scripts/sprint-controller.py
```

The tests cover detached-session survival, clean and unclean restart, every
nonterminal supervisor and job state, duplicate operator and terminal delivery,
worker death, mixed outcomes, pause/drain restoration, stale evidence, state
tampering, lease loss, and exact ticket-local continuation.

## Recovery crash-boundary matrix

The machine-readable
[`orka.recovery-crash-boundaries/v1`](../contracts/recovery-crash-boundaries-v1.json)
contract declares the durable state and next action for these exact boundaries:

| Boundary | Durable recovery rule |
| --- | --- |
| eligibility observation | no mutation; re-observe |
| recovery fencing | reuse the content-derived active fence |
| controller mutation | load either the old or atomically replaced checkpoint |
| worker reservation | retain either pending or one running attempt |
| worker launch | reconcile an exact execution unit; never blind-relaunch |
| worker attach | reuse only an unconsumed capability or monitor the bound unit |
| provider acknowledgement | reconcile the exact reservation; never resubmit ambiguity |
| PR observation | re-observe the exact PR head before mutation |
| terminal application | replay the terminal receipt; duplicate delivery is a no-op |

Run `python3 tests/recovery_crash_boundary_test.py` for the consolidated local
fault matrix. It injects checkpoint failure before recovery commit, replays a
preserved-PR fence, replays worker reservation and attach, verifies monotonic
attempt/history state, and proves an ineligible recovery cannot stop an
independent queue. Existing API-agent and supervisor-dispatch suites cover
ambiguous provider acknowledgement and duplicate terminal delivery. No case
uses live Jira, GitHub, model-provider, or email credentials.
