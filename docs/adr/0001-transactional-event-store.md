# ADR 0001: Transactional repository event store

- Status: proposed for acceptance in issue #76
- Date: 2026-10-01
- Owners: Orka maintainers
- Supersedes: no prior ADR

## Context

Orka 1.x persists controller, supervisor, review, provider, budget, and recovery
state as independently locked JSON files. Atomic replacement protects one file,
but cannot atomically commit a reservation, attempt, resource claim, usage
reservation, external-operation key, timer, and event history as one change.
Independent `flock` domains also make concurrent completions and cross-platform
recovery harder to reason about.

The current repository resolver places shared state beneath the parent of the
Git common directory. That works when the common directory is `<checkout>/.git`,
but a bare repository makes its parent an unrelated run directory. Sibling bare
repositories can consequently share one `.orchestration` domain, and a linked
worktree can silently miss its declared policy and fall back to compiled
defaults. Issue #51 records a concrete reproduction.

A durable supervisor needs:

- atomic multi-entity transitions;
- append-only audit events and queryable current state;
- idempotent external operations and replay;
- durable timers and concurrent worker completion;
- explicit schema migrations;
- repository-scoped policy authority;
- deterministic, human-inspectable export;
- fail-closed migration from existing JSON state.

## Decision

Orka 2 will use one SQLite database per local Git repository as its canonical
runtime state store. The database combines an append-only event journal with
transactionally maintained current-state tables. Production mutation is owned
by one durable supervisor writer. Workers and adapters submit structured results
to that supervisor; they do not write the database directly.

SQLite is selected over extending locked JSON. It is available through Python's
standard library, supplies atomic transactions and crash recovery, and supports
concurrent readers while keeping the deployment local-first. JSON remains the
stable status, backup interchange, test-fixture, and migration format.

This ADR does not activate SQLite in the production controller. Cutover occurs
only through the separately reviewed slices listed below.

## Repository identity and state root

Git's absolute common directory is the repository boundary:

```text
normal checkout       <checkout>/.git/orka-runtime/
linked worktree       <main-checkout>/.git/orka-runtime/
bare-backed worktree  <repository>.git/orka-runtime/
```

The runtime MUST NOT use the common directory's parent.

On explicit initialization, Orka creates
`<git-common-dir>/orka-runtime/repository.json` with:

- a random repository UUID;
- the canonical absolute Git common directory;
- the Git object directory identity;
- creation time and schema version;
- the operator-selected policy ref and repository-relative policy path.

The marker is created atomically under a repository-local initialization lock,
must be a private regular file, and is never inferred from runtime state in a
worktree. A raw filesystem copy whose recorded common-directory identity no
longer matches fails closed and requires an explicit operator migration. A Git
clone receives a new UUID because Git does not clone runtime metadata.

The repository UUID is the primary key in the database and every exported
record. The canonical path is an additional anti-confusion binding, not the
identity by itself. Two sibling bare repositories therefore have distinct
state roots and UUIDs even when they share one parent directory.

## Canonical policy

Policy is read from a Git blob, never directly from the invoking worktree.
Initialization records an operator-selected trusted ref and path, for example:

```text
refs/remotes/origin/main:.orchestration/config.yaml
```

Each preflight resolves that ref to an exact commit and blob, reads the blob
through Git, and reports:

- policy ref and repository-relative path;
- resolved commit and blob object IDs;
- SHA-256 of the exact policy bytes;
- repository UUID and common directory;
- runtime state root and database path.

A missing ref, missing blob, malformed policy, identity mismatch, or ambiguous
legacy policy fails closed. Compiled defaults may validate an explicitly
present key but cannot replace a missing canonical policy. Feature worktrees
cannot activate their unmerged policy changes merely by invoking Orka from that
worktree. Local-only repositories may bind a local protected branch ref through
the same explicit initialization procedure.

This is a policy-selection boundary, not a claim that files writable by the
same operating-system identity are hostile-code-resistant. Under the portable
`cooperative-worker` profile it prevents accidental worktree policy drift.
`isolated-worker` deployments must additionally make the identity marker,
policy binding, database, and supervisor socket writable only by the distinct
supervisor owner; workers receive no direct mutation permission.

## Storage model

The initial schema contains:

- `metadata` and `schema_migrations` for format and migration identity;
- `repositories` for repository and canonical-policy bindings;
- `events` for ordered immutable facts and idempotency keys;
- `jobs` for materialized sprint/ticket lifecycle state;
- `attempts` for attempt, dispatch, execution-unit, and fence identity;
- `resource_claims` and `timers` for scheduling;
- `external_operations` for side-effect intent, ambiguity, and receipts;
- `migration_receipts` for legacy imports.

The event payload is canonical JSON. Current-state rows are projections updated
in the same transaction as their event. The event journal is not rebuilt from
materialized state. Materialized state can be independently checked by replaying
events into a temporary database and comparing canonical exports.

## Transaction boundaries

Every write uses a bounded `BEGIN IMMEDIATE` transaction and optimistic
aggregate version checks.

### Reservation

One transaction:

1. verifies queue version, budget, route, dependency, WIP, and exclusions;
2. inserts the attempt and resource claims;
3. records any local usage reservation;
4. changes the job from queued/repair/recovery ready to reserved;
5. appends the reservation event.

Nothing is launched if that transaction does not commit.

### Launch

One transaction verifies the exact reservation, attempt token, dispatch ID,
supervisor fence, and execution-unit identity; records the launch receipt;
changes the attempt and job to running; and appends the launch event. Provider
submission occurs only after a durable external-operation intent exists.

### External side effects

Network calls never occur inside a database transaction:

1. commit a unique operation key and intended request digest;
2. perform the external operation;
3. commit the provider/GitHub/Jira receipt and resulting state transition.

An ambiguous call remains `needs_reconcile`. Replaying the same operation key
returns or reconciles the existing record; it never submits a second call
blindly.

### Completion

One transaction verifies the attempt, dispatch, execution unit, supervisor
fence, terminal envelope, and external receipts; appends the terminal event;
updates the job/attempt projections; settles or preserves usage; releases
claims; and creates any retry timer or decision record. Concurrent completions
for different jobs serialize safely. A stale completion fails its version and
identity predicates without modifying state.

## Journal and durability mode

The preferred local mode is WAL with `synchronous=FULL`, foreign keys enabled,
a bounded busy timeout, and supervisor-owned checkpointing. WAL is supported
only on a local filesystem where SQLite's shared-memory locking works. Network
filesystems are unsupported and fail preflight.

SQLite documents a WAL-reset race in versions through 3.51.2, with fixed
releases 3.51.3, 3.50.7, and 3.44.6. Orka therefore enables WAL only when the
loaded Python SQLite library identifies a fixed release. Older supported
libraries use the rollback journal with `synchronous=FULL`; status reports the
active journal mode and reason. The implementation must verify the requested
mode rather than assuming the pragma succeeded.

Only the supervisor owns a production write connection. CLI mutations are sent
through the supervisor socket. Offline migration or repair requires a stopped
supervisor and exclusive repository lease. Read transactions are short-lived so
they cannot starve checkpoints.

References:

- <https://www.sqlite.org/wal.html>
- <https://www.sqlite.org/transactional.html>
- <https://www.sqlite.org/howtocorrupt.html>
- <https://www.sqlite.org/pragma.html#pragma_integrity_check>

## Deterministic export

`orka state export` emits canonical JSON with:

- explicit export and schema versions;
- repository and policy identity;
- ordered events by durable sequence;
- lexically ordered materialized entities;
- original timestamps and exact numeric/string values;
- SHA-256 digests for each section and the whole export.

Formatting uses UTF-8, sorted object keys, stable separators, and a terminal
newline. Export never contains credentials, API keys, prompt bodies, or raw
provider secrets. Human status is derived from the same read transaction so it
cannot mix generations.

## Legacy import and cutover

Import is explicit, offline, and idempotent:

1. stop the supervisor and prove the prior execution unit absent;
2. acquire the repository lease and initialization lock;
3. locate only known repository-local and historical state locations;
4. hash every source before mutation;
5. prove ownership from repository, sprint, attempt, branch/worktree, PR, and
   existing receipt bindings;
6. reject conflicting, parent-level, or unattributable state for operator
   reconciliation;
7. import into a temporary database in one transaction;
8. replay and compare deterministic export with the normalized legacy source;
9. run integrity and foreign-key checks;
10. atomically install the database and a signed migration receipt;
11. retain legacy inputs read-only until an explicit later cleanup.

Attempts, spend, reservations, branches, worktrees, PRs, review findings,
provider health, decisions, timers, and external receipts are preserved. Legacy
parent-level state is imported only when every artifact proves the same unique
repository. Identical files can be deduplicated by digest; ambiguity never uses
"newest wins."

The cutover marker records a minimum Orka version. A 1.x runtime that observes
the marker must refuse to write. The new runtime can perform a shadow export and
comparison before activation, but there is no dual-writer mode. A running 1.x
controller is never upgraded in place.

## Backup, corruption, and repair

- Online backups use SQLite's backup API from a bounded read connection.
- Raw file copies are allowed only while stopped and must keep database, WAL,
  and shared-memory state together as SQLite requires.
- Startup runs `quick_check`, foreign-key validation, schema/version checks, and
  repository/policy binding verification.
- Scheduled or operator diagnostics may run full `integrity_check` and event
  replay comparison.
- Corruption stops admission globally but never deletes the source. Recovery
  restores a verified backup or rebuilds projections from an authenticated
  export/event stream into a new file.
- Repair always produces an audit receipt; in-place hand editing is unsupported.

## Cross-platform behavior

SQLite owns database concurrency rather than Unix-only `flock`. Private-file,
atomic-create, path, ownership, and process-liveness helpers must have explicit
macOS, Linux, and Windows implementations or fail preflight as unsupported.
Repository state remains on a local filesystem. Case normalization and symlink
resolution are recorded during initialization and rechecked at startup.

## Alternatives considered

### Extend locked JSON

Rejected as the canonical Orka 2 store. JSON is inspectable and dependency-free,
but atomicity ends at one file, every mutation rewrites broad state, secondary
indexes and timers are ad hoc, independent lock ordering can deadlock, and
multi-process crash recovery requires recreating database semantics. JSON is
retained as the interchange and status format.

### Append-only JSONL plus snapshots

Rejected for the initial implementation. It improves auditability but still
requires custom locking, checksums, truncation recovery, compaction, indexes,
transactions spanning projections, and portable concurrent access.

### External database or queue service

Rejected as a requirement. It would weaken local-first installation, complicate
credentialing and backup, and make a single-repository developer workflow depend
on network availability. A future adapter may mirror events externally without
changing the local authority.

## Consequences

Positive:

- one transaction can preserve every state-affecting invariant;
- worker completions become durable and idempotent without global JSON locks;
- timers, decisions, status, and replay are queryable;
- repository identity and policy authority work with bare repositories;
- deterministic export keeps state inspectable and portable.

Costs and constraints:

- schema migrations and database operations require dedicated tests;
- WAL mode is local-filesystem-only and version-gated;
- operators must initialize a canonical policy binding;
- old runtimes cannot write after cutover;
- human inspection uses exported JSON rather than editing live state.

## Delivery slices

1. Issue #51: repository identity, common-directory state root, canonical Git
   policy binding, and fail-closed legacy-root discovery.
2. Issue #116: transactional store core, schema, one-writer transaction API,
   event and projection invariants, idempotency, timers, and concurrent
   completion tests.
3. Issue #117: deterministic export, legacy JSON importer, migration receipts,
   ambiguity handling, and shadow comparison.
4. Issue #118: controller/supervisor integration, socket-owned mutations,
   minimum-version marker, and no-dual-writer activation.
5. Issue #119: backup, corruption detection, replay validation,
   cross-platform locking/path behavior, and crash injection at every boundary.

Each slice is independently reviewable. The current JSON runtime remains the
production authority until the cutover slice passes its migration and fault
matrix.
