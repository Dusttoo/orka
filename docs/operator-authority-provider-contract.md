# Replaceable operator-authority provider contract

Contract: `orka.operator-authority-provider` version 1

Machine-readable source: [`contracts/operator-authority-provider-v1.json`](../contracts/operator-authority-provider-v1.json)

Reference conformance suite: [`tests/operator_authority_provider_test.py`](../tests/operator_authority_provider_test.py)

## Purpose

This contract separates verification and consumption of an exceptional operator
capability from the backend that issues and durably stores it. It covers only
bounded exceptions: a ticket budget ceiling, relaunch ceiling, exact terminal
recovery, PR repair-cycle ceiling, or a restart allowance with absolute
ceilings. It does not schedule a ticket, approve a review, merge a PR, or
perform a destructive action.

The scheduler asks an authority coordinator to verify a provider receipt for a
specific action. It never needs a provider-specific state-machine branch. A
future human approval service therefore implements this protocol and its
durable receipt semantics; it does not modify the scheduler. The coordinator
integration itself is deliberately deferred until the transactional authority
state delivered by #127.

## Trust boundary

Repository configuration and worker input are untrusted for authority. They
cannot name a provider, set an issuer, issue a capability, extend an expiry,
or change a scope. Provider discovery is host/operator-owned and pins both a
provider identity and compatible protocol version before an authority request
is attempted. The reference `LocalRootAuthorityProvider` is constructed only
from the installed helper configuration; repository and worker input cannot
select an executable, provider, or issuer. It verifies, consumes, and revokes
through helper commands rather than accepting a caller-supplied receipt.

An issuer creates an opaque, one-use capability outside the repository and
worker security boundary. Every capability includes the provider identity,
issuer identity, repository identity, narrow ticket/PR/attempt scope, authority
class, exact allowed action, issuance and expiry times, and a non-secret audit
receipt identifier. Its scope is closed by authority class: terminal recovery
binds exactly one ticket and attempt; budget/relaunch/restart bind their exact
absolute ceilings; review repair binds exactly one PR and repair-cycle ceiling.
A provider must durably record consumption before returning success. Replays,
revocations, expiry, future-issued data, a scope/provider mismatch, malformed
receipts, and unavailable providers deny or remain `unknown`; they never imply
permission. An outage preserves prior durable state and fails closed.

An authority class is not a general override. In particular, no class permits
skipping code review, security review, merge authority, or destructive actions.
The table in the machine-readable contract is closed:

| Class | Sole action |
| --- | --- |
| `budget-extension` | Raise one ticket’s absolute budget ceiling |
| `relaunch-extension` | Raise one ticket’s absolute attempt limit |
| `terminal-recovery` | Recover one exact terminal attempt |
| `review-repair` | Extend one PR’s repair-cycle ceiling |
| `restart` | Restart one ticket under exact absolute ceilings |

## Lifecycle and compatibility

1. A host-owned coordinator discovers a provider and negotiates version 1 and
   the needed class before asking it for authority.
2. A separately trusted issuer creates a scoped, expiring, opaque capability.
3. The coordinator verifies its provider, repository, class, and exact scope.
4. The provider atomically consumes it and returns an audit receipt, or denies
   it without changing state.
5. Root/operator revocation durably prevents future verification; consuming or
   revoking a grant never recreates it.

The current root-owned helper is the reference profile
`orka.local-root-helper`. Its issuer is `host-root`; it keeps pending, active,
and consumed records in root-owned private files and exposes only narrow sudo
runtime commands. Revocation removes both pending and active records with the
same exact scope, so an issued-but-not-yet-activated token cannot be used after
revocation. The compatibility adapter maps its existing commands as
follows:

| Contract class | Issue | Runtime verification/consumption | Revocation |
| --- | --- | --- | --- |
| budget extension | `issue-budget` | `activate-budget`, `budget-ceiling` | `revoke-budget` |
| relaunch extension | `issue-relaunch` | `activate-relaunch`, `relaunch-ceiling` | `revoke-relaunch` |
| terminal recovery | `issue-recovery` | `consume-recovery` | `revoke-recovery` |
| review repair | `issue-review-repair` | `activate-review-repair`, `review-repair-grant` | `revoke-review-repair` |
| restart | `issue-restart` | `activate-restart`, `restart-grant` | `revoke-restart` |

The adapter creates a protocol audit receipt from the helper’s durable
pending/active/consumed record and its command receipt. It must never expose a
capability token in logs or receipts. The helper’s root ownership and
non-writable installation remain the enforcement point; this contract does not
weaken that separation or authorize a repository-selected executable.

The file-backed helper writes each replacement file, fsyncs it, atomically
renames it, and fsyncs the affected parent directories after replacements and
unlinks. It consequently claims durable file-system transitions only on local
storage that supports directory fsync. It does not claim transactional coupling
to a scheduler state update; that coupling remains the transactional event-store
boundary introduced by #127.

## Conformance fixtures

The shared fixtures cover issuance shape, provider discovery, verification,
one-use consumption, revocation, replay, and outage behavior without a
credential, a real repository decision, cloud IAM, OAuth, or network access.
The reference test runs the host-pinned local-helper adapter in isolated test
mode and proves the same one-use, exact-attempt, repository-bound, pending
revocation, replay, and unavailable-provider behavior expected from a future
provider.

Run:

```bash
bash tests/operator-authority-provider.test.sh
```
