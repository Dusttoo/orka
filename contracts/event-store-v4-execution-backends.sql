CREATE TABLE execution_backends (
    execution_key TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    attempt_token TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    execution_unit_id TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    supervisor_fence TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    last_event_sequence INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'intended', 'launched', 'attached', 'cancelling',
            'terminal', 'fenced', 'uncertain'
        )
    ),
    launch_receipt_json TEXT CHECK (
        launch_receipt_json IS NULL OR json_valid(launch_receipt_json)
    ),
    attachment_receipt_json TEXT CHECK (
        attachment_receipt_json IS NULL OR json_valid(attachment_receipt_json)
    ),
    heartbeat_receipt_json TEXT CHECK (
        heartbeat_receipt_json IS NULL OR json_valid(heartbeat_receipt_json)
    ),
    progress_receipt_json TEXT CHECK (
        progress_receipt_json IS NULL OR json_valid(progress_receipt_json)
    ),
    cancellation_receipt_json TEXT CHECK (
        cancellation_receipt_json IS NULL OR json_valid(cancellation_receipt_json)
    ),
    fence_receipt_json TEXT CHECK (
        fence_receipt_json IS NULL OR json_valid(fence_receipt_json)
    ),
    inspection_receipt_json TEXT CHECK (
        inspection_receipt_json IS NULL OR json_valid(inspection_receipt_json)
    ),
    terminal_receipt_json TEXT CHECK (
        terminal_receipt_json IS NULL OR json_valid(terminal_receipt_json)
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (
        repository_id, job_id, phase, attempt_token, dispatch_id,
        execution_unit_id, worktree_id, supervisor_fence
    ),
    FOREIGN KEY (repository_id) REFERENCES repositories(repository_id),
    FOREIGN KEY (last_event_sequence) REFERENCES events(sequence)
) WITHOUT ROWID;

CREATE INDEX execution_backends_attempt
    ON execution_backends(attempt_token, state);
