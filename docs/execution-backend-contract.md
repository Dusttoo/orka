# Replaceable execution-backend contract

Contract: `orka.execution-backend` version 1

Machine-readable source:
[`contracts/execution-backend-v1.json`](../contracts/execution-backend-v1.json)

Reference implementation:
[`scripts/execution_backend.py`](../scripts/execution_backend.py)

Shared fixtures:
[`tests/fixtures/execution-backend-v1.json`](../tests/fixtures/execution-backend-v1.json)

This contract is the boundary between Orka's deterministic supervisor and the
mechanism that runs one already-admitted phase. It is derived from the existing
Codex desktop, Claude desktop, and direct API implementations and from the
phase-worker protocol delivered by #112–#114. It does not invent container,
microVM, remote-worker, or distributed-scheduler behavior.

## Ownership boundary

The supervisor owns:

- scheduling, admission, reservations, retries, and replacement decisions;
- repository, job, phase, attempt, worktree, dispatch, execution-unit, and
  supervisor-fence identities;
- policy evaluation, spend controls, review gates, and merge authority;
- deciding whether external evidence is sufficient to change durable state.

An execution backend may only advertise capabilities, execute a bound phase,
report observations, acknowledge cancellation, and return terminal evidence.
It may not schedule work, reserve a budget, weaken policy, admit a replacement,
or authorize a merge. A backend receipt is evidence for the supervisor, not a
state transition by itself.

This lets the scheduler depend on one lifecycle instead of branching on Codex,
Claude, API, PID, shell, container, or remote-process behavior.

## Contract lifecycle

1. **Discover.** The backend returns its protocol version, lifecycle support,
   containment claims, and separately named provider requirements.
2. **Negotiate.** The supervisor checks all mandatory capabilities before a
   reservation or provider submission. An unsupported route fails without
   consuming a worker attempt.
3. **Launch.** The supervisor supplies the complete immutable binding and a
   fresh `orka.phase-worker-protocol/v1` job envelope. Conversation or provider
   session resumption fields are forbidden. The canonical phase-worker
   validator—not a backend-specific subset—validates required identity,
   capabilities, and sanitized input before the backend runs. It recursively
   rejects exact forbidden session-state keys in nested objects and arrays;
   ordinary string values may still discuss those field names.
4. **Attach.** The backend returns a handle, backend-instance identity, process
   birth identity, and launch receipt mechanically bound to the exact
   execution and job-envelope digest. A missing or mismatched receipt is an
   uncertain launch and its execution identity cannot be reused.
5. **Heartbeat and progress.** Observations retain the full binding. They prove
   neither completion nor authority to extend budgets or bypass gates.
6. **Cancel.** Replacement is safe only after an authenticated acknowledgement
   or a supervisor-owned fence. A request, timeout, or unknown result is not an
   acknowledgement.
7. **Inspect.** The backend reports `live`, `absent`, `terminal`, or `unknown`.
   Timeout and permission failure normalize to `unknown` and remain unsafe for
   replacement.
8. **Terminal.** A terminal status is insufficient. The backend must return a
   canonical phase-worker terminal envelope with the exact immutable identity,
   and the supervisor validates it before applying any lifecycle transition.
   An inspection that claims `terminal` without valid canonical evidence
   normalizes to `unknown` and is not replacement-safe.

All operations after discovery are execution-bound. A replaced dispatch or
execution unit cannot report progress, cancellation, or terminal state for its
successor.

## Immutable identity

Every execution binds:

| Field | Meaning |
|---|---|
| `repository_id` | Canonical repository identity |
| `job_id` | Stable logical phase job |
| `phase` | One scope, implementation, review, repair, or verification phase |
| `attempt_token` | Controller-authorized attempt fence |
| `dispatch_id` | One supervisor dispatch of the logical job |
| `execution_unit_id` | One disposable worker execution |
| `worktree_id` | Exact isolated repository checkout |
| `supervisor_fence` | Active supervisor lease generation |

The logical job may survive replacement, but a replacement receives new
`dispatch_id` and `execution_unit_id` values. Backends cannot synthesize a
missing value or reuse a stale value. Host process reuse is allowed only when
the backend starts a fresh phase context and retains no model-session state.

Every full execution key is single-use. Launch consumes the key even if the
transport fails after starting work or the launch receipt is malformed. A
fence leaves a permanent tombstone; it never makes that key launchable again.
The reference coordinator keeps tombstones in memory for conformance testing.
Production integration must persist them in authoritative supervisor state so
restart cannot erase the single-use guarantee.

## Capability negotiation

Lifecycle, containment, and model-provider capabilities are intentionally
different domains.

Containment describes what the execution host actually enforces:

- process: shared host, isolated process, container, microVM, or remote;
- filesystem: none, worktree, sandbox, or isolated volume;
- network: none, restricted, unrestricted, or unknown;
- credentials: none, scoped, ambient, or unknown;
- process control: observation, cancellation, and fencing support.

A backend cannot advertise `container` merely because a provider uses remote
compute, or `scoped` credentials merely because the model API authenticates.
Provider capabilities may be required by a route, but never grant scheduling or
merge authority.

Repository policy can require stronger containment than a backend offers. That
mismatch fails during negotiation, before reservation and submission.

## Liveness and process identity

`unknown` is a durable uncertainty, not a synonym for dead. It is returned for
inspection timeout, permission denial, malformed observation, or any result
that lacks a mechanical identity receipt. The supervisor must keep the
execution fenced from replacement until it receives stronger evidence or
applies its own authoritative fence.

A PID does not identify a process across time. Proven absence binds the backend
instance, opaque backend handle, and original process birth identity. If that
handle or PID now identifies a different process, the receipt proves only that
the original execution is absent; it says nothing about the replacement
process and does not authorize the backend to launch another worker.

## Cancellation

There are two replacement-safe cancellation outcomes:

- `acknowledged`: the exact execution returns a cancellation identifier and an
  authenticated cancellation receipt. The acknowledgement must be a canonical
  phase-worker envelope bound to the exact cancellation request, launch
  receipt, backend handle, process identity, and execution identity. The
  backend adapter must mechanically verify it using its trusted host primitive
  (for example, an exact process wait receipt or a verified remote signature);
- **supervisor fence**: separately, the supervisor may record a new fence that
  makes all later evidence from the old execution stale. A backend cannot
  claim or return this outcome itself.

`requested`, `timeout`, `unknown`, and a backend-asserted `fenced` result remain
unsafe. The scheduler may wait,
inspect again, or fence according to its own policy, but the backend cannot
convert uncertainty into acknowledgement.

## Current compatibility map

| Route | Existing fresh-context behavior | Baseline host containment | Backend mapping |
|---|---|---|---|
| Codex desktop | `codex exec --ephemeral` | Shared host and managed worktree | Local-process backend plus Codex provider adapter |
| Claude desktop | One `claude --print` invocation | Shared host and managed worktree | Local-process backend plus Claude provider adapter |
| Direct API | One on-demand request with no session resume | Isolated request process and managed worktree | Local-process backend plus API provider adapter |

The execution backend does not build prompts or provider payloads. The phase
worker/provider adapter does that after backend negotiation. This preserves the
same scheduler behavior while keeping provider-specific invocation details out
of the backend contract.

## Conformance kit

`DeterministicFakeBackend` implements every operation without model credentials,
network access, sleeping, or real processes. It advertises `test_only: true`
and the coordinator rejects it unless the caller explicitly enables conformance
mode, so repository or production configuration cannot select it. The focused
suite proves:

- mandatory capability rejection happens before launch;
- each launch consumes a fresh #112 job envelope;
- malformed canonical jobs and mechanically mismatched launch receipts fail;
- stale or fenced executions cannot report progress or terminal evidence;
- fenced and uncertain launches leave non-reusable execution tombstones;
- inspection timeouts remain unknown;
- terminal inspection requires exact canonical terminal evidence;
- process identity reuse produces a bound absence receipt;
- cancellation acknowledgement is bound to its exact request and launch and
  passes a backend-specific mechanical verification hook;
- only the supervisor may assert a fence;
- containment and provider capabilities remain separate;
- the backend exposes no scheduling or merge-authority operation.

Run it with:

```bash
bash tests/execution-backend.test.sh
```

The fake backend is a conformance reference, not a production execution host,
and cannot be selected outside explicit conformance mode.
Production backends must pass the same suite with backend-specific fixtures.

## Delivery boundary

This contract and conformance kit are independently reviewable before the
runtime cutover. Integrating the local Codex, Claude, and API implementations
behind this interface is the next implementation step. Until that integration
lands, the existing 1.x launcher remains authoritative and the scheduler still
contains compatibility code for current route processes.

That production integration must persist launched-key tombstones, launch and
cancellation receipts, and backend inspection evidence in the authoritative
transactional supervisor store. The in-memory reference coordinator is not a
restart-safe production state store.

Container, microVM, Kubernetes, and remote-worker implementations remain
explicit non-goals for this slice.
