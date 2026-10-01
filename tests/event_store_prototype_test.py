#!/usr/bin/env python3
"""Executable prototype for ADR 0001 transaction and concurrency claims."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contracts/event-store-v1.sql"
REPOSITORY_ID = "repo-00000000-0000-4000-8000-000000000001"
NOW = "2026-10-01T00:00:00Z"


def wal_reset_fix_available(version: str) -> bool:
    parsed = tuple(int(part) for part in version.split(".")[:3])
    return (
        parsed >= (3, 51, 3)
        or (3, 50, 7) <= parsed < (3, 51, 0)
        or (3, 44, 6) <= parsed < (3, 45, 0)
    )


def connect(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path, timeout=5, isolation_level=None)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA synchronous = FULL")
    database.execute("PRAGMA busy_timeout = 5000")
    return database


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def append_event(
    database: sqlite3.Connection,
    *,
    ticket: str,
    version: int,
    event_type: str,
    idempotency_key: str,
) -> int:
    event_id = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    cursor = database.execute(
        """
        INSERT INTO events(
            event_id, repository_id, aggregate_type, aggregate_id,
            aggregate_version, event_type, idempotency_key, payload_json,
            occurred_at
        ) VALUES (?, ?, 'job', ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            REPOSITORY_ID,
            f"65:{ticket}",
            version,
            event_type,
            idempotency_key,
            canonical_json({"ticket": ticket}),
            NOW,
        ),
    )
    return int(cursor.lastrowid)


class TransactionalEventStorePrototypeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "orka-state.sqlite3"
        with connect(self.database_path) as database:
            selected = database.execute(
                "PRAGMA journal_mode = "
                + ("WAL" if wal_reset_fix_available(sqlite3.sqlite_version) else "DELETE")
            ).fetchone()[0]
            self.journal_mode = str(selected).lower()
            database.executescript(SCHEMA.read_text(encoding="utf-8"))
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
            )
            database.execute(
                """
                INSERT INTO repositories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    REPOSITORY_ID,
                    "/srv/repository.git",
                    "objects-sha256",
                    "refs/remotes/origin/main",
                    ".orchestration/config.yaml",
                    "commit-sha",
                    "blob-sha",
                    "policy-sha256",
                    NOW,
                ),
            )
            for number in range(12):
                ticket = f"PROJ-{number + 1}"
                sequence = append_event(
                    database,
                    ticket=ticket,
                    version=1,
                    event_type="worker_attached",
                    idempotency_key=f"launch:{ticket}:attempt-1",
                )
                database.execute(
                    "INSERT INTO jobs VALUES (?, '65', ?, 'running', 1, ?, ?)",
                    (REPOSITORY_ID, ticket, sequence, NOW),
                )
                database.execute(
                    """
                    INSERT INTO attempts VALUES (
                        ?, ?, '65', ?, ?, ?, ?, 'running', 1, ?, ?, ?
                    )
                    """,
                    (
                        f"attempt:{ticket}:1",
                        REPOSITORY_ID,
                        ticket,
                        f"dispatch:{ticket}:1",
                        f"unit:{ticket}:1",
                        "supervisor:1",
                        sequence,
                        NOW,
                        NOW,
                    ),
                )
            database.commit()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def complete_ticket(self, ticket: str, barrier: threading.Barrier) -> None:
        database = connect(self.database_path)
        try:
            barrier.wait(timeout=5)
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """
                SELECT state, version FROM jobs
                WHERE repository_id = ? AND sprint_id = '65' AND ticket_id = ?
                """,
                (REPOSITORY_ID, ticket),
            ).fetchone()
            self.assertEqual(row, ("running", 1))
            sequence = append_event(
                database,
                ticket=ticket,
                version=2,
                event_type="worker_completed",
                idempotency_key=f"complete:{ticket}:attempt-1",
            )
            updated = database.execute(
                """
                UPDATE jobs SET state = 'completed', version = 2,
                    last_event_sequence = ?, updated_at = ?
                WHERE repository_id = ? AND sprint_id = '65' AND ticket_id = ?
                    AND state = 'running' AND version = 1
                """,
                (sequence, NOW, REPOSITORY_ID, ticket),
            ).rowcount
            self.assertEqual(updated, 1)
            database.execute(
                """
                UPDATE attempts SET state = 'completed', version = 2,
                    last_event_sequence = ?, updated_at = ?
                WHERE attempt_token = ? AND state = 'running' AND version = 1
                """,
                (sequence, NOW, f"attempt:{ticket}:1"),
            )
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()

    def export(self) -> str:
        with connect(self.database_path) as database:
            database.execute("BEGIN")
            value = {
                "events": [
                    dict(zip(("sequence", "aggregate_id", "event_type", "payload_json"), row))
                    for row in database.execute(
                        """
                        SELECT sequence, aggregate_id, event_type, payload_json
                        FROM events ORDER BY sequence
                        """
                    )
                ],
                "jobs": [
                    dict(zip(("sprint_id", "ticket_id", "state", "version"), row))
                    for row in database.execute(
                        """
                        SELECT sprint_id, ticket_id, state, version FROM jobs
                        ORDER BY sprint_id, ticket_id
                        """
                    )
                ],
            }
            database.commit()
        return canonical_json(value) + "\n"

    def test_concurrent_completions_are_atomic_and_export_is_stable(self) -> None:
        expected_mode = "wal" if wal_reset_fix_available(sqlite3.sqlite_version) else "delete"
        self.assertEqual(self.journal_mode, expected_mode)

        barrier = threading.Barrier(12)
        failures: list[BaseException] = []

        def run(ticket: str) -> None:
            try:
                self.complete_ticket(ticket, barrier)
            except BaseException as exc:  # retained and asserted on the parent thread
                failures.append(exc)

        workers = [
            threading.Thread(target=run, args=(f"PROJ-{number + 1}",))
            for number in range(12)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

        with connect(self.database_path) as database:
            completed = database.execute(
                "SELECT count(*) FROM jobs WHERE state = 'completed' AND version = 2"
            ).fetchone()[0]
            completions = database.execute(
                "SELECT count(*) FROM events WHERE event_type = 'worker_completed'"
            ).fetchone()[0]
            integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(completed, 12)
        self.assertEqual(completions, 12)
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])
        self.assertEqual(self.export(), self.export())

    def test_wal_mode_requires_a_documented_fixed_sqlite_release(self) -> None:
        expectations = {
            "3.44.5": False,
            "3.44.6": True,
            "3.45.3": False,
            "3.50.6": False,
            "3.50.7": True,
            "3.51.2": False,
            "3.51.3": True,
            "3.53.4": True,
        }
        self.assertEqual(
            {version: wal_reset_fix_available(version) for version in expectations},
            expectations,
        )

    def test_event_history_is_immutable(self) -> None:
        with connect(self.database_path) as database:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "events are immutable"):
                database.execute("UPDATE events SET event_type = 'changed' WHERE sequence = 1")

    def test_stale_completion_rolls_back_its_event(self) -> None:
        ticket = "PROJ-1"
        idempotency_key = "stale-completion:PROJ-1"
        with connect(self.database_path) as database:
            database.execute("BEGIN IMMEDIATE")
            append_event(
                database,
                ticket=ticket,
                version=2,
                event_type="worker_completed",
                idempotency_key=idempotency_key,
            )
            updated = database.execute(
                """
                UPDATE jobs SET state = 'completed', version = 2
                WHERE repository_id = ? AND sprint_id = '65' AND ticket_id = ?
                    AND state = 'running' AND version = 99
                """,
                (REPOSITORY_ID, ticket),
            ).rowcount
            self.assertEqual(updated, 0)
            database.rollback()
            count = database.execute(
                "SELECT count(*) FROM events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
