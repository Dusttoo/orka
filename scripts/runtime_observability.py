#!/usr/bin/env python3
"""Versioned, redacted, read-only views of Orka's authoritative runtime."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import stat
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from authoritative_supervisor_state import database_path
from event_store import canonical_json
from runtime_state import RuntimeStateError, repository_identity, runtime_cutover_marker


EVENT_SCHEMA = "orka.runtime-event/v1"
PAGE_SCHEMA = "orka.runtime-event-page/v1"
CURSOR_SCHEMA = "orka.runtime-event-cursor/v1"
METRICS_SCHEMA = "orka.runtime-metrics/v1"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1_000
READ_TIMEOUT_SECONDS = 1.0
PROVIDER_HOLD_STATES = {
    "authentication",
    "incompatible",
    "rate_limited",
    "transport",
    "unknown",
}
PROVIDER_HOLD_SCOPES = {"provider", "route", "unknown"}


class ObservabilityError(RuntimeError):
    """The requested read cannot be authenticated or represented safely."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _repository_binding(repository_id: str) -> str:
    return _digest(f"orka-observability-repository:v1:{repository_id}")


def _subject(repository_id: str, aggregate_type: str, aggregate_id: str) -> str:
    return _digest(
        f"orka-observability-subject:v1:{repository_id}:{aggregate_type}:{aggregate_id}"
    )


def _encode_cursor(repository_binding: str, next_sequence: int) -> str:
    payload = {
        "schema": CURSOR_SCHEMA,
        "repository_binding": repository_binding,
        "next_sequence": next_sequence,
    }
    envelope = {
        "payload": payload,
        "checksum": _digest(canonical_json(payload)),
    }
    encoded = base64.urlsafe_b64encode(canonical_json(envelope).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, repository_binding: str) -> int:
    if len(cursor) > 4096:
        raise ObservabilityError("event cursor is malformed")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ObservabilityError("event cursor is malformed") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "checksum"}:
        raise ObservabilityError("event cursor has an unsupported envelope")
    payload = envelope["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "repository_binding",
        "next_sequence",
    }:
        raise ObservabilityError("event cursor has an unsupported payload")
    if envelope["checksum"] != _digest(canonical_json(payload)):
        raise ObservabilityError("event cursor integrity check failed")
    if payload["schema"] != CURSOR_SCHEMA:
        raise ObservabilityError("event cursor schema is unsupported")
    if payload["repository_binding"] != repository_binding:
        raise ObservabilityError("event cursor belongs to a different repository")
    sequence = payload["next_sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ObservabilityError("event cursor sequence is invalid")
    return sequence


def _connect(repository: Path) -> tuple[sqlite3.Connection, str, str]:
    try:
        marker = runtime_cutover_marker(repository)
        identity = repository_identity(repository)
        path = database_path(repository)
    except RuntimeStateError as exc:
        raise ObservabilityError(f"cannot authenticate repository runtime: {exc}") from exc
    if marker is None:
        raise ObservabilityError("transactional runtime cutover is not active")
    repository_id = str(identity["repository_uuid"])
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ObservabilityError("authoritative event store is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ObservabilityError("authoritative event store is not a regular file")
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=READ_TIMEOUT_SECONDS,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("BEGIN")
        cutover = connection.execute(
            """
            SELECT activation_id, state FROM runtime_cutovers
            WHERE repository_id = ?
            """,
            (repository_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        if "connection" in locals():
            connection.close()
        raise ObservabilityError(f"cannot open observability snapshot: {exc}") from exc
    if cutover is None or (str(cutover["activation_id"]), str(cutover["state"])) != (
        str(marker["activation_id"]),
        "active",
    ):
        connection.close()
        raise ObservabilityError("transactional runtime cutover is missing or stale")
    return connection, repository_id, _repository_binding(repository_id)


def event_page(
    repository: Path,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Copy one deterministic event page, closing SQLite before returning it."""

    if isinstance(limit, bool) or limit < 1 or limit > MAX_LIMIT:
        raise ObservabilityError(f"event limit must be from 1 through {MAX_LIMIT}")
    connection, repository_id, binding = _connect(repository)
    try:
        bounds = connection.execute(
            """
            SELECT MIN(sequence) AS retained_from, MAX(sequence) AS high_water
            FROM events WHERE repository_id = ?
            """,
            (repository_id,),
        ).fetchone()
        retained_from = int(bounds["retained_from"] or 1)
        high_water = int(bounds["high_water"] or 0)
        next_sequence = (
            _decode_cursor(cursor, binding) if cursor else retained_from
        )
        if cursor and next_sequence < retained_from:
            raise ObservabilityError(
                "event cursor expired before the earliest retained sequence"
            )
        rows = connection.execute(
            """
            SELECT sequence, event_id, aggregate_type, aggregate_id,
                   aggregate_version, event_type, occurred_at
            FROM events
            WHERE repository_id = ? AND sequence >= ? AND sequence <= ?
            ORDER BY sequence ASC
            LIMIT ?
            """,
            (repository_id, next_sequence, high_water, limit + 1),
        ).fetchall()
        selected = rows[:limit]
        events = [
            {
                "schema": EVENT_SCHEMA,
                "sequence": int(row["sequence"]),
                "event_id": str(row["event_id"]),
                "repository_binding": binding,
                "occurred_at": str(row["occurred_at"]),
                "transition": {
                    "class": str(row["aggregate_type"]),
                    "name": str(row["event_type"]),
                    "version": int(row["aggregate_version"]),
                },
                "subject": _subject(
                    repository_id,
                    str(row["aggregate_type"]),
                    str(row["aggregate_id"]),
                ),
            }
            for row in selected
        ]
        continuation = (
            int(selected[-1]["sequence"]) + 1 if selected else next_sequence
        )
        has_more = len(rows) > limit
        connection.commit()
    except ObservabilityError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise ObservabilityError(f"cannot read event snapshot: {exc}") from exc
    finally:
        connection.close()
    return {
        "schema": PAGE_SCHEMA,
        "repository_binding": binding,
        "retained_from_sequence": retained_from,
        "high_water_sequence": high_water,
        "events": events,
        "has_more": has_more,
        "next_cursor": _encode_cursor(binding, continuation),
    }


def _safe_number(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    raw = str(value)
    if len(raw) > 64:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return format(parsed, "f")


def _known(value: Any, allowed: set[str]) -> str:
    candidate = str(value or "unknown")
    return candidate if candidate in allowed else "unknown"


def metrics_snapshot(repository: Path) -> dict[str, Any]:
    """Copy stable aggregate metrics without returning private runtime fields."""

    connection, repository_id, binding = _connect(repository)
    try:
        state_rows = connection.execute(
            """
            SELECT state, COUNT(*) AS total FROM jobs
            WHERE repository_id = ? GROUP BY state ORDER BY state
            """,
            (repository_id,),
        ).fetchall()
        states = Counter({str(row["state"]): int(row["total"]) for row in state_rows})
        transition_rows = connection.execute(
            """
            SELECT event_type, COUNT(*) AS total FROM events
            WHERE repository_id = ? GROUP BY event_type ORDER BY event_type
            """,
            (repository_id,),
        ).fetchall()
        transitions = {
            str(row["event_type"]): int(row["total"]) for row in transition_rows
        }
        timers = connection.execute(
            """
            SELECT state, COUNT(*) AS total FROM timers
            WHERE repository_id = ? GROUP BY state ORDER BY state
            """,
            (repository_id,),
        ).fetchall()
        timer_counts = {str(row["state"]): int(row["total"]) for row in timers}
        recovered = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM events AS recovery
                WHERE recovery.repository_id = ?
                  AND recovery.event_type = 'job_completed'
                  AND json_extract(
                      recovery.payload_json, '$.terminal_state'
                  ) = 'recovery_ready'
                  AND EXISTS (
                      SELECT 1 FROM events AS resumed
                      WHERE resumed.repository_id = recovery.repository_id
                        AND resumed.aggregate_type = recovery.aggregate_type
                        AND resumed.aggregate_id = recovery.aggregate_id
                        AND resumed.sequence > recovery.sequence
                        AND resumed.event_type = 'job_reserved'
                  )
                """,
                (repository_id,),
            ).fetchone()[0]
        )
        active_claims = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT resource_claims.attempt_token)
                FROM resource_claims
                JOIN attempts USING (attempt_token)
                WHERE attempts.repository_id = ?
                  AND resource_claims.released_at IS NULL
                """,
                (repository_id,),
            ).fetchone()[0]
        )
        document = connection.execute(
            """
            SELECT payload_json, payload_digest, updated_at FROM runtime_documents
            WHERE repository_id = ? AND document_type = 'supervisor'
              AND document_id = 'primary'
            """,
            (repository_id,),
        ).fetchone()
        supervisor: dict[str, Any] = {}
        observed_at = ""
        if document is not None:
            observed_at = str(document["updated_at"])
            loaded = json.loads(str(document["payload_json"]))
            if (
                not isinstance(loaded, dict)
                or _digest(canonical_json(loaded)) != str(document["payload_digest"])
            ):
                raise ObservabilityError(
                    "authoritative supervisor state digest is invalid"
                )
            supervisor = loaded
        planning = supervisor.get("planning")
        planning = planning if isinstance(planning, dict) else {}
        provider_holds = planning.get("provider_holds")
        provider_holds = provider_holds if isinstance(provider_holds, list) else []
        hold_states = Counter()
        hold_scopes = Counter()
        for hold in provider_holds:
            if not isinstance(hold, dict):
                continue
            hold_states[_known(hold.get("state"), PROVIDER_HOLD_STATES)] += 1
            hold_scopes[_known(hold.get("scope"), PROVIDER_HOLD_SCOPES)] += 1
        budget = planning.get("budget")
        budget = budget if isinstance(budget, dict) else {}
        lane_capacity = planning.get("concurrency_max")
        if not isinstance(lane_capacity, int) or isinstance(lane_capacity, bool):
            lane_capacity = None
        connection.commit()
    except ObservabilityError:
        connection.rollback()
        raise
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        connection.rollback()
        raise ObservabilityError(f"cannot read metrics snapshot: {exc}") from exc
    finally:
        connection.close()

    running = sum(states[name] for name in ("running", "reserved", "launch_uncertain"))
    queued = sum(
        states[name]
        for name in ("queued", "repair_ready", "recovery_ready", "decomposition_ready")
    )
    waiting = states["retry_wait"]
    parked = sum(states[name] for name in ("parked_decision", "operator_action"))
    blocked = sum(states[name] for name in ("blocked", "external_blocked", "parked_external"))
    completed = sum(states[name] for name in ("completed", "decomposed", "cancelled"))
    total = sum(states.values())
    return {
        "schema": METRICS_SCHEMA,
        "repository_binding": binding,
        "observed_at": observed_at,
        "work": {
            "running": running,
            "queued": queued,
            "waiting": waiting,
            "parked": parked,
            "blocked": blocked,
            "recovered": recovered,
            "completed": completed,
        },
        "lanes": {
            "active": active_claims,
            "capacity": lane_capacity,
        },
        "transitions": transitions,
        "retries": {
            "pending": timer_counts.get("pending", 0),
            "fired": timer_counts.get("fired", 0),
            "cancelled": timer_counts.get("cancelled", 0),
        },
        "recovery": {
            "ready": states.get("recovery_ready", 0),
            "transitions": recovered,
        },
        "provider_holds": {
            "total": sum(hold_states.values()),
            "by_state": dict(sorted(hold_states.items())),
            "by_scope": dict(sorted(hold_scopes.items())),
        },
        "cost": {
            "api": {
                "measurement": "metered",
                "spent_usd": _safe_number(budget.get("spent_usd")),
                "reserved_usd": _safe_number(budget.get("reserved_usd")),
                "projected_usd": _safe_number(budget.get("projected_usd")),
            },
            "desktop_subscription": {
                "measurement": "unmetered",
                "spent_usd": None,
            },
        },
        "completion": {
            "completed": completed,
            "total": total,
            "ratio": (completed / total) if total else None,
        },
    }


def jsonl_records(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a page as metadata/event/cursor records for line-oriented drains."""

    metadata = {
        "record": "metadata",
        "schema": page["schema"],
        "repository_binding": page["repository_binding"],
        "retained_from_sequence": page["retained_from_sequence"],
        "high_water_sequence": page["high_water_sequence"],
    }
    records = [metadata]
    records.extend({"record": "event", **event} for event in page["events"])
    records.append(
        {
            "record": "cursor",
            "schema": CURSOR_SCHEMA,
            "has_more": page["has_more"],
            "next_cursor": page["next_cursor"],
        }
    )
    return records
