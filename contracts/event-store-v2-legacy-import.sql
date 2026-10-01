ALTER TABLE migration_receipts
    ADD COLUMN manifest_json TEXT CHECK (
        manifest_json IS NULL OR json_valid(manifest_json)
    );

CREATE TABLE migration_sources (
    receipt_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    normalized_digest TEXT NOT NULL,
    PRIMARY KEY (receipt_id, source_path),
    FOREIGN KEY (receipt_id) REFERENCES migration_receipts(receipt_id)
) WITHOUT ROWID;

CREATE TABLE legacy_records (
    repository_id TEXT NOT NULL,
    category TEXT NOT NULL,
    record_key TEXT NOT NULL,
    source_path TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_digest TEXT NOT NULL,
    PRIMARY KEY (repository_id, category, record_key),
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id)
) WITHOUT ROWID;
