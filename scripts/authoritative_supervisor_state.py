#!/usr/bin/env python3
"""Authoritative supervisor snapshots after transactional runtime cutover.

The legacy supervisor checkpoint remains the compatibility boundary before
cutover.  Once cutover is active, this module makes the event store's
``supervisor:primary`` runtime document the only durable supervisor snapshot.
The elected supervisor keeps the sole writer open for its lifetime; status
clients use a read-only SQLite connection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from event_store import TransactionalEventStore, canonical_json
from runtime_state import repository_identity, repository_layout, runtime_cutover_marker


DATABASE_NAME = "orka-state.sqlite3"
DOCUMENT_TYPE = "supervisor"
DOCUMENT_ID = "primary"


class AuthoritativeStateError(RuntimeError):
    """The transactional supervisor snapshot is absent, stale, or corrupt."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def database_path(repository: Path) -> Path:
    return repository_layout(repository).state_root / DATABASE_NAME


def read_authoritative_state(repository: Path) -> dict[str, Any] | None:
    """Read one committed supervisor generation without acquiring writer authority."""

    marker = runtime_cutover_marker(repository)
    if marker is None:
        return None
    identity = repository_identity(repository)
    try:
        database = sqlite3.connect(f"file:{database_path(repository)}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        database.execute("BEGIN")
        cutover = database.execute(
            """
            SELECT activation_id, state FROM runtime_cutovers
            WHERE repository_id = ?
            """,
            (identity["repository_uuid"],),
        ).fetchone()
        row = database.execute(
            """
            SELECT generation, payload_json, payload_digest
            FROM runtime_documents
            WHERE repository_id = ? AND document_type = ? AND document_id = ?
            """,
            (identity["repository_uuid"], DOCUMENT_TYPE, DOCUMENT_ID),
        ).fetchone()
        database.commit()
    except (OSError, sqlite3.Error) as exc:
        raise AuthoritativeStateError(
            f"cannot read authoritative supervisor state: {exc}"
        ) from exc
    finally:
        if "database" in locals():
            database.close()
    if cutover is None or tuple(map(str, cutover)) != (
        str(marker["activation_id"]),
        "active",
    ):
        raise AuthoritativeStateError(
            "transactional supervisor cutover activation is missing or stale"
        )
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise AuthoritativeStateError(
            "authoritative supervisor state is malformed"
        ) from exc
    if not isinstance(payload, dict) or _digest(payload) != str(row["payload_digest"]):
        raise AuthoritativeStateError(
            "authoritative supervisor state digest is invalid"
        )
    return payload


class AuthoritativeSupervisorState:
    """Serialize all supervisor generations through its one event-store writer."""

    def __init__(
        self,
        repository: Path,
        store: TransactionalEventStore,
        *,
        writer_identity: str,
        supervisor_fence: str,
    ) -> None:
        marker = runtime_cutover_marker(repository)
        if marker is None:
            raise AuthoritativeStateError("transactional runtime cutover is not active")
        self.repository = repository
        self.store = store
        self.writer_identity = writer_identity
        self.supervisor_fence = supervisor_fence
        self.repository_id = repository_identity(repository)["repository_uuid"]
        self.activation_id = str(marker["activation_id"])
        self._lock = threading.RLock()
        self._generation = self._load_generation()

    def _snapshot_document(self) -> dict[str, Any] | None:
        snapshot = self.store.runtime_snapshot(repository_id=self.repository_id)
        cutover = snapshot.get("cutover") or {}
        if (
            str(cutover.get("activation_id") or "") != self.activation_id
            or cutover.get("state") != "active"
        ):
            raise AuthoritativeStateError(
                "transactional supervisor cutover activation is missing or stale"
            )
        for document in snapshot.get("documents") or []:
            if (
                document.get("document_type") == DOCUMENT_TYPE
                and document.get("document_id") == DOCUMENT_ID
            ):
                payload = document.get("payload")
                if not isinstance(payload, dict) or _digest(payload) != document.get(
                    "payload_digest"
                ):
                    raise AuthoritativeStateError(
                        "authoritative supervisor state digest is invalid"
                    )
                return document
        return None

    def _load_generation(self) -> int:
        document = self._snapshot_document()
        return int(document.get("generation") or 0) if document else 0

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            document = self._snapshot_document()
            if document is None:
                self._generation = 0
                return None
            generation = int(document["generation"])
            if generation < self._generation:
                raise AuthoritativeStateError(
                    "authoritative supervisor generation moved backwards"
                )
            self._generation = generation
            return dict(document["payload"])

    def persist(self, state: dict[str, Any]) -> str:
        """Commit one complete supervisor generation and return its digest."""

        with self._lock:
            payload_digest = _digest(state)
            expected = self._generation
            result = self.store.write_runtime_document(
                repository_id=self.repository_id,
                activation_id=self.activation_id,
                document_type=DOCUMENT_TYPE,
                document_id=DOCUMENT_ID,
                expected_generation=expected,
                payload=state,
                supervisor_fence=self.supervisor_fence,
                idempotency_key=(
                    f"supervisor-state:{self.activation_id}:{expected + 1}:"
                    f"{payload_digest}"
                ),
                writer_identity=self.writer_identity,
                operation_digest=payload_digest,
            )
            self._generation = result.generation
            return result.payload_digest

    @property
    def generation(self) -> int:
        return self._generation
