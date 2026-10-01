# Disposable phase-worker protocol

Contract: `orka.phase-worker-protocol` version 1

Machine-readable source:
[`contracts/phase-worker-protocol-v1.json`](../contracts/phase-worker-protocol-v1.json)

Shared conformance fixtures:
[`tests/fixtures/phase-worker-protocol-v1.json`](../tests/fixtures/phase-worker-protocol-v1.json)

This contract defines the provider-neutral boundary between the durable Orka
supervisor and one disposable AI phase execution. It deliberately separates a
long-lived host process from model context: a Codex, Claude, or API adapter may
keep its process available, but every dispatch must start a fresh model context.
Conversation or provider-session identifiers never cross the boundary.

## Lifecycle

1. An adapter sends a `capability_offer` before a job is reserved or submitted
   to a provider.
2. The supervisor rejects an unsupported protocol version or a missing
   mandatory capability without consuming a provider request.
3. The supervisor sends one sanitized `job` envelope bound to immutable job,
   ticket, phase, attempt, repository, worktree, dispatch, execution-unit, and
   supervisor-fence identities.
4. The worker may send bound `progress` and `heartbeat` envelopes. These are
   evidence, not authority to weaken gates or infer completion.
5. Cancellation completes only after a bound `cancellation_ack` supplies a
   terminal receipt. Without that acknowledgement, the supervisor fences the
   execution unit before admitting a replacement.
6. Exactly one validated `terminal` envelope enters the supervisor lifecycle.
   Stale, malformed, or identity-mismatched results are bounded worker failures
   and cannot mutate another attempt.

## Mandatory capabilities

Every adapter must advertise:

- `attempt-fencing`;
- `cancellation-ack-or-fence`;
- `fresh-context-per-dispatch`;
- `immutable-job-identity`;
- `structured-progress`;
- `structured-terminal-result`.

Desktop adapters also advertise `desktop-subscription`; API adapters advertise
`provider-receipts`. Optional capabilities may be added compatibly. Removing a
mandatory capability, changing its meaning, or accepting a dispatch without it
requires a new protocol version.

## Identity and validation

The `job` and `terminal` envelopes carry the complete immutable identity. Live
progress, heartbeat, cancellation, and acknowledgement envelopes carry the
attempt, dispatch, execution-unit, and supervisor-fence subset needed to reject
stale activity. The validator fixes those bindings per envelope so a contract
edit cannot silently drop a fence while leaving a field superficially present.

Envelope validation occurs before scheduler state changes. Unknown additive
fields are tolerated for forward-compatible metadata, but conversation/session
resumption fields are explicitly forbidden. Implementations must not interpret
unknown fields as authority.

Terminal artifact references are limited to `artifact_id`, `branch`, `head`,
`pr`, and `tree`, and every supplied value must be a nonempty string. The
protocol transports those references but does not authenticate them; the
supervisor must verify them against the repository and external provider before
applying an outcome-specific transition.

## Failure behavior

| Boundary failure | Classification |
|---|---|
| Unsupported protocol version | permanent launch rejection |
| Missing mandatory capability | permanent launch rejection |
| Identity or fence mismatch | invalid worker result |
| Malformed envelope | invalid worker result |
| Cancellation without acknowledgement | fenced/cancelled worker |

These classifications are ticket-local. They do not stop unrelated work or
weaken review, security, budget, merge, or destructive-action controls.

## Current-controller compatibility

Protocol v1 maps existing Orka evidence without changing the checkpoint store:

| Current evidence | Protocol identity |
|---|---|
| `attempt_token` | `attempt_token` |
| `controller_invocation_id` | `dispatch_id` |
| `execution_unit_identity` | `execution_unit_id` |
| `repository_identity` | `repository_id` |
| `worker_cwd` | `worktree_id` |

This is a compatibility interpretation, not permission to reconstruct missing
identity. Adapters in later delivery slices must negotiate the contract before
reservation and preserve these exact bindings through terminal processing.

## Conformance

Run the focused suite with:

```bash
bash tests/phase-worker-protocol.test.sh
```

The fixture set covers Codex desktop, Claude desktop, and API capability offers;
valid job, progress, cancellation, and terminal messages; stale fences;
conversation reuse; missing capabilities; unknown outcomes; and unsupported
protocol versions. It contains no repository-specific credentials or data.
