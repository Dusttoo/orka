#!/usr/bin/env python3
"""Repository-scoped transactional event store for the Orka 2 supervisor.

This module deliberately does not replace the JSON controller.  It provides the
storage boundary selected by ADR 0001 so the later import and cutover slices can
integrate a tested API instead of issuing ad-hoc SQL.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 3
DEFAULT_BUSY_TIMEOUT_MS = 5_000
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts/event-store-v1.sql"
LEGACY_IMPORT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/event-store-v2-legacy-import.sql"
)
RUNTIME_CUTOVER_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/event-store-v3-runtime-cutover.sql"
)


class EventStoreError(RuntimeError):
    """Base class for store failures that must fail controller admission."""


class StaleWriteError(EventStoreError):
    """The supplied aggregate version or execution fence is no longer current."""


class IdempotencyConflict(EventStoreError):
    """An idempotency key was reused with different operation material."""


class WriterAuthorityError(EventStoreError):
    """A mutation did not originate from the supervisor-owned writer."""


@dataclass(frozen=True)
class RepositoryBinding:
    repository_id: str
    common_directory: str
    object_directory_id: str
    policy_ref: str
    policy_path: str
    policy_commit: str
    policy_blob: str
    policy_digest: str
    created_at: str


@dataclass(frozen=True)
class StoreStatus:
    database_path: str
    schema_version: int
    journal_mode: str
    journal_reason: str
    sqlite_version: str
    busy_timeout_ms: int


@dataclass(frozen=True)
class TransitionResult:
    sequence: int
    replayed: bool
    aggregate_version: int


@dataclass(frozen=True)
class ImportResult:
    receipt_id: str
    replayed: bool
    source_count: int
    record_count: int


@dataclass(frozen=True)
class CutoverResult:
    activation_id: str
    replayed: bool
    state: str
    seeded_documents: int
    first_authoritative_sequence: int | None


@dataclass(frozen=True)
class RuntimeDocumentResult:
    sequence: int
    replayed: bool
    generation: int
    payload_digest: str


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def wal_reset_fix_available(version: str) -> bool:
    try:
        parts = tuple(int(part) for part in version.split(".")[:3])
    except ValueError:
        return False
    if len(parts) != 3:
        return False
    return (
        parts >= (3, 51, 3)
        or (3, 50, 7) <= parts < (3, 51, 0)
        or (3, 44, 6) <= parts < (3, 45, 0)
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def migration_specifications() -> tuple[tuple[int, str, Path], ...]:
    """Return the one authoritative ordered event-store migration manifest."""

    return (
        (1, "0001-initial-event-store", SCHEMA_PATH),
        (2, "0002-legacy-import", LEGACY_IMPORT_SCHEMA_PATH),
        (3, "0003-runtime-cutover", RUNTIME_CUTOVER_SCHEMA_PATH),
    )


class TransactionalEventStore:
    """One supervisor-owned SQLite writer with serialized transactions.

    The connection may be called by completion callbacks on different host
    threads, but all writes pass through one lock and one connection.  Every
    mutation also requires the opaque writer identity selected by the owning
    supervisor.  Workers are not given this object or identity.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        writer_identity: str,
        local_filesystem: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        schema_path: Path = SCHEMA_PATH,
        expected_database_identity: tuple[int, int] | None = None,
        startup_validator: Callable[[sqlite3.Connection, Path], None] | None = None,
    ) -> None:
        if not writer_identity.strip():
            raise WriterAuthorityError("writer identity must be non-empty")
        if busy_timeout_ms <= 0:
            raise EventStoreError("busy timeout must be positive")
        self.database_path = database_path.resolve()
        self._writer_identity = writer_identity
        self._busy_timeout_ms = busy_timeout_ms
        self._expected_database_identity = expected_database_identity
        self._write_lock = threading.RLock()
        self._closed = False
        self._validate_database_path()
        self._writer_lock_handle = self._acquire_writer_lock()
        try:
            self._verify_expected_database_identity()
            self._database = sqlite3.connect(
                self.database_path,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._verify_expected_database_identity()
            if startup_validator is not None:
                try:
                    startup_validator(self._database, self.database_path)
                except Exception as exc:
                    raise EventStoreError(
                        "event-store startup authority changed after diagnostics"
                    ) from exc
            os.chmod(self.database_path, 0o600)
            self._database.execute("PRAGMA foreign_keys = ON")
            self._database.execute("PRAGMA synchronous = FULL")
            self._database.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            self._journal_mode, self._journal_reason = self._select_journal_mode(
                local_filesystem=local_filesystem
            )
            self._apply_migrations(schema_path)
            self._validate_connection_settings()
        except BaseException:
            if hasattr(self, "_database"):
                self._database.close()
            self._release_writer_lock()
            raise

    def __enter__(self) -> "TransactionalEventStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._write_lock:
            if not self._closed:
                self._database.close()
                self._release_writer_lock()
                self._closed = True

    def _validate_database_path(self) -> None:
        parent = self.database_path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise EventStoreError("database parent must be a real local directory")
        if self.database_path.exists():
            mode = self.database_path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise EventStoreError("database path must be a regular file, not a symlink")

    def _verify_expected_database_identity(self) -> None:
        if self._expected_database_identity is None:
            return
        try:
            metadata = self.database_path.lstat()
        except OSError as exc:
            raise EventStoreError("checked event-store database is unavailable") from exc
        observed = (metadata.st_dev, metadata.st_ino)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or observed != self._expected_database_identity
        ):
            raise EventStoreError(
                "event-store database identity changed after startup diagnostics"
            )

    def _acquire_writer_lock(self) -> Any:
        lock_path = self.database_path.with_name(self.database_path.name + ".writer.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            with contextlib.suppress(UnboundLocalError, OSError):
                handle.close()
            with contextlib.suppress(UnboundLocalError, OSError):
                os.close(descriptor)
            raise WriterAuthorityError(
                "another supervisor already owns the event-store writer lock"
            ) from exc
        return handle

    def _release_writer_lock(self) -> None:
        handle = getattr(self, "_writer_lock_handle", None)
        if handle is not None and not handle.closed:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _select_journal_mode(self, *, local_filesystem: bool) -> tuple[str, str]:
        fixed = wal_reset_fix_available(sqlite3.sqlite_version)
        requested = "WAL" if local_filesystem and fixed else "DELETE"
        selected = str(
            self._database.execute(f"PRAGMA journal_mode = {requested}").fetchone()[0]
        ).lower()
        expected = requested.lower()
        if selected != expected:
            raise EventStoreError(
                f"SQLite selected journal mode {selected!r}, expected {expected!r}"
            )
        if not local_filesystem:
            reason = "rollback journal: caller did not prove a local filesystem"
        elif not fixed:
            reason = (
                "rollback journal: loaded SQLite does not contain the documented "
                "WAL-reset fix"
            )
        else:
            reason = "WAL enabled: local filesystem and fixed SQLite release verified"
        return selected, reason

    def _apply_migrations(self, schema_path: Path) -> None:
        specifications = tuple(
            (version, migration_id, schema_path if version == 1 else path)
            for version, migration_id, path in migration_specifications()
        )
        migrations: list[tuple[int, str, str, str]] = []
        for version, migration_id, path in specifications:
            try:
                schema = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise EventStoreError(f"cannot read event-store schema: {path}") from exc
            migrations.append(
                (
                    version,
                    migration_id,
                    hashlib.sha256(schema.encode("utf-8")).hexdigest(),
                    schema,
                )
            )
        has_ledger = self._database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied: list[tuple[int, str, str]] = []
        if has_ledger:
            applied = self._database.execute(
                "SELECT version, migration_id, source_digest FROM schema_migrations ORDER BY version"
            ).fetchall()
            expected_prefix = [row[:3] for row in migrations[: len(applied)]]
            if applied != expected_prefix or len(applied) > len(migrations):
                raise EventStoreError(
                    "event-store migration ledger mismatch: "
                    f"expected prefix {expected_prefix!r}, found {applied!r}"
                )
        for version, migration_id, source_digest, schema in migrations[len(applied) :]:
            migration = (
                "BEGIN IMMEDIATE;\n"
                + schema
                + "\nINSERT INTO schema_migrations(version, migration_id, source_digest, applied_at) "
                + f"VALUES ({version}, {_sql_literal(migration_id)}, "
                + f"{_sql_literal(source_digest)}, {_sql_literal(utc_now())});\n"
                + "INSERT INTO metadata(key, value) VALUES "
                + f"('schema_version', {_sql_literal(str(version))}) "
                + "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\nCOMMIT;\n"
            )
            try:
                self._database.executescript(migration)
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    self._database.execute("ROLLBACK")
                raise EventStoreError(
                    f"event-store migration {migration_id} failed atomically"
                ) from exc

    def _validate_connection_settings(self) -> None:
        foreign_keys = int(self._database.execute("PRAGMA foreign_keys").fetchone()[0])
        synchronous = int(self._database.execute("PRAGMA synchronous").fetchone()[0])
        timeout = int(self._database.execute("PRAGMA busy_timeout").fetchone()[0])
        if foreign_keys != 1 or synchronous != 2 or timeout != self._busy_timeout_ms:
            raise EventStoreError("SQLite durability settings were not applied")

    def _require_writer(self, writer_identity: str) -> None:
        if self._closed:
            raise EventStoreError("event store is closed")
        if writer_identity != self._writer_identity:
            raise WriterAuthorityError("mutation requires the supervisor writer identity")

    @contextlib.contextmanager
    def _transaction(self, writer_identity: str) -> Iterator[sqlite3.Connection]:
        self._require_writer(writer_identity)
        with self._write_lock:
            try:
                self._database.execute("BEGIN IMMEDIATE")
                yield self._database
                self._database.commit()
            except BaseException:
                self._database.rollback()
                raise

    def status(self) -> StoreStatus:
        return StoreStatus(
            database_path=str(self.database_path),
            schema_version=SCHEMA_VERSION,
            journal_mode=self._journal_mode,
            journal_reason=self._journal_reason,
            sqlite_version=sqlite3.sqlite_version,
            busy_timeout_ms=self._busy_timeout_ms,
        )

    def check_integrity(self) -> dict[str, Any]:
        with self._write_lock:
            result = str(self._database.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = self._database.execute("PRAGMA foreign_key_check").fetchall()
        return {"integrity": result, "foreign_key_violations": foreign_keys}

    def event_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        """Read an immutable event receipt on the supervisor-owned connection."""

        with self._write_lock:
            row = self._database.execute(
                """
                SELECT sequence, aggregate_version, payload_json
                FROM events WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "sequence": int(row[0]),
            "aggregate_version": int(row[1]),
            "payload": json.loads(str(row[2])),
        }

    def bind_repository(
        self, binding: RepositoryBinding, *, writer_identity: str
    ) -> None:
        values = tuple(binding.__dict__.values())
        with self._transaction(writer_identity) as database:
            existing = database.execute(
                "SELECT * FROM repositories WHERE repository_id = ?",
                (binding.repository_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise IdempotencyConflict("repository identity is already bound differently")
                return
            other = database.execute("SELECT repository_id FROM repositories LIMIT 1").fetchone()
            if other is not None:
                raise EventStoreError("one event-store database cannot bind multiple repositories")
            database.execute(
                "INSERT INTO repositories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", values
            )

    def _append_event(
        self,
        database: sqlite3.Connection,
        *,
        repository_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        event_type: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        occurred_at: str,
    ) -> TransitionResult:
        payload_json = canonical_json(payload)
        existing = database.execute(
            """
            SELECT sequence, repository_id, aggregate_type, aggregate_id,
                   aggregate_version, event_type, payload_json
            FROM events WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        expected = (
            repository_id,
            aggregate_type,
            aggregate_id,
            aggregate_version,
            event_type,
            payload_json,
        )
        if existing is not None:
            if tuple(existing[1:]) != expected:
                raise IdempotencyConflict(
                    f"idempotency key {idempotency_key!r} was reused with different material"
                )
            return TransitionResult(int(existing[0]), True, aggregate_version)
        event_id = _digest(
            canonical_json(
                {
                    "repository_id": repository_id,
                    "idempotency_key": idempotency_key,
                }
            )
        )
        try:
            cursor = database.execute(
                """
                INSERT INTO events(
                    event_id, repository_id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, idempotency_key,
                    payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    repository_id,
                    aggregate_type,
                    aggregate_id,
                    aggregate_version,
                    event_type,
                    idempotency_key,
                    payload_json,
                    occurred_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise StaleWriteError(
                f"aggregate {aggregate_type}:{aggregate_id} version {aggregate_version} is stale"
            ) from exc
        return TransitionResult(int(cursor.lastrowid), False, aggregate_version)

    def _existing_event(
        self,
        database: sqlite3.Connection,
        *,
        repository_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        aggregate_version: int | None = None,
    ) -> TransitionResult | None:
        row = database.execute(
            """
            SELECT sequence, repository_id, aggregate_type, aggregate_id,
                   aggregate_version, event_type, payload_json
            FROM events WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        expected = (
            repository_id,
            aggregate_type,
            aggregate_id,
            event_type,
            canonical_json(payload),
        )
        actual = (str(row[1]), str(row[2]), str(row[3]), str(row[5]), str(row[6]))
        if actual != expected or (
            aggregate_version is not None and int(row[4]) != aggregate_version
        ):
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key!r} was reused with different material"
            )
        return TransitionResult(int(row[0]), True, int(row[4]))

    @staticmethod
    def _job_id(sprint_id: str, ticket_id: str) -> str:
        return f"{sprint_id}:{ticket_id}"

    def create_job(
        self,
        *,
        repository_id: str,
        sprint_id: str,
        ticket_id: str,
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        at = occurred_at or utc_now()
        payload = {"sprint_id": sprint_id, "ticket_id": ticket_id, "state": "queued"}
        with self._transaction(writer_identity) as database:
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="job",
                aggregate_id=self._job_id(sprint_id, ticket_id),
                aggregate_version=1,
                event_type="job_created",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                return event
            database.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, 'queued', 1, ?, ?)",
                (repository_id, sprint_id, ticket_id, event.sequence, at),
            )
            return event

    def reserve_job(
        self,
        *,
        repository_id: str,
        sprint_id: str,
        ticket_id: str,
        expected_version: int,
        attempt_token: str,
        dispatch_id: str,
        execution_unit_id: str,
        supervisor_fence: str,
        claims: Sequence[Mapping[str, Any]],
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        at = occurred_at or utc_now()
        next_version = expected_version + 1
        payload = {
            "attempt_token": attempt_token,
            "claims": [dict(claim) for claim in claims],
            "dispatch_id": dispatch_id,
            "execution_unit_id": execution_unit_id,
            "supervisor_fence": supervisor_fence,
        }
        with self._transaction(writer_identity) as database:
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="job",
                aggregate_id=self._job_id(sprint_id, ticket_id),
                aggregate_version=next_version,
                event_type="job_reserved",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                return event
            updated = database.execute(
                """
                UPDATE jobs SET state = 'reserved', version = ?,
                    last_event_sequence = ?, updated_at = ?
                WHERE repository_id = ? AND sprint_id = ? AND ticket_id = ?
                    AND state IN ('queued', 'repair_ready', 'recovery_ready')
                    AND version = ?
                """,
                (
                    next_version,
                    event.sequence,
                    at,
                    repository_id,
                    sprint_id,
                    ticket_id,
                    expected_version,
                ),
            ).rowcount
            if updated != 1:
                raise StaleWriteError("job reservation state or version is stale")
            database.execute(
                """
                INSERT INTO attempts VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'reserved', 1, ?, ?, ?
                )
                """,
                (
                    attempt_token,
                    repository_id,
                    sprint_id,
                    ticket_id,
                    dispatch_id,
                    execution_unit_id,
                    supervisor_fence,
                    event.sequence,
                    at,
                    at,
                ),
            )
            for claim in claims:
                database.execute(
                    """
                    INSERT INTO resource_claims(
                        claim_key, attempt_token, resource_type, resource_id,
                        exclusive, units, acquired_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        claim["claim_key"],
                        attempt_token,
                        claim["resource_type"],
                        claim["resource_id"],
                        1 if claim.get("exclusive", False) else 0,
                        int(claim.get("units", 1)),
                        at,
                    ),
                )
            return event

    def launch_job(
        self,
        *,
        attempt_token: str,
        dispatch_id: str,
        execution_unit_id: str,
        supervisor_fence: str,
        expected_job_version: int,
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        at = occurred_at or utc_now()
        with self._transaction(writer_identity) as database:
            attempt = database.execute(
                """
                SELECT repository_id, sprint_id, ticket_id, state, version,
                       dispatch_id, execution_unit_id, supervisor_fence
                FROM attempts WHERE attempt_token = ?
                """,
                (attempt_token,),
            ).fetchone()
            if attempt is None:
                raise StaleWriteError("launch attempt identity or fence is stale")
            repository_id, sprint_id, ticket_id = map(str, attempt[:3])
            next_version = expected_job_version + 1
            payload = {
                "attempt_token": attempt_token,
                "dispatch_id": dispatch_id,
                "execution_unit_id": execution_unit_id,
                "supervisor_fence": supervisor_fence,
            }
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="job",
                aggregate_id=self._job_id(sprint_id, ticket_id),
                aggregate_version=next_version,
                event_type="job_launched",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                return event
            if tuple(attempt[3:]) != (
                "reserved",
                1,
                dispatch_id,
                execution_unit_id,
                supervisor_fence,
            ):
                raise StaleWriteError("launch attempt identity or fence is stale")
            updated = database.execute(
                """
                UPDATE jobs SET state = 'running', version = ?,
                    last_event_sequence = ?, updated_at = ?
                WHERE repository_id = ? AND sprint_id = ? AND ticket_id = ?
                    AND state = 'reserved' AND version = ?
                """,
                (
                    next_version,
                    event.sequence,
                    at,
                    repository_id,
                    sprint_id,
                    ticket_id,
                    expected_job_version,
                ),
            ).rowcount
            if updated != 1:
                raise StaleWriteError("launch job state or version is stale")
            updated = database.execute(
                """
                UPDATE attempts SET state = 'running', version = 2,
                    last_event_sequence = ?, updated_at = ?
                WHERE attempt_token = ? AND state = 'reserved' AND version = 1
                """,
                (event.sequence, at, attempt_token),
            ).rowcount
            if updated != 1:
                raise StaleWriteError("launch attempt projection is stale")
            return event

    def complete_job(
        self,
        *,
        attempt_token: str,
        dispatch_id: str,
        execution_unit_id: str,
        supervisor_fence: str,
        expected_job_version: int,
        terminal_state: str,
        terminal_envelope_digest: str,
        idempotency_key: str,
        writer_identity: str,
        retry_timer: Mapping[str, str] | None = None,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        if terminal_state not in {
            "completed",
            "repair_ready",
            "recovery_ready",
            "operator_action",
            "external_blocked",
        }:
            raise EventStoreError(f"unsupported terminal job state: {terminal_state}")
        at = occurred_at or utc_now()
        with self._transaction(writer_identity) as database:
            attempt = database.execute(
                """
                SELECT repository_id, sprint_id, ticket_id, state, version,
                       dispatch_id, execution_unit_id, supervisor_fence
                FROM attempts WHERE attempt_token = ?
                """,
                (attempt_token,),
            ).fetchone()
            if attempt is None:
                raise StaleWriteError("completion attempt identity or fence is stale")
            repository_id, sprint_id, ticket_id = map(str, attempt[:3])
            next_version = expected_job_version + 1
            payload: dict[str, Any] = {
                "attempt_token": attempt_token,
                "dispatch_id": dispatch_id,
                "execution_unit_id": execution_unit_id,
                "supervisor_fence": supervisor_fence,
                "terminal_envelope_digest": terminal_envelope_digest,
                "terminal_state": terminal_state,
            }
            if retry_timer is not None:
                payload["retry_timer"] = dict(retry_timer)
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="job",
                aggregate_id=self._job_id(sprint_id, ticket_id),
                aggregate_version=next_version,
                event_type="job_completed",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                return event
            if tuple(attempt[3:]) != (
                "running",
                2,
                dispatch_id,
                execution_unit_id,
                supervisor_fence,
            ):
                raise StaleWriteError("completion attempt identity or fence is stale")
            updated = database.execute(
                """
                UPDATE jobs SET state = ?, version = ?,
                    last_event_sequence = ?, updated_at = ?
                WHERE repository_id = ? AND sprint_id = ? AND ticket_id = ?
                    AND state = 'running' AND version = ?
                """,
                (
                    terminal_state,
                    next_version,
                    event.sequence,
                    at,
                    repository_id,
                    sprint_id,
                    ticket_id,
                    expected_job_version,
                ),
            ).rowcount
            if updated != 1:
                raise StaleWriteError("completion job state or version is stale")
            updated = database.execute(
                """
                UPDATE attempts SET state = ?, version = 3,
                    last_event_sequence = ?, updated_at = ?
                WHERE attempt_token = ? AND state = 'running' AND version = 2
                """,
                (terminal_state, event.sequence, at, attempt_token),
            ).rowcount
            if updated != 1:
                raise StaleWriteError("completion attempt projection is stale")
            database.execute(
                """
                UPDATE resource_claims SET released_at = ?
                WHERE attempt_token = ? AND released_at IS NULL
                """,
                (at, attempt_token),
            )
            if retry_timer is not None:
                database.execute(
                    """
                    INSERT INTO timers(
                        timer_id, repository_id, sprint_id, ticket_id,
                        timer_type, generation, due_at, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        retry_timer["timer_id"],
                        repository_id,
                        sprint_id,
                        ticket_id,
                        retry_timer["timer_type"],
                        retry_timer["generation"],
                        retry_timer["due_at"],
                    ),
                )
            return event

    def intend_external_operation(
        self,
        *,
        repository_id: str,
        sprint_id: str | None,
        ticket_id: str | None,
        operation_key: str,
        operation_type: str,
        request_digest: str,
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        at = occurred_at or utc_now()
        payload = {
            "operation_key": operation_key,
            "operation_type": operation_type,
            "request_digest": request_digest,
            "sprint_id": sprint_id,
            "ticket_id": ticket_id,
        }
        with self._transaction(writer_identity) as database:
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="external_operation",
                aggregate_id=operation_key,
                aggregate_version=1,
                event_type="external_operation_intended",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                return event
            database.execute(
                """
                INSERT INTO external_operations(
                    operation_key, repository_id, sprint_id, ticket_id,
                    operation_type, request_digest, state, receipt_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'intended', NULL, ?, ?)
                """,
                (
                    operation_key,
                    repository_id,
                    sprint_id,
                    ticket_id,
                    operation_type,
                    request_digest,
                    at,
                    at,
                ),
            )
            return event

    def settle_external_operation(
        self,
        *,
        repository_id: str,
        operation_key: str,
        request_digest: str,
        expected_version: int,
        state: str,
        receipt: Mapping[str, Any] | None,
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        if state not in {"submitted", "settled", "needs_reconcile"}:
            raise EventStoreError(f"unsupported external operation state: {state}")
        at = occurred_at or utc_now()
        payload = {
            "operation_key": operation_key,
            "receipt": dict(receipt) if receipt is not None else None,
            "request_digest": request_digest,
            "state": state,
        }
        with self._transaction(writer_identity) as database:
            replay = self._existing_event(
                database,
                repository_id=repository_id,
                aggregate_type="external_operation",
                aggregate_id=operation_key,
                event_type="external_operation_updated",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            operation = database.execute(
                "SELECT request_digest, state FROM external_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if operation is None or str(operation[0]) != request_digest:
                raise IdempotencyConflict("external operation request digest changed")
            current_version = int(
                database.execute(
                    """
                    SELECT max(aggregate_version) FROM events
                    WHERE repository_id = ? AND aggregate_type = 'external_operation'
                        AND aggregate_id = ?
                    """,
                    (repository_id, operation_key),
                ).fetchone()[0]
            )
            if current_version != expected_version:
                raise StaleWriteError(
                    f"external operation version is stale: expected {expected_version}, "
                    f"current {current_version}"
                )
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="external_operation",
                aggregate_id=operation_key,
                aggregate_version=expected_version + 1,
                event_type="external_operation_updated",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                return event
            database.execute(
                """
                UPDATE external_operations
                SET state = ?, receipt_json = ?, updated_at = ?
                WHERE operation_key = ? AND request_digest = ?
                """,
                (
                    state,
                    canonical_json(receipt) if receipt is not None else None,
                    at,
                    operation_key,
                    request_digest,
                ),
            )
            return event

    def fire_timer(
        self,
        *,
        repository_id: str,
        timer_id: str,
        generation: str,
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> TransitionResult:
        at = occurred_at or utc_now()
        with self._transaction(writer_identity) as database:
            timer = database.execute(
                """
                SELECT sprint_id, ticket_id, timer_type, generation, state
                FROM timers WHERE timer_id = ? AND repository_id = ?
                """,
                (timer_id, repository_id),
            ).fetchone()
            if timer is None:
                raise StaleWriteError("timer generation or state is stale")
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="timer",
                aggregate_id=timer_id,
                aggregate_version=1,
                event_type="timer_fired",
                idempotency_key=idempotency_key,
                payload={
                    "generation": generation,
                    "sprint_id": str(timer[0]),
                    "ticket_id": str(timer[1]),
                    "timer_type": str(timer[2]),
                },
                occurred_at=at,
            )
            if event.replayed:
                return event
            if tuple(timer[3:]) != (generation, "pending"):
                raise StaleWriteError("timer generation or state is stale")
            updated = database.execute(
                """
                UPDATE timers SET state = 'fired'
                WHERE timer_id = ? AND generation = ? AND state = 'pending'
                """,
                (timer_id, generation),
            ).rowcount
            if updated != 1:
                raise StaleWriteError("timer was concurrently changed")
            return event

    @staticmethod
    def _next_aggregate_version(
        database: sqlite3.Connection,
        repository_id: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> int:
        row = database.execute(
            """
            SELECT max(aggregate_version) FROM events
            WHERE repository_id = ? AND aggregate_type = ? AND aggregate_id = ?
            """,
            (repository_id, aggregate_type, aggregate_id),
        ).fetchone()
        return int(row[0] or 0) + 1

    def activate_runtime_cutover(
        self,
        *,
        repository_id: str,
        activation_id: str,
        marker_digest: str,
        minimum_version: str,
        legacy_snapshot_digest: str,
        seed_documents: Sequence[Mapping[str, Any]],
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> CutoverResult:
        """Activate one no-dual-writer boundary without starting runtime writes."""

        at = occurred_at or utc_now()
        ordered = sorted(
            (dict(item) for item in seed_documents),
            key=lambda item: (str(item["document_type"]), str(item["document_id"])),
        )
        identities = [
            (str(item["document_type"]), str(item["document_id"])) for item in ordered
        ]
        if len(identities) != len(set(identities)):
            raise IdempotencyConflict("cutover seed contains duplicate runtime documents")
        seed_manifest = []
        for item in ordered:
            payload_json = canonical_json(item["payload"])
            payload_digest = _digest(payload_json)
            if payload_digest != item["payload_digest"]:
                raise IdempotencyConflict(
                    "cutover runtime-document digest does not match its payload"
                )
            seed_manifest.append(
                {
                    "document_id": str(item["document_id"]),
                    "document_type": str(item["document_type"]),
                    "payload_digest": payload_digest,
                }
            )
        payload = {
            "activation_id": activation_id,
            "legacy_snapshot_digest": legacy_snapshot_digest,
            "marker_digest": marker_digest,
            "minimum_version": minimum_version,
            "seed_documents": seed_manifest,
        }
        with self._transaction(writer_identity) as database:
            receipt = database.execute(
                """
                SELECT normalized_export_digest FROM migration_receipts
                WHERE repository_id = ? AND source_kind = 'legacy-runtime-v1'
                """,
                (repository_id,),
            ).fetchone()
            if receipt is None or str(receipt[0]) != legacy_snapshot_digest:
                raise IdempotencyConflict(
                    "cutover legacy snapshot does not match the imported migration receipt"
                )
            existing = database.execute(
                """
                SELECT activation_id, marker_digest, minimum_version,
                       legacy_snapshot_digest, state, activation_generation,
                       first_authoritative_sequence
                FROM runtime_cutovers WHERE repository_id = ?
                """,
                (repository_id,),
            ).fetchone()
            expected = (
                activation_id,
                marker_digest,
                minimum_version,
                legacy_snapshot_digest,
            )
            if existing is not None and str(existing[4]) == "active":
                if tuple(map(str, existing[:4])) != expected:
                    raise IdempotencyConflict(
                        "repository already has a different active runtime cutover"
                    )
                replay = self._existing_event(
                    database,
                    repository_id=repository_id,
                    aggregate_type="runtime_cutover",
                    aggregate_id=repository_id,
                    event_type="runtime_cutover_activated",
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
                if replay is None:
                    raise IdempotencyConflict(
                        "active runtime cutover belongs to another activation receipt"
                    )
                return CutoverResult(
                    activation_id,
                    True,
                    "active",
                    len(ordered),
                    int(existing[6]) if existing[6] is not None else None,
                )
            event_version = self._next_aggregate_version(
                database, repository_id, "runtime_cutover", repository_id
            )
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="runtime_cutover",
                aggregate_id=repository_id,
                aggregate_version=event_version,
                event_type="runtime_cutover_activated",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            if event.replayed:
                raise IdempotencyConflict(
                    "cutover activation event exists without an active projection"
                )
            activation_generation = (
                int(existing[5]) + 1 if existing is not None else 1
            )
            database.execute(
                "DELETE FROM runtime_documents WHERE repository_id = ?",
                (repository_id,),
            )
            if existing is None:
                database.execute(
                    """
                    INSERT INTO runtime_cutovers(
                        repository_id, activation_id, marker_digest,
                        minimum_version, legacy_snapshot_digest, state,
                        activation_generation, activated_at,
                        first_authoritative_sequence, rolled_back_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
                    """,
                    (
                        repository_id,
                        activation_id,
                        marker_digest,
                        minimum_version,
                        legacy_snapshot_digest,
                        activation_generation,
                        at,
                    ),
                )
            else:
                database.execute(
                    """
                    UPDATE runtime_cutovers
                    SET activation_id = ?, marker_digest = ?, minimum_version = ?,
                        legacy_snapshot_digest = ?, state = 'active',
                        activation_generation = ?, activated_at = ?,
                        first_authoritative_sequence = NULL, rolled_back_at = NULL
                    WHERE repository_id = ? AND state = 'rolled_back'
                    """,
                    (
                        activation_id,
                        marker_digest,
                        minimum_version,
                        legacy_snapshot_digest,
                        activation_generation,
                        at,
                        repository_id,
                    ),
                )
            for item in ordered:
                database.execute(
                    """
                    INSERT INTO runtime_documents(
                        repository_id, document_type, document_id, generation,
                        payload_json, payload_digest, supervisor_fence,
                        last_event_sequence, updated_at
                    ) VALUES (?, ?, ?, 0, ?, ?, '', NULL, ?)
                    """,
                    (
                        repository_id,
                        item["document_type"],
                        item["document_id"],
                        canonical_json(item["payload"]),
                        item["payload_digest"],
                        at,
                    ),
                )
            return CutoverResult(activation_id, False, "active", len(ordered), None)

    def write_runtime_document(
        self,
        *,
        repository_id: str,
        activation_id: str,
        document_type: str,
        document_id: str,
        expected_generation: int,
        payload: Mapping[str, Any],
        supervisor_fence: str,
        idempotency_key: str,
        writer_identity: str,
        operation_digest: str = "",
        occurred_at: str | None = None,
    ) -> RuntimeDocumentResult:
        """Commit one authoritative runtime generation under the active fence."""

        if not supervisor_fence:
            raise WriterAuthorityError("runtime document write requires a supervisor fence")
        at = occurred_at or utc_now()
        payload_json = canonical_json(payload)
        payload_digest = _digest(payload_json)
        aggregate_id = f"{document_type}:{document_id}"
        event_payload = {
            "activation_id": activation_id,
            "document_id": document_id,
            "document_type": document_type,
            "payload_digest": payload_digest,
            "supervisor_fence": supervisor_fence,
        }
        if operation_digest:
            event_payload["operation_digest"] = operation_digest
        with self._transaction(writer_identity) as database:
            replay = self._existing_event(
                database,
                repository_id=repository_id,
                aggregate_type="runtime_document",
                aggregate_id=aggregate_id,
                event_type="runtime_document_written",
                idempotency_key=idempotency_key,
                payload=event_payload,
                aggregate_version=expected_generation + 1,
            )
            if replay is not None:
                return RuntimeDocumentResult(
                    replay.sequence, True, replay.aggregate_version, payload_digest
                )
            cutover = database.execute(
                """
                SELECT activation_id, state FROM runtime_cutovers
                WHERE repository_id = ?
                """,
                (repository_id,),
            ).fetchone()
            if cutover is None or tuple(map(str, cutover)) != (activation_id, "active"):
                raise StaleWriteError("runtime cutover activation is missing or stale")
            current = database.execute(
                """
                SELECT generation FROM runtime_documents
                WHERE repository_id = ? AND document_type = ? AND document_id = ?
                """,
                (repository_id, document_type, document_id),
            ).fetchone()
            current_generation = int(current[0]) if current is not None else 0
            if current_generation != expected_generation:
                raise StaleWriteError(
                    "runtime document generation is stale: "
                    f"expected {expected_generation}, current {current_generation}"
                )
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="runtime_document",
                aggregate_id=aggregate_id,
                aggregate_version=expected_generation + 1,
                event_type="runtime_document_written",
                idempotency_key=idempotency_key,
                payload=event_payload,
                occurred_at=at,
            )
            if current is None:
                database.execute(
                    """
                    INSERT INTO runtime_documents(
                        repository_id, document_type, document_id, generation,
                        payload_json, payload_digest, supervisor_fence,
                        last_event_sequence, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repository_id,
                        document_type,
                        document_id,
                        event.aggregate_version,
                        payload_json,
                        payload_digest,
                        supervisor_fence,
                        event.sequence,
                        at,
                    ),
                )
            else:
                updated = database.execute(
                    """
                    UPDATE runtime_documents
                    SET generation = ?, payload_json = ?, payload_digest = ?,
                        supervisor_fence = ?, last_event_sequence = ?, updated_at = ?
                    WHERE repository_id = ? AND document_type = ?
                        AND document_id = ? AND generation = ?
                    """,
                    (
                        event.aggregate_version,
                        payload_json,
                        payload_digest,
                        supervisor_fence,
                        event.sequence,
                        at,
                        repository_id,
                        document_type,
                        document_id,
                        expected_generation,
                    ),
                ).rowcount
                if updated != 1:
                    raise StaleWriteError("runtime document changed concurrently")
            database.execute(
                """
                UPDATE runtime_cutovers SET first_authoritative_sequence = ?
                WHERE repository_id = ? AND first_authoritative_sequence IS NULL
                """,
                (event.sequence, repository_id),
            )
            return RuntimeDocumentResult(
                event.sequence,
                False,
                event.aggregate_version,
                payload_digest,
            )

    def rollback_runtime_cutover(
        self,
        *,
        repository_id: str,
        activation_id: str,
        marker_digest: str,
        reason: str,
        idempotency_key: str,
        writer_identity: str,
        occurred_at: str | None = None,
    ) -> CutoverResult:
        """Rollback activation only while no authoritative runtime write exists."""

        if not reason.strip():
            raise EventStoreError("cutover rollback requires a reason")
        at = occurred_at or utc_now()
        payload = {
            "activation_id": activation_id,
            "marker_digest": marker_digest,
            "reason": reason.strip(),
        }
        with self._transaction(writer_identity) as database:
            replay = self._existing_event(
                database,
                repository_id=repository_id,
                aggregate_type="runtime_cutover",
                aggregate_id=repository_id,
                event_type="runtime_cutover_rolled_back",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            current = database.execute(
                """
                SELECT activation_id, marker_digest, state,
                       first_authoritative_sequence
                FROM runtime_cutovers WHERE repository_id = ?
                """,
                (repository_id,),
            ).fetchone()
            if current is None:
                raise StaleWriteError("runtime cutover is not activated")
            if tuple(map(str, current[:2])) != (activation_id, marker_digest):
                raise IdempotencyConflict("cutover rollback identity changed")
            if str(current[2]) == "rolled_back":
                if replay is None:
                    raise IdempotencyConflict(
                        "runtime cutover was rolled back by another receipt"
                    )
                return CutoverResult(activation_id, True, "rolled_back", 0, None)
            if current[3] is not None:
                raise WriterAuthorityError(
                    "runtime cutover cannot roll back after its first authoritative write"
                )
            if replay is not None:
                raise StaleWriteError(
                    "cutover rollback event exists without a rolled-back projection"
                )
            event = self._append_event(
                database,
                repository_id=repository_id,
                aggregate_type="runtime_cutover",
                aggregate_id=repository_id,
                aggregate_version=self._next_aggregate_version(
                    database, repository_id, "runtime_cutover", repository_id
                ),
                event_type="runtime_cutover_rolled_back",
                idempotency_key=idempotency_key,
                payload=payload,
                occurred_at=at,
            )
            database.execute(
                "DELETE FROM runtime_documents WHERE repository_id = ?",
                (repository_id,),
            )
            database.execute(
                """
                UPDATE runtime_cutovers
                SET state = 'rolled_back', rolled_back_at = ?
                WHERE repository_id = ? AND state = 'active'
                """,
                (at, repository_id),
            )
            return CutoverResult(activation_id, event.replayed, "rolled_back", 0, None)

    def runtime_snapshot(self, *, repository_id: str) -> dict[str, Any]:
        """Read cutover and all runtime documents from one SQLite transaction."""

        with self._write_lock:
            self._database.execute("BEGIN")
            try:
                cutover = self._database.execute(
                    "SELECT * FROM runtime_cutovers WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone()
                columns = [
                    item[1]
                    for item in self._database.execute(
                        "PRAGMA table_info(runtime_cutovers)"
                    ).fetchall()
                ]
                document_rows = self._database.execute(
                    """
                    SELECT document_type, document_id, generation, payload_json,
                           payload_digest, supervisor_fence,
                           last_event_sequence, updated_at
                    FROM runtime_documents WHERE repository_id = ?
                    ORDER BY document_type, document_id
                    """,
                    (repository_id,),
                ).fetchall()
                result = {
                    "cutover": dict(zip(columns, cutover)) if cutover else None,
                    "documents": [
                        {
                            "document_type": row[0],
                            "document_id": row[1],
                            "generation": int(row[2]),
                            "payload": json.loads(row[3]),
                            "payload_digest": row[4],
                            "supervisor_fence": row[5],
                            "last_event_sequence": row[6],
                            "updated_at": row[7],
                        }
                        for row in document_rows
                    ],
                }
                self._database.commit()
                return result
            except BaseException:
                self._database.rollback()
                raise

    def import_legacy_snapshot(
        self,
        *,
        repository_id: str,
        receipt_id: str,
        manifest_digest: str,
        normalized_export_digest: str,
        sources: Sequence[Mapping[str, Any]],
        writer_identity: str,
        imported_at: str | None = None,
    ) -> ImportResult:
        """Import one validated legacy manifest in a single transaction.

        Callers must inventory, hash, normalize, and prove ownership before
        entering this boundary. The store refuses both partial replay and a
        different second migration for the repository.
        """

        at = imported_at or utc_now()
        ordered = sorted(
            (dict(source) for source in sources), key=lambda item: str(item["source_path"])
        )
        manifest = {
            "repository_id": repository_id,
            "sources": [
                {
                    "category": source["category"],
                    "normalized_digest": source["normalized_digest"],
                    "record_key": source["record_key"],
                    "source_digest": source["source_digest"],
                    "source_kind": source["source_kind"],
                    "source_path": source["source_path"],
                }
                for source in ordered
            ],
        }
        manifest_json = canonical_json(manifest)
        if _digest(manifest_json) != manifest_digest:
            raise IdempotencyConflict("legacy import manifest digest does not match its sources")
        with self._transaction(writer_identity) as database:
            existing = database.execute(
                """
                SELECT repository_id, source_kind, source_digest,
                       normalized_export_digest, manifest_json
                FROM migration_receipts WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            expected = (
                repository_id,
                "legacy-runtime-v1",
                manifest_digest,
                normalized_export_digest,
                manifest_json,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise IdempotencyConflict(
                        "legacy migration receipt was reused with changed input"
                    )
                return ImportResult(receipt_id, True, len(ordered), len(ordered))
            prior = database.execute(
                "SELECT receipt_id FROM migration_receipts WHERE repository_id = ? LIMIT 1",
                (repository_id,),
            ).fetchone()
            if prior is not None:
                raise IdempotencyConflict(
                    "repository already has a different legacy migration receipt"
                )
            database.execute(
                """
                INSERT INTO migration_receipts(
                    receipt_id, repository_id, source_kind, source_digest,
                    normalized_export_digest, imported_at, manifest_json
                ) VALUES (?, ?, 'legacy-runtime-v1', ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    repository_id,
                    manifest_digest,
                    normalized_export_digest,
                    at,
                    manifest_json,
                ),
            )
            for index, source in enumerate(ordered, start=1):
                payload_json = canonical_json(source["payload"])
                if _digest(payload_json) != source["normalized_digest"]:
                    raise IdempotencyConflict(
                        f"normalized digest changed for {source['source_path']}"
                    )
                database.execute(
                    """
                    INSERT INTO migration_sources(
                        receipt_id, source_path, source_kind, source_digest,
                        normalized_digest
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        source["source_path"],
                        source["source_kind"],
                        source["source_digest"],
                        source["normalized_digest"],
                    ),
                )
                database.execute(
                    """
                    INSERT INTO legacy_records(
                        repository_id, category, record_key, source_path,
                        payload_json, payload_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repository_id,
                        source["category"],
                        source["record_key"],
                        source["source_path"],
                        payload_json,
                        source["normalized_digest"],
                    ),
                )
                self._append_event(
                    database,
                    repository_id=repository_id,
                    aggregate_type="legacy_import",
                    aggregate_id=receipt_id,
                    aggregate_version=index,
                    event_type="legacy_source_imported",
                    idempotency_key=f"legacy-import:{receipt_id}:{index}",
                    payload={
                        "category": source["category"],
                        "normalized_digest": source["normalized_digest"],
                        "record_key": source["record_key"],
                        "source_digest": source["source_digest"],
                        "source_path": source["source_path"],
                    },
                    occurred_at=at,
                )
            return ImportResult(receipt_id, False, len(ordered), len(ordered))

    def rows(self, table: str) -> list[dict[str, Any]]:
        """Return deterministic diagnostic rows for tests and pre-cutover status."""
        allowed = {
            "attempts",
            "events",
            "external_operations",
            "jobs",
            "legacy_records",
            "migration_receipts",
            "migration_sources",
            "resource_claims",
            "runtime_cutovers",
            "runtime_documents",
            "schema_migrations",
            "timers",
        }
        if table not in allowed:
            raise EventStoreError(f"unsupported diagnostic table: {table}")
        with self._write_lock:
            cursor = self._database.execute(f"SELECT * FROM {table} ORDER BY 1")
            names = [description[0] for description in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
