# Legacy state migration and deterministic export

Orka 1.8.23 implements the offline migration slice tracked by issue #117. It
does not activate the SQLite store for the controller or supervisor. Existing
JSON state remains authoritative until the separately reviewed cutover work in
issue #118.

## Safety boundary

Run inventory and import only against an initialized repository identity and
canonical policy. Import acquires the repository migration lock, the supervisor
lease, and the repository initialization lock. A live supervisor, an unclean
supervisor stop, an ambiguous historical state directory, a symlinked source,
or a conflicting ownership proof fails closed.

The importer reads recognized JSON and JSONL files from registered Git
worktrees. Historical parent-level state is accepted only when its
`repository-binding.json` exactly matches the initialized repository UUID, Git
common directory, and object-directory identity. It never selects legacy state
using file age.

## Commands

From the repository to inspect or migrate:

```bash
python3 "$ORKA_ROOT/scripts/state_migration.py" inventory --repo .
python3 "$ORKA_ROOT/scripts/state_migration.py" import --repo .
python3 "$ORKA_ROOT/scripts/state_migration.py" export --repo . \
  --output /private/operator-chosen-path/orka-state.json
```

`inventory` reports the repository identity, content-addressed receipt, source
count, and source digests without writing the database. `import` builds a
temporary database, verifies SQLite integrity, compares its deterministic
shadow export with the normalized source, fsyncs it, and atomically installs
it beneath the private Git common-directory runtime. Legacy inputs are not
changed or deleted.

Repeating an exact import is an evidenced no-op. If any recognized source has
changed, the existing receipt cannot be reused or replaced. Operators must
reconcile that conflict explicitly instead of selecting a newer file.

## Preserved state and redaction

The version-2 store retains each normalized source as an immutable migration
record and keeps its original SHA-256 digest. This preserves controller and
supervisor checkpoints, attempts, costs, reservations, branches, worktrees,
PR bindings, review findings, timers, provider health, decisions, recovery
records, usage records, and external receipts for the later cutover mapper.

Prompt bodies, request and response bodies, credentials, access tokens, API
keys, passwords, and recognizable provider-secret strings are removed from
the normalized import and export. Operational attempt tokens remain because
they are execution fences, not provider credentials.

Exports use UTF-8 canonical JSON with sorted keys, stable separators, a final
newline, lexically ordered projections, durable event sequence order, and
SHA-256 digests for every section, the legacy shadow, and the export payload.

## Failure recovery

Import is replay-safe at inventory, temporary-database creation, import
transaction, integrity verification, and atomic-install boundaries. A failure
before installation leaves no live database. A failure after installation is
recovered by rerunning the same import, which verifies the receipt and shadow
digest and returns an exact replay result.
