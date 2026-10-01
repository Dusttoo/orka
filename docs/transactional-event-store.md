# Transactional event-store core

Orka 1.8.21 implements the storage core selected by
[ADR 0001](adr/0001-transactional-event-store.md). It is an integration API for
the later importer and controller-cutover slices. The existing JSON controller
remains production authority in this release.

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
import schema is `contracts/event-store-v2-legacy-import.sql`. Initialization
records each migration identifier and SHA-256 source digest. Reopening is
idempotent; unknown, missing, or changed migration history is rejected rather
than inferred.

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
[Legacy state migration](legacy-state-migration.md). The later tracked slices
still own controller/supervisor cutover (#118), and backup, replay validation,
cross-platform behavior, and broader injected crash recovery (#119).
