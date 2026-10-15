# Runtime observability contract

Machine-readable contract:
[`orka.runtime-observability/v1`](../contracts/runtime-observability-v1.json)

Orka exposes a local, read-only observability boundary for dashboards, log
drains, and future OpenTelemetry adapters. Consumers do not read checkpoint
files or SQLite tables. They call the supervisor CLI and receive versioned,
redacted JSON.

## Event stream

```bash
python3 scripts/sprint-supervisor.py events --repo /path/to/repository
python3 scripts/sprint-supervisor.py events --repo /path/to/repository \
  --cursor '<cursor>' --limit 100 --format jsonl
```

Events are ordered by the immutable transactional sequence. A page includes a
repository-bound cursor for the next sequence, the snapshot high-water mark,
and the earliest retained sequence. Reusing a cursor after a supervisor restart
returns the same continuation because cursors contain no process-local state.
Version 1 retains events for the event store's lifetime. A future compactor may
advance the earliest retained sequence; a cursor behind that boundary then
fails explicitly instead of silently skipping history.

The public envelope includes only event identity, sequence, time, transition
class/name/version, and a one-way subject digest. It never exposes event
payloads, raw aggregate IDs, tickets, paths, idempotency keys, prompts,
credentials, source text, or external/provider bodies. Unknown fields remain
redacted by default.

JSONL writes one metadata record, zero or more event records, and one cursor
record. The complete page is copied under one short read transaction; the
database is closed before stdout is written, so a slow or disconnected observer
cannot hold the supervisor writer.

## Metrics snapshot

```bash
python3 scripts/sprint-supervisor.py metrics --repo /path/to/repository
```

The metrics schema reports stable groups for work categories, lane capacity,
transition totals, retry/recovery activity, provider holds, cost, and
completion. It uses numeric values only where authoritative state measures
them. Desktop subscription use is always reported as `unmetered`, with a null
USD value; it is never represented as zero spend.

Metrics and events are strictly local. This contract does not select a
telemetry vendor, start a network listener, or grant mutation authority.

## Adapter guidance

An adapter should persist the returned cursor only after accepting the complete
page. It should tolerate additive fields, reject an unknown schema major
version, and treat cursor expiry as a request for a new snapshot rather than
guessing a sequence. Adapters must not infer identity from subject digests or
combine them across repositories.
