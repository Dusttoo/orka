#!/usr/bin/env python3
"""Production API tests for the ADR 0001 transactional store core."""

from __future__ import annotations

import importlib.util
import hashlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("event_store", ROOT / "scripts/event_store.py")
assert SPEC and SPEC.loader
event_store = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = event_store
SPEC.loader.exec_module(event_store)

EventStoreError = event_store.EventStoreError
IdempotencyConflict = event_store.IdempotencyConflict
RepositoryBinding = event_store.RepositoryBinding
StaleWriteError = event_store.StaleWriteError
TransactionalEventStore = event_store.TransactionalEventStore
WriterAuthorityError = event_store.WriterAuthorityError
wal_reset_fix_available = event_store.wal_reset_fix_available

WRITER = "supervisor:test:fence-1"
REPOSITORY_ID = "00000000-0000-4000-8000-000000000001"
NOW = "2026-10-01T00:00:00Z"


def binding(**overrides: str) -> RepositoryBinding:
    values = {
        "repository_id": REPOSITORY_ID,
        "common_directory": "/srv/repository.git",
        "object_directory_id": "objects-sha256",
        "policy_ref": "refs/remotes/origin/main",
        "policy_path": ".orchestration/config.yaml",
        "policy_commit": "commit-sha",
        "policy_blob": "blob-sha",
        "policy_digest": "policy-sha256",
        "created_at": NOW,
    }
    values.update(overrides)
    return RepositoryBinding(**values)


class TransactionalEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "orka-state.sqlite3"
        self.store = TransactionalEventStore(self.path, writer_identity=WRITER)
        self.store.bind_repository(binding(), writer_identity=WRITER)

    def tearDown(self) -> None:
        if hasattr(self.store, "_write_lock"):
            self.store.close()
        self.temporary.cleanup()

    def create_job(self, ticket: str) -> None:
        self.store.create_job(
            repository_id=REPOSITORY_ID,
            sprint_id="65",
            ticket_id=ticket,
            idempotency_key=f"create:{ticket}",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def reserve_job(self, ticket: str, *, resource_id: str | None = None) -> None:
        self.store.reserve_job(
            repository_id=REPOSITORY_ID,
            sprint_id="65",
            ticket_id=ticket,
            expected_version=1,
            attempt_token=f"attempt:{ticket}:1",
            dispatch_id=f"dispatch:{ticket}:1",
            execution_unit_id=f"unit:{ticket}:1",
            supervisor_fence="supervisor:1",
            claims=[
                {
                    "claim_key": f"claim:{ticket}:1",
                    "resource_type": "worktree",
                    "resource_id": resource_id or ticket,
                    "exclusive": True,
                    "units": 1,
                }
            ],
            idempotency_key=f"reserve:{ticket}:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def launch_job(self, ticket: str) -> None:
        self.store.launch_job(
            attempt_token=f"attempt:{ticket}:1",
            dispatch_id=f"dispatch:{ticket}:1",
            execution_unit_id=f"unit:{ticket}:1",
            supervisor_fence="supervisor:1",
            expected_job_version=2,
            idempotency_key=f"launch:{ticket}:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def complete_job(self, ticket: str, **overrides: object) -> event_store.TransitionResult:
        values: dict[str, object] = {
            "attempt_token": f"attempt:{ticket}:1",
            "dispatch_id": f"dispatch:{ticket}:1",
            "execution_unit_id": f"unit:{ticket}:1",
            "supervisor_fence": "supervisor:1",
            "expected_job_version": 3,
            "terminal_state": "completed",
            "terminal_envelope_digest": f"terminal:{ticket}",
            "idempotency_key": f"complete:{ticket}:1",
            "writer_identity": WRITER,
            "occurred_at": NOW,
        }
        values.update(overrides)
        return self.store.complete_job(**values)

    def running_job(self, ticket: str) -> None:
        self.create_job(ticket)
        self.reserve_job(ticket)
        self.launch_job(ticket)

    def test_migration_is_idempotent_and_binding_is_fail_closed(self) -> None:
        first = self.store.rows("schema_migrations")
        self.store.close()
        self.store = TransactionalEventStore(self.path, writer_identity=WRITER)
        self.store.bind_repository(binding(), writer_identity=WRITER)
        self.assertEqual(self.store.rows("schema_migrations"), first)
        with self.assertRaisesRegex(IdempotencyConflict, "bound differently"):
            self.store.bind_repository(
                binding(policy_digest="different"), writer_identity=WRITER
            )
        self.assertEqual(self.store.check_integrity()["integrity"], "ok")

    def test_existing_version_one_database_migrates_forward_once(self) -> None:
        self.store.close()
        self.path.unlink()
        schema_path = ROOT / "contracts/event-store-v1.sql"
        schema = schema_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.path) as database:
            database.executescript(schema)
            database.execute(
                "INSERT INTO schema_migrations VALUES (1, ?, ?, ?)",
                ("0001-initial-event-store", digest, NOW),
            )
            database.execute(
                "INSERT INTO metadata(key, value) VALUES ('schema_version', '1')"
            )
        self.store = TransactionalEventStore(self.path, writer_identity=WRITER)
        self.assertEqual(self.store.status().schema_version, 2)
        self.assertEqual(len(self.store.rows("schema_migrations")), 2)
        self.assertEqual(self.store.rows("migration_sources"), [])

    def test_unknown_or_modified_migration_ledger_fails_closed(self) -> None:
        self.store.close()
        with sqlite3.connect(self.path) as database:
            database.execute(
                "UPDATE schema_migrations SET source_digest = 'modified' WHERE version = 1"
            )
        with self.assertRaisesRegex(EventStoreError, "migration ledger mismatch"):
            TransactionalEventStore(self.path, writer_identity=WRITER)

    def test_writer_identity_is_required_for_every_mutation(self) -> None:
        with self.assertRaisesRegex(WriterAuthorityError, "already owns"):
            TransactionalEventStore(self.path, writer_identity="second-supervisor")
        with self.assertRaisesRegex(WriterAuthorityError, "supervisor writer"):
            self.store.create_job(
                repository_id=REPOSITORY_ID,
                sprint_id="65",
                ticket_id="PROJ-1",
                idempotency_key="create:PROJ-1",
                writer_identity="worker:direct-write",
            )
        self.assertEqual(self.store.rows("events"), [])

    def test_reserve_launch_complete_is_atomic_and_releases_claims(self) -> None:
        self.running_job("PROJ-1")
        result = self.complete_job(
            "PROJ-1",
            terminal_state="recovery_ready",
            retry_timer={
                "timer_id": "timer:PROJ-1:retry-1",
                "timer_type": "retry",
                "generation": "1",
                "due_at": "2026-10-01T00:05:00Z",
            },
        )
        self.assertFalse(result.replayed)
        job = self.store.rows("jobs")[0]
        attempt = self.store.rows("attempts")[0]
        claim = self.store.rows("resource_claims")[0]
        timer = self.store.rows("timers")[0]
        self.assertEqual((job["state"], job["version"]), ("recovery_ready", 4))
        self.assertEqual((attempt["state"], attempt["version"]), ("recovery_ready", 3))
        self.assertEqual(claim["released_at"], NOW)
        self.assertEqual(timer["state"], "pending")

        launch_replay = self.store.launch_job(
            attempt_token="attempt:PROJ-1:1",
            dispatch_id="dispatch:PROJ-1:1",
            execution_unit_id="unit:PROJ-1:1",
            supervisor_fence="supervisor:1",
            expected_job_version=2,
            idempotency_key="launch:PROJ-1:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        completion_replay = self.complete_job(
            "PROJ-1",
            terminal_state="recovery_ready",
            retry_timer={
                "timer_id": "timer:PROJ-1:retry-1",
                "timer_type": "retry",
                "generation": "1",
                "due_at": "2026-10-01T00:05:00Z",
            },
        )
        self.assertTrue(launch_replay.replayed)
        self.assertTrue(completion_replay.replayed)
        self.assertEqual(len(self.store.rows("events")), 4)

    def test_changed_idempotency_payload_fails_and_exact_replay_is_noop(self) -> None:
        first = self.store.create_job(
            repository_id=REPOSITORY_ID,
            sprint_id="65",
            ticket_id="PROJ-1",
            idempotency_key="job:create:one",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        replay = self.store.create_job(
            repository_id=REPOSITORY_ID,
            sprint_id="65",
            ticket_id="PROJ-1",
            idempotency_key="job:create:one",
            writer_identity=WRITER,
            occurred_at="2026-10-02T00:00:00Z",
        )
        self.assertEqual(replay.sequence, first.sequence)
        self.assertTrue(replay.replayed)
        with self.assertRaises(IdempotencyConflict):
            self.store.create_job(
                repository_id=REPOSITORY_ID,
                sprint_id="65",
                ticket_id="PROJ-2",
                idempotency_key="job:create:one",
                writer_identity=WRITER,
                occurred_at=NOW,
            )
        self.assertEqual(len(self.store.rows("events")), 1)
        self.assertEqual(len(self.store.rows("jobs")), 1)

    def test_stale_fence_rolls_back_event_projection_and_claims(self) -> None:
        self.running_job("PROJ-1")
        before = len(self.store.rows("events"))
        with self.assertRaisesRegex(StaleWriteError, "identity or fence"):
            self.complete_job("PROJ-1", supervisor_fence="stale")
        self.assertEqual(len(self.store.rows("events")), before)
        self.assertEqual(self.store.rows("jobs")[0]["state"], "running")
        self.assertIsNone(self.store.rows("resource_claims")[0]["released_at"])

    def test_exclusive_claim_conflict_rolls_back_reservation(self) -> None:
        self.create_job("PROJ-1")
        self.create_job("PROJ-2")
        self.reserve_job("PROJ-1", resource_id="shared")
        with self.assertRaises(sqlite3.IntegrityError):
            self.reserve_job("PROJ-2", resource_id="shared")
        jobs = {row["ticket_id"]: row for row in self.store.rows("jobs")}
        self.assertEqual(jobs["PROJ-2"]["state"], "queued")
        self.assertEqual(jobs["PROJ-2"]["version"], 1)
        self.assertNotIn("reserve:PROJ-2:1", {e["idempotency_key"] for e in self.store.rows("events")})

    def test_concurrent_completions_use_one_writer_without_lost_state(self) -> None:
        tickets = [f"PROJ-{number}" for number in range(1, 13)]
        for ticket in tickets:
            self.running_job(ticket)
        barrier = threading.Barrier(len(tickets))
        failures: list[BaseException] = []

        def complete(ticket: str) -> None:
            try:
                barrier.wait(timeout=5)
                self.complete_job(
                    ticket,
                    terminal_state="recovery_ready",
                    retry_timer={
                        "timer_id": f"timer:{ticket}:retry-1",
                        "timer_type": "retry",
                        "generation": "1",
                        "due_at": "2026-10-01T00:05:00Z",
                    },
                )
                self.store.intend_external_operation(
                    repository_id=REPOSITORY_ID,
                    sprint_id="65",
                    ticket_id=ticket,
                    operation_key=f"provider:{ticket}:1",
                    operation_type="provider_request",
                    request_digest=f"request:{ticket}",
                    idempotency_key=f"operation:intend:{ticket}:1",
                    writer_identity=WRITER,
                    occurred_at=NOW,
                )
                self.store.settle_external_operation(
                    repository_id=REPOSITORY_ID,
                    operation_key=f"provider:{ticket}:1",
                    request_digest=f"request:{ticket}",
                    expected_version=1,
                    state="settled",
                    receipt={"response_id": f"response:{ticket}"},
                    idempotency_key=f"operation:settle:{ticket}:1",
                    writer_identity=WRITER,
                    occurred_at=NOW,
                )
            except BaseException as exc:
                failures.append(exc)

        workers = [threading.Thread(target=complete, args=(ticket,)) for ticket in tickets]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(
            all(row["state"] == "recovery_ready" for row in self.store.rows("jobs"))
        )
        self.assertTrue(
            all(row["released_at"] == NOW for row in self.store.rows("resource_claims"))
        )
        self.assertEqual(len(self.store.rows("timers")), 12)
        self.assertTrue(
            all(row["state"] == "settled" for row in self.store.rows("external_operations"))
        )
        self.assertEqual(len(self.store.rows("events")), 72)

    def test_external_operation_and_timer_are_idempotent_and_fenced(self) -> None:
        self.running_job("PROJ-1")
        self.complete_job(
            "PROJ-1",
            terminal_state="recovery_ready",
            retry_timer={
                "timer_id": "timer:1",
                "timer_type": "retry",
                "generation": "generation-1",
                "due_at": "2026-10-01T00:05:00Z",
            },
        )
        fired = self.store.fire_timer(
            repository_id=REPOSITORY_ID,
            timer_id="timer:1",
            generation="generation-1",
            idempotency_key="timer:fire:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        self.assertFalse(fired.replayed)
        with self.assertRaises(StaleWriteError):
            self.store.fire_timer(
                repository_id=REPOSITORY_ID,
                timer_id="timer:1",
                generation="stale-generation",
                idempotency_key="timer:fire:stale",
                writer_identity=WRITER,
                occurred_at=NOW,
            )
        intended = self.store.intend_external_operation(
            repository_id=REPOSITORY_ID,
            sprint_id="65",
            ticket_id="PROJ-1",
            operation_key="github:merge:1",
            operation_type="github_merge",
            request_digest="request-sha",
            idempotency_key="operation:intend:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        self.assertFalse(intended.replayed)
        settled = self.store.settle_external_operation(
            repository_id=REPOSITORY_ID,
            operation_key="github:merge:1",
            request_digest="request-sha",
            expected_version=1,
            state="settled",
            receipt={"merge_sha": "abc"},
            idempotency_key="operation:settle:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        self.assertFalse(settled.replayed)
        settled_replay = self.store.settle_external_operation(
            repository_id=REPOSITORY_ID,
            operation_key="github:merge:1",
            request_digest="request-sha",
            expected_version=1,
            state="settled",
            receipt={"merge_sha": "abc"},
            idempotency_key="operation:settle:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        timer_replay = self.store.fire_timer(
            repository_id=REPOSITORY_ID,
            timer_id="timer:1",
            generation="generation-1",
            idempotency_key="timer:fire:1",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        self.assertTrue(settled_replay.replayed)
        self.assertTrue(timer_replay.replayed)
        with self.assertRaises(IdempotencyConflict):
            self.store.settle_external_operation(
                repository_id=REPOSITORY_ID,
                operation_key="github:merge:1",
                request_digest="different",
                expected_version=2,
                state="settled",
                receipt={"merge_sha": "abc"},
                idempotency_key="operation:settle:2",
                writer_identity=WRITER,
            )

    def test_event_rows_are_immutable_and_journal_mode_is_explicit(self) -> None:
        self.create_job("PROJ-1")
        with sqlite3.connect(self.path) as database:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "events are immutable"):
                database.execute("UPDATE events SET event_type = 'changed'")
        status = self.store.status()
        self.assertEqual(status.journal_mode, "delete")
        self.assertIn("local filesystem", status.journal_reason)

        self.store.close()
        self.store = TransactionalEventStore(
            self.path, writer_identity=WRITER, local_filesystem=True
        )
        expected = "wal" if wal_reset_fix_available(sqlite3.sqlite_version) else "delete"
        self.assertEqual(self.store.status().journal_mode, expected)
        self.assertIn("WAL", self.store.status().journal_reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
