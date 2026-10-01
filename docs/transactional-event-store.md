# Transactional event-store core

Orka 1.8.21 introduced the storage core selected by
[ADR 0001](adr/0001-transactional-event-store.md). Orka 1.8.27 connects the
controller to that core after explicit cutover, and Orka 1.8.29 makes the
transactional supervisor snapshot authoritative. Before cutover, the JSON
controller and supervisor checkpoints remain compatible and authoritative.
After cutover, direct controller commands route through the elected repository
supervisor, controller JSON is a private ephemeral materialization, and the
supervisor no longer writes its legacy JSON checkpoint.

## Ownership boundary

`scripts/event_store.py` opens one SQLite write connection and holds a
non-blocking repository-local writer lock for its lifetime. Every mutation must
also present the opaque identity selected by the owning supervisor. Worker
processes receive neither the connection nor that identity; a second writer
fails closed.

Host callbacks may arrive on multiple threads. The store serializes them
through the one connection and bounded `BEGIN IMMEDIATE` transactions. This is
concurrent completion at the supervisor boundary, not permission for workers
to write SQLite directly.

## Atomic operations

The initial API supports:

- repository binding to the UUID, Git common directory, object identity, and
  canonical policy provenance established by Orka 1.8.20;
- queued job creation;
- reservation with an attempt, dispatch, execution unit, supervisor fence,
  and typed resource claims;
- launch only when every reserved identity still matches;
- terminal completion, claim release, and optional retry-timer creation in one
  transaction;
- external-operation intent followed by an independently committed receipt or
  reconciliation state;
- generation-fenced timer firing;
- deterministic diagnostic reads and SQLite integrity checks.

Every state transition appends an immutable canonical-JSON event and updates
its projection in the same transaction. Aggregate versions reject stale
writers. Replaying an exact idempotency key returns the original sequence
without another mutation; changing its operation material fails closed.

## Schema and durability

The version-1 schema is `contracts/event-store-v1.sql`; the additive legacy
import schema is `contracts/event-store-v2-legacy-import.sql`; and the cutover
authority/runtime-document schema is
`contracts/event-store-v3-runtime-cutover.sql`. Initialization records each
migration identifier and SHA-256 source digest. Reopening is idempotent;
unknown, missing, or changed migration history is rejected rather than
inferred.

Connections enable foreign keys, `synchronous=FULL`, and a bounded busy
timeout. WAL is requested only when the caller explicitly proves a local
filesystem and Python's loaded SQLite is one of the fixed releases documented
in ADR 0001. Every other case uses the rollback journal and reports why.

The database and writer lock are private files. Event rows reject update and
delete operations through schema triggers. Startup validates the requested
journal and durability settings; diagnostics expose integrity and foreign-key
checks.

## Verification boundary

The production API suite proves migration idempotency, repository-binding
conflicts, exclusive writer ownership, direct-worker rejection, exact replay,
changed-payload rejection, stale versions and fences, immutable events,
exclusive-claim rollback, timer generations, external-operation receipts, and
12 concurrent terminal completions without losing jobs, events, claims,
timers, or receipts.

Orka 1.8.23 adds the explicit offline importer and deterministic export. See
[Legacy state migration](legacy-state-migration.md). Orka 1.8.27 completes the
controller-command routing slice (#125). Orka 1.8.29 makes the supervisor's
whole state generation authoritative in the store (#126). The final no-dual-
writer proof remains in #127; backup, replay validation, cross-platform
behavior, and broader injected crash recovery remain in #119.

## Authoritative supervisor generations

Once cutover is active, the elected supervisor opens the event store once and
holds its writer lock until lease release. Controller commands share that
connection instead of opening a second production writer. Each supervisor
mutation commits the complete `supervisor:primary` document as one generation,
so status never combines planning, dispatch, attempt, claim, timer, decision,
or external-operation ambiguity from different points in time.

Worker and adapter processes receive neither the connection nor its writer
identity. Their results remain untrusted envelopes delivered to the supervisor;
only the supervisor may turn them into another generation. Concurrent host
callbacks serialize on the connection lock. A restart reads the last committed
generation, advances the supervisor lease fence, and restores exact attempts,
claims, timers, decisions, and ambiguous external operations without consulting
or replacing `.orchestration/.supervisor/state.json`.

The legacy JSON path is deliberately unchanged before cutover. This permits an
operator to qualify the imported shadow state before activation without
silently changing the established 1.x runtime.

## Controller command boundary after cutover

The public `sprint-controller.py` interface remains unchanged. When an active
cutover marker is present, it reads the current controller generation set and
sends the original argument vector to the repository-derived Unix socket. The
request binds:

- repository UUID and cutover activation;
- supervisor lease UUID and generation;
- a bounded command identity; and
- every expected controller-document generation.

The supervisor rejects stale or mismatched bindings. It materializes controller
documents beneath its private `0700` runtime directory, supplies an unforgeable
per-execution capability to the legacy controller process, and forbids state or
policy path overrides. A successful invocation may change at most one sprint
document. The supervisor commits that document through
`write_runtime_document`; the legacy checkpoint is never replaced.

Every successful command advances the selected controller generation, even
when the command is logically read-only. That generation is also the durable
command receipt: exact delivery of the same command identity and material is a
no-op, while changed arguments or bindings fail closed.

| Failure or adversarial case | Required behavior |
| --- | --- |
| Missing or stopped supervisor | Direct command fails without touching legacy JSON. |
| Stale repository, activation, lease, or controller generation | Supervisor rejects before materialization or execution. |
| Duplicate command identity and exact material | Return the recorded generation without re-executing the controller. |
| Duplicate command identity with changed material | Reject as an idempotency conflict. |
| `--state-dir` or `--config` override | Reject before controller execution. |
| Controller failure or malformed checkpoint | Discard the private materialization; commit nothing. |
| More than one changed sprint document | Reject the whole command; commit nothing. |
| Pre-cutover repository | Preserve the existing file-backed controller behavior. |
