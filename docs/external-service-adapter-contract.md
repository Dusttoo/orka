# External-service adapter contract

Contract: `orka.external-service-adapter` version 1

Machine-readable source:
[`contracts/external-service-adapter-v1.json`](../contracts/external-service-adapter-v1.json)

Reference conformance boundary:
[`scripts/external_service_adapter.py`](../scripts/external_service_adapter.py)

Sanitized fixtures:
[`tests/fixtures/external-service-adapter-v1.json`](../tests/fixtures/external-service-adapter-v1.json)

This contract defines how Orka's deterministic supervisor can obtain evidence
from an issue tracker or source-control service and submit a previously
authorized mutation without depending on a vendor response shape. It maps the
current Jira and GitHub behavior; it does not route production traffic through
the new boundary yet and does not add a new provider.

## Authority boundary

The supervisor remains the only authority that may:

- schedule or prioritize work;
- reserve budget or consume a worker attempt;
- interpret dependencies and choose a state transition;
- decide whether external evidence is sufficient to change durable state;
- authorize, gate, or perform a merge under repository policy; or
- retry, park, or reconcile an ambiguous operation.

An adapter may advertise capabilities, return normalized evidence, execute one
exact bound mutation intent, and reconcile that intent. Adapter output is
evidence, not a controller transition. Even the `scm.merge.request` capability
only executes a merge intent that has already passed the independent merge
guard; advertising it never grants merge authority.

Credentials stay inside the adapter process. Authorization headers, cookies,
tokens, secrets, raw vendor responses, and arbitrary provider fields cannot
cross the boundary into scheduler state.

## Capability negotiation

An adapter advertises its stable identity, implementation version, compatible
protocol versions, and independently versioned capabilities. The supervisor
declares the exact capabilities required by a route before reservation or
external mutation.

Protocol v1 uses exact major compatibility and additive capability evolution:

- a missing required capability or older capability version is a permanent
  pre-admission rejection;
- unknown optional capabilities may be ignored;
- an incompatible protocol version fails before work or budget is reserved;
- unsupported behavior is reported structurally, never inferred from a 404 or
  an unrecognized vendor message.

The v1 surface covers issue inventory, dependency edges, issue state, issue
comments, pull requests, CI, approvals, PR comments, merge requests, and merge
receipts. A repository only negotiates the subset its workflow actually needs.

## Exact operation binding

Every request and response carries the same immutable binding:

| Field | Purpose |
|---|---|
| `repository_id` | Immutable canonical repository identity; a rename does not change it |
| `adapter_id` / `adapter_version` | Exact implementation that handled the request |
| `protocol_version` | Negotiated protocol major |
| `operation` | Versioned capability name |
| `operation_key` | Stable logical operation chosen by the supervisor |
| `request_id` | One submission or read attempt |
| `request_digest` | Canonical digest of the binding and sanitized request material |
| `external_object` | Exact normalized object type and provider-stable identifier |

An adapter cannot answer a request for another repository, PR, issue, head, or
operation. The supervisor validates the complete echoed binding and the
canonical evidence or acknowledgement digest before accepting a response.

## Read evidence and pagination

Single-object reads return only `binding`, `observed_at`, `data`, and an
`evidence_digest`. `data` is a normalized object defined by the capability;
unknown envelope fields and vendor payloads are rejected.

Collection reads additionally prove completeness. Every page binds the request
digest, page index, and input cursor. Every item has an exact `{type, id}`
identity. Cursors cannot repeat, item identities cannot repeat, and only the
terminal page may carry a final proof. That proof binds:

- the number of pages;
- the exact item count; and
- a digest of the complete sorted identity set;
- the provider's completion basis (an exact total or terminal cursor); and
- the provider total when that completion mechanism supplies one.

This preserves the safety property already implemented by the Jira inventory
adapter and the GitHub CI reader: missing pages cannot silently release work or
turn partial CI into green CI.

## Mutation lifecycle

The supervisor creates an immutable intent containing the request binding,
sanitized input and its digest, and an idempotency key. An adapter may return:

- `completed`, with a normalized provider receipt and exact external object;
- `rejected`, with a permanent normalized reason;
- `rate_limited`, with scope, remaining capacity, reset time, and retry delay;
- `ambiguous`, with a reconciliation key and digest of the original intent.

An ambiguous acknowledgement is not a failure and is not retryable. The
original intent remains durable and the only supported next operation is
`reconcile` against that exact intent. This prevents duplicate Jira comments,
status transitions, PRs, and merges after a transport timeout.

The host-owned intent store must atomically claim an intent before the adapter
is called; a read-then-write store is not conformant because two coordinators
could submit the same operation. That claim also creates a durable host
reconciliation key before provider I/O starts. If the provider call, response
validation, or acknowledgement persistence fails, the coordinator returns that
key and the exact intent remains reconcile-only across restart. A provider
reconciliation key may supplement the host key, but a collision with another
intent is rejected rather than overwriting either recovery path.

A rate-limited acknowledgement preserves the exact intent and may be retried
only with that same idempotency key and binding after its declared retry delay.
A crash or transport loss while submission is in flight is reconcile-only,
never a blind retry. The bundled file store is a POSIX `flock` reference
implementation for cooperating local processes. It fsyncs both the replacement
file and its parent directory before reporting a durable transition. A
distributed host must provide equivalent atomic-claim and durable-commit
semantics.

Unknown errors before an intent is claimed fail closed as permanent. Unknown
errors after a durable claim are uncertain and reconcile-only because the
external side effect may have completed. A new adapter cannot broaden retry
behavior by returning prose.

## Current compatibility map

The map is descriptive until production integration lands.

| Existing behavior | v1 capability | Current implementation and retained proof |
|---|---|---|
| Sprint issue inventory | `issues.inventory.read` | `jira_inventory_fetch.py`; exhaustive cursor/page proof, field allowlist, immutable issue keys |
| Dependency edges/status | `issues.dependencies.read`, `issues.status.read` | Jira inventory normalization; configured link direction and external dependency status |
| Jira workflow transitions | `issues.status.transition` | `jira_decomposition.py`; approved multi-step path, re-read after mutation |
| Jira comments | `issues.comment.create` | Protocol surface is defined; current agent-mediated comments are not yet routed through a shared client |
| Pull-request identity | `scm.pull-request.read` | `github_progress.py`; immutable repository id, PR number, branch, head, and tree |
| Pull-request creation/update | `scm.pull-request.create`, `scm.pull-request.update` | Current `gh` workflow; v1 adds uniform intent/acknowledgement/reconciliation |
| Check runs and statuses | `scm.ci.read` | `github_progress.py`; paginated checks/statuses and exact head re-read |
| Reviews and approvals | `scm.approval.read` | Current PR gate evidence; exact PR/head identity remains mandatory |
| PR comments | `scm.comment.create` | Current `gh` workflow; v1 requires idempotency and ambiguity handling |
| Merge and receipt | `scm.merge.request`, `scm.merge-receipt.read` | `merge-on-green.sh`, `merge-guard.sh`, controller merge verification; v1 does not replace those authorities |

Production integration must preserve each current proof rather than merely
wrapping an HTTP call. The existing Jira/GitHub clients remain authoritative
until a separate implementation slice routes them behind this contract.

## Adding an adapter

Contributors proposing another service should:

1. Open an architecture issue describing the capability subset, immutable
   repository/object identities, pagination model, idempotency mechanism, rate
   limits, and ambiguous-side-effect reconciliation API.
2. Map vendor data into existing normalized capabilities. Do not add vendor
   fields to scheduler state or make the scheduler branch on provider names.
3. Add sanitized positive and adversarial fixtures. Never capture credentials,
   private repository names, customer data, or raw production responses.
4. Run the credential-free conformance suite and add provider-specific tests
   for pagination gaps, object/head movement, rate limiting, timeout after
   submission, and reconciliation.
5. Keep scheduling, dependency policy, budget, review, and merge decisions out
   of the adapter.

New capabilities are additive and independently versioned. Changing the
meaning of an existing capability requires a new protocol major and migration
plan; silently weakening v1 is not compatible.

## Conformance suite and delivery boundary

The deterministic fixture adapter uses no credentials, network, clock sleeps,
or private project data. It cannot be selected outside explicit conformance
mode. The suite proves negotiation before mutation, exact response binding,
redaction, Jira/GitHub capability coverage, complete pagination, idempotent
mutation submission, ambiguity reconciliation, normalized rate limits, and the
adapter authority boundary.

Run it with:

```bash
bash tests/external-service-adapter.test.sh
```

This slice intentionally stops at the accepted protocol, compatibility map,
fixtures, and conformance boundary. Routing production Jira/GitHub clients
through it, adding an adapter SDK, and implementing another vendor remain
separate work.
