CREATE TABLE runtime_cutovers (
    repository_id TEXT PRIMARY KEY,
    activation_id TEXT NOT NULL UNIQUE,
    marker_digest TEXT NOT NULL,
    minimum_version TEXT NOT NULL,
    legacy_snapshot_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'rolled_back')),
    activation_generation INTEGER NOT NULL CHECK (activation_generation > 0),
    activated_at TEXT NOT NULL,
    first_authoritative_sequence INTEGER,
    rolled_back_at TEXT,
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id),
    FOREIGN KEY (first_authoritative_sequence) REFERENCES events(sequence)
) WITHOUT ROWID;

CREATE TABLE runtime_documents (
    repository_id TEXT NOT NULL,
    document_type TEXT NOT NULL,
    document_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_digest TEXT NOT NULL,
    supervisor_fence TEXT NOT NULL,
    last_event_sequence INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repository_id, document_type, document_id),
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id),
    FOREIGN KEY (last_event_sequence) REFERENCES events(sequence)
) WITHOUT ROWID;
