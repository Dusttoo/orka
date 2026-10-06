#!/usr/bin/env python3
"""Restart, concurrency, and authority tests for supervisor state cutover."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "scripts/sprint-supervisor.py"
sys.path.insert(0, str(ROOT / "scripts"))

from authoritative_supervisor_state import (  # noqa: E402
    AuthoritativeSupervisorState,
    database_path,
    read_authoritative_state,
)
from event_store import (  # noqa: E402
    EventStoreError,
    TransactionalEventStore,
    WriterAuthorityError,
)
from runtime_state import initialize_repository_identity, repository_identity  # noqa: E402
from state_migration import activate_runtime_cutover, import_legacy_state  # noqa: E402

SUPERVISOR_SPEC = importlib.util.spec_from_file_location(
    "authority_test_sprint_supervisor", SUPERVISOR
)
sprint_supervisor = importlib.util.module_from_spec(SUPERVISOR_SPEC)
assert SUPERVISOR_SPEC.loader is not None
SUPERVISOR_SPEC.loader.exec_module(sprint_supervisor)


class AuthoritativeSupervisorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        config = self.repo / ".orchestration/config.yaml"
        config.parent.mkdir()
        config.write_text("integration_branch: main\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "initial")
        initialize_repository_identity(
            self.repo,
            policy_ref="refs/heads/main",
            policy_path=".orchestration/config.yaml",
        )
        checkpoint = self.repo / ".orchestration/.sprint-state/65.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": str(self.repo),
                    "sprint": {"id": "65", "name": "Sprint"},
                    "tickets": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.legacy_bytes = checkpoint.read_bytes()
        import_legacy_state(self.repo)
        activate_runtime_cutover(self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=self.repo, check=True, capture_output=True
        )

    def open_authority(
        self, fence: str
    ) -> tuple[TransactionalEventStore, AuthoritativeSupervisorState]:
        writer = f"supervisor:{repository_identity(self.repo)['repository_uuid']}"
        store = TransactionalEventStore(database_path(self.repo), writer_identity=writer)
        authority = AuthoritativeSupervisorState(
            self.repo,
            store,
            writer_identity=writer,
            supervisor_fence=fence,
        )
        return store, authority

    @staticmethod
    def state(sequence: int) -> dict:
        return {
            "schema_version": 2,
            "repository": "repository",
            "lifecycle_state": "active",
            "sequence": sequence,
            "dispatch": {
                "jobs": {
                    "run-1": {
                        "attempt_token": "attempt-1",
                        "claims": ["worktree:ticket-1"],
                        "timer": {"generation": "retry-1", "due_at": 42},
                    }
                }
            },
            "planning": {
                "decision_queue": [{"key": "TICKET-2", "class": "product"}],
                "external_operations": [
                    {"key": "github:pr:1", "state": "needs_reconcile"}
                ],
            },
        }

    def test_restart_restores_exact_state_without_rewriting_legacy_json(self) -> None:
        checkpoint = self.repo / ".orchestration/.sprint-state/65.json"
        first_store, first = self.open_authority("lease-1:1")
        expected = self.state(1)
        first.persist(expected)
        first_store.close()

        self.assertEqual(checkpoint.read_bytes(), self.legacy_bytes)
        self.assertEqual(read_authoritative_state(self.repo), expected)

        second_store, second = self.open_authority("lease-2:2")
        self.assertEqual(second.load(), expected)
        resumed = self.state(2)
        second.persist(resumed)
        second_store.close()
        self.assertEqual(read_authoritative_state(self.repo), resumed)
        self.assertEqual(checkpoint.read_bytes(), self.legacy_bytes)

    def test_concurrent_completions_serialize_as_whole_generations(self) -> None:
        store, authority = self.open_authority("lease-1:1")
        failures: list[BaseException] = []

        def commit(index: int) -> None:
            try:
                authority.persist(self.state(index))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=commit, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(authority.generation, 12)
        restored = authority.load()
        self.assertIn(restored["sequence"], range(12))
        self.assertEqual(authority.generation, 12)
        store.close()

    def test_workers_cannot_open_a_second_writer_or_reuse_closed_authority(self) -> None:
        store, authority = self.open_authority("lease-1:1")
        with self.assertRaisesRegex(
            WriterAuthorityError, "another supervisor already owns"
        ):
            TransactionalEventStore(
                database_path(self.repo), writer_identity="worker:attempt-1"
            )
        authority.persist(self.state(1))
        store.close()
        with self.assertRaisesRegex(EventStoreError, "closed"):
            authority.persist(self.state(2))

    def test_execution_migration_is_bound_to_real_source_and_target_generations(self) -> None:
        store, authority = self.open_authority("lease-1:1")
        source = {
            "schema_version": 2,
            "repository": str(self.repo),
            "runtime_fingerprint": "runtime-old",
            "contract_digest": "contract-old",
            "planning": {},
            "dispatch": {
                "jobs": {
                    "run-old": {
                        "ticket": "PNP-1",
                        "sprint": "65",
                        "run_ref": "run-old",
                        "attempt_token": "attempt-old",
                        "state": "running",
                        "phase_execution": {
                            "schema_version": "orka.phase-execution-state/v1"
                        },
                        "execution_identity": {"invocation_id": "invocation-old"},
                    }
                }
            },
        }
        authority.persist(source)
        expected = authority.load()

        migrated = sprint_supervisor.migrate_locked_authoritative_generation(
            authority,
            expected,
            target_runtime_fingerprint="runtime-new",
            target_contract_digest="contract-new",
        )

        receipt = migrated["execution_backend_migration"]
        self.assertTrue(authority.authenticate_migration_receipt(receipt))
        self.assertEqual(receipt["target_generation"], receipt["source_generation"] + 1)
        for field, changed in (
            ("activation_id", "forged-activation"),
            ("source_generation", receipt["source_generation"] + 3),
            ("source_payload_digest", "f" * 64),
            ("source_event_id", "e" * 64),
        ):
            with self.subTest(field=field):
                tampered = dict(receipt)
                tampered[field] = changed
                self.assertFalse(authority.authenticate_migration_receipt(tampered))

        store.close()

    def test_real_writer_cas_rejects_stale_migration_source(self) -> None:
        store, authority = self.open_authority("lease-1:1")
        source = {
            "schema_version": 2,
            "repository": str(self.repo),
            "runtime_fingerprint": "runtime-old",
            "contract_digest": "contract-old",
            "planning": {},
            "dispatch": {"jobs": {}},
        }
        authority.persist(source)
        stale = authority.load()
        stale_generation = authority.generation
        authority.persist({**source, "history": [{"event": "newer-generation"}]})

        with self.assertRaisesRegex(
            sprint_supervisor.SupervisorError, "generation changed"
        ):
            sprint_supervisor.migrate_locked_authoritative_generation(
                authority,
                stale,
                target_runtime_fingerprint="runtime-new",
                target_contract_digest="contract-new",
            )

        self.assertEqual(authority.generation, stale_generation + 1)
        store.close()

    def test_cutover_supervisor_uses_database_for_control_and_restart(self) -> None:
        def run(command: str, *extra: str) -> dict:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    command,
                    "--repo",
                    str(self.repo),
                    *extra,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return json.loads(result.stdout)

        first = run("start")
        self.assertEqual(first["lifecycle_state"], "active")
        self.assertFalse((self.repo / ".orchestration/.supervisor/state.json").exists())
        status = run("status")
        self.assertEqual(status["lease_generation"], 1)
        paused = run("pause", "--request-id", "pause-1")
        self.assertEqual(paused["lifecycle_state"], "paused")
        stopped = run("stop", "--request-id", "stop-1")
        self.assertEqual(stopped["lifecycle_state"], "stopped")
        self.assertFalse((self.repo / ".orchestration/.supervisor/state.json").exists())
        second = run("start")
        self.assertEqual(second["lease_generation"], 2)
        run("stop", "--request-id", "stop-2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
