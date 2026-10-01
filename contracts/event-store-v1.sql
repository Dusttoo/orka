PRAGMA foreign_keys = ON;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    migration_id TEXT NOT NULL UNIQUE,
    source_digest TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE repositories (
    repository_id TEXT PRIMARY KEY,
    common_directory TEXT NOT NULL,
    object_directory_id TEXT NOT NULL,
    policy_ref TEXT NOT NULL,
    policy_path TEXT NOT NULL,
    policy_commit TEXT NOT NULL,
    policy_blob TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    repository_id TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    event_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
);

CREATE UNIQUE INDEX events_aggregate_version
    ON events(repository_id, aggregate_type, aggregate_id, aggregate_version);

CREATE TABLE jobs (
    repository_id TEXT NOT NULL,
    sprint_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    last_event_sequence INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repository_id, sprint_id, ticket_id),
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id),
    FOREIGN KEY (last_event_sequence) REFERENCES events(sequence)
) WITHOUT ROWID;

CREATE TABLE attempts (
    attempt_token TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    sprint_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL UNIQUE,
    execution_unit_id TEXT NOT NULL,
    supervisor_fence TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    last_event_sequence INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (repository_id, sprint_id, ticket_id, attempt_token),
    FOREIGN KEY (repository_id, sprint_id, ticket_id)
        REFERENCES jobs(repository_id, sprint_id, ticket_id),
    FOREIGN KEY (last_event_sequence) REFERENCES events(sequence)
) WITHOUT ROWID;

CREATE TABLE resource_claims (
    claim_key TEXT PRIMARY KEY,
    attempt_token TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    exclusive INTEGER NOT NULL CHECK (exclusive IN (0, 1)),
    units INTEGER NOT NULL CHECK (units > 0),
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    FOREIGN KEY (attempt_token) REFERENCES attempts(attempt_token)
) WITHOUT ROWID;

CREATE UNIQUE INDEX active_exclusive_resource_claim
    ON resource_claims(resource_type, resource_id)
    WHERE released_at IS NULL AND exclusive = 1;

CREATE TABLE timers (
    timer_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    sprint_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    timer_type TEXT NOT NULL,
    generation TEXT NOT NULL,
    due_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'fired', 'cancelled')),
    UNIQUE (repository_id, sprint_id, ticket_id, timer_type, generation),
    FOREIGN KEY (repository_id, sprint_id, ticket_id)
        REFERENCES jobs(repository_id, sprint_id, ticket_id)
) WITHOUT ROWID;

CREATE TABLE external_operations (
    operation_key TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    sprint_id TEXT,
    ticket_id TEXT,
    operation_type TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('intended', 'submitted', 'settled', 'needs_reconcile')),
    receipt_json TEXT CHECK (receipt_json IS NULL OR json_valid(receipt_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
) WITHOUT ROWID;

CREATE TABLE migration_receipts (
    receipt_id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    normalized_export_digest TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (repository_id, source_kind, source_digest),
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
) WITHOUT ROWID;

CREATE TRIGGER events_are_immutable_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;

CREATE TRIGGER events_are_immutable_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are immutable');
END;
