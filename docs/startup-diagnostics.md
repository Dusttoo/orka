# Supervisor startup diagnostics

Contract: `orka.startup-diagnostic`, schema version 1
Machine-readable source: [`contracts/startup-diagnostic-v1.json`](../contracts/startup-diagnostic-v1.json)

The elected repository supervisor must produce a healthy startup-diagnostic
receipt before it synchronizes inventory, plans work, reserves capacity, or
launches a worker. The receipt has a fixed check order, stable reason codes, and
safe operator actions. It contains no absolute paths, credentials, repository
payloads, prompts, or ticket data.

For a transactional repository, startup checks:

- owner-only access to the runtime directory, database, identity marker,
  cutover marker, and any SQLite sidecars;
- bounded SQLite `quick_check(1)` and complete foreign-key validation;
- the exact reviewed schema and migration-ledger digests;
- repository identity and canonical-policy bindings;
- the authenticated cutover marker, database cutover record, and minimum Orka
  version.

The diagnostic opens SQLite with `mode=ro` and `query_only`, starts a read
transaction, and rolls it back. It never repairs schema, rewrites a marker,
changes permissions, updates policy, or creates a database sidecar. A failed
check stops admission and preserves the source store for an explicit recovery
procedure. `PRAGMA integrity_check` remains the deeper operator-requested
diagnostic; it is intentionally not part of bounded startup.

The supervisor retains the checked database device/inode identity and immutable
policy commit, blob, digest, and content outside the sanitized receipt. Under
its exclusive lease and event-store writer lock, it opens the writer only when
the database identity still matches and revalidates the complete startup
authority on that same SQLite connection before permissions, journal mode,
migrations, planning, or admission can change state. It then materializes the
retained policy blob without resolving the moving policy ref again. Deleting or
corrupting `cutover.json` cannot downgrade an event store whose
`runtime_cutovers` ledger already records active cutover.

A pre-existing writer-lock sidecar must already be a private regular file; the
constructor never repairs an unsafe mode. If admission needs to create the lock
and later validation fails, it removes only the exact inode it created. A
symlink or another process's replacement is never followed, changed, or
removed.

Legacy repositories retain their 1.x behavior. Their event-store-only checks
are reported as `legacy_not_applicable`, while the compatible repository policy
is still verified. An initialized pre-cutover repository also validates its
repository identity and canonical Git policy without activating cutover.

Run the same read-only check independently:

```bash
python3 scripts/sprint-supervisor.py diagnose --repo /path/to/repository
```

The command exits nonzero when `healthy` is false. Successful supervisor start
and status responses retain the exact receipt that admitted that supervisor
generation.
