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
