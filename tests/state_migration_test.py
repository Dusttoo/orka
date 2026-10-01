#!/usr/bin/env python3
"""Legacy import, deterministic export, and crash-boundary tests."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_store import TransactionalEventStore  # noqa: E402
from runtime_state import (  # noqa: E402
    RuntimeStateError,
    assert_legacy_runtime_writable,
    initialize_repository_identity,
    repository_identity,
    runtime_cutover_marker,
)
from state_migration import (  # noqa: E402
    DATABASE_NAME,
    MigrationError,
    activate_runtime_cutover,
    export_bytes,
    export_state,
    import_legacy_state,
    inventory_legacy_state,
    rollback_runtime_cutover,
    runtime_cutover_status,
)


class LegacyStateMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.make_repository(self.base / "repo")
        self.write_legacy_state(self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def make_repository(self, path: Path) -> Path:
        path.mkdir()
        self.git("init", "-q", "-b", "main", cwd=path)
        self.git("config", "user.email", "test@example.com", cwd=path)
        self.git("config", "user.name", "Test", cwd=path)
        config = path / ".orchestration/config.yaml"
        config.parent.mkdir()
        config.write_text("integration_branch: main\n", encoding="utf-8")
        self.git("add", ".", cwd=path)
        self.git("commit", "-q", "-m", "initial", cwd=path)
        initialize_repository_identity(
            path,
            policy_ref="refs/heads/main",
            policy_path=".orchestration/config.yaml",
        )
        return path

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def write_legacy_state(self, repository: Path) -> None:
        runtime = repository / ".orchestration"
        self.write_json(
            runtime / ".sprint-state/65.json",
            {
                "repository": str(repository),
                "sprint": {"id": "65", "name": "Sprint 65"},
                "tickets": {
                    "PROJ-1": {
                        "attempt_token": "attempt-PROJ-1-1",
                        "branch": "1-ticket",
                        "cost": "4.25",
                        "pr": "10",
                        "reservation": "reservation-1",
                        "state": "needs_repair",
                        "timer": {
                            "deadline": "2026-01-01T00:00:00Z",
                            "generation": 2,
                        },
                        "worktree": str(repository / ".worktrees/1-ticket"),
                    }
                },
                "prompt": "must not enter the event store export",
                "api_key": "sk-ant-secret-material",
            },
        )
        self.write_json(
            runtime / ".review-ledger/pr-10.json",
            {"findings": [{"component": "src/app.py:run", "status": "open"}]},
        )
        usage = runtime / ".api-usage/ledger.jsonl"
        usage.parent.mkdir(parents=True)
        usage.write_text(
            json.dumps(
                {
                    "kind": "reservation",
                    "reservation_id": "reservation-1",
                    "ticket": "PROJ-1",
                }
            )
            + "\n"
            + json.dumps({"kind": "usage", "cost": "4.25", "ticket": "PROJ-1"})
            + "\n",
            encoding="utf-8",
        )
        self.write_json(
            runtime / ".provider-health/openai.json",
            {"route": "openai", "state": "healthy"},
        )
        self.write_json(
            runtime / ".decisions/PROJ-1.json",
            {"decision": "repair", "ticket": "PROJ-1"},
        )
        self.write_json(
            runtime / ".recovery/PROJ-1.json",
            {"receipt": "absence-proof", "ticket": "PROJ-1"},
        )
        self.write_json(
            runtime / ".api-runs/PROJ-1.json",
            {"operation": "review", "receipt": "provider-receipt-1"},
        )
        supervisor = runtime / ".supervisor"
        self.write_json(
            supervisor / "state.json",
            {
                "lifecycle_state": "stopped",
                "lease": {
                    "release_count": 1,
                    "released_at": "2026-01-01T00:00:00Z",
                },
            },
        )
        (supervisor / "lease.lock").touch()

    def database_path(self, repository: Path | None = None) -> Path:
        target = repository or self.repo
        return target / ".git/orka-runtime" / DATABASE_NAME

    def test_inventory_hashes_and_sanitizes_every_known_source(self) -> None:
        inventory = inventory_legacy_state(self.repo)
        self.assertEqual(len(inventory["sources"]), 8)
        self.assertRegex(inventory["manifest_digest"], r"^[a-f0-9]{64}$")
        encoded = json.dumps(inventory, sort_keys=True)
        self.assertNotIn("must not enter", encoded)
        self.assertNotIn("sk-ant", encoded)
        self.assertIn("attempt-PROJ-1-1", encoded)
        self.assertEqual(
            {source["category"] for source in inventory["sources"]},
            {
                "controller",
                "decision",
                "external_receipt",
                "provider_health",
                "recovery",
                "review_ledger",
                "supervisor",
                "usage",
            },
        )

    def test_import_is_atomic_idempotent_and_export_is_byte_stable(self) -> None:
        first = import_legacy_state(self.repo)
        self.assertFalse(first["replayed"])
        path = self.database_path()
        self.assertTrue(path.is_file())
        first_export = export_bytes(path)
        self.assertEqual(first_export, export_bytes(path))
        exported = export_state(path)
        inventory = inventory_legacy_state(self.repo)
        self.assertEqual(
            exported["digests"]["legacy_snapshot"],
            inventory["normalized_export_digest"],
        )
        self.assertEqual(exported["schema_version"], 3)
        self.assertNotIn("must not enter", first_export.decode())
        self.assertNotIn("sk-ant", first_export.decode())
        self.assertIn("attempt-PROJ-1-1", first_export.decode())

        second = import_legacy_state(self.repo)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(first_export, export_bytes(path))

    def test_changed_source_cannot_reuse_or_replace_receipt(self) -> None:
        import_legacy_state(self.repo)
        path = self.database_path()
        before = path.read_bytes()
        decision = self.repo / ".orchestration/.decisions/PROJ-1.json"
        self.write_json(decision, {"decision": "redesign", "ticket": "PROJ-1"})
        with self.assertRaisesRegex(MigrationError, "different legacy migration receipt"):
            import_legacy_state(self.repo)
        self.assertEqual(path.read_bytes(), before)

    def test_held_supervisor_lease_blocks_import(self) -> None:
        supervisor = self.repo / ".orchestration/.supervisor"
        supervisor.mkdir(parents=True, exist_ok=True)
        lease = (supervisor / "lease.lock").open("a+")
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(MigrationError, "supervisor lease is held"):
                import_legacy_state(self.repo)
        finally:
            fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
            lease.close()
        self.assertFalse(self.database_path().exists())

    def test_every_install_boundary_is_replay_safe(self) -> None:
        boundaries = (
            "after_inventory",
            "before_temporary_database",
            "after_temporary_database",
            "after_import_transaction",
            "after_integrity_check",
            "before_install",
            "after_install",
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                repository = self.make_repository(self.base / f"repo-{boundary}")
                self.write_legacy_state(repository)

                def crash(current: str) -> None:
                    if current == boundary:
                        raise RuntimeError(f"crash:{boundary}")

                with self.assertRaisesRegex(RuntimeError, f"crash:{boundary}"):
                    import_legacy_state(repository, crash_hook=crash)
                destination = self.database_path(repository)
                if boundary == "after_install":
                    self.assertTrue(destination.is_file())
                    self.assertTrue(import_legacy_state(repository)["replayed"])
                else:
                    self.assertFalse(destination.exists())
                self.assertEqual(
                    list(destination.parent.glob(f".{DATABASE_NAME}.*.tmp")), []
                )

    def test_cutover_activation_is_idempotent_and_blocks_legacy_writers(self) -> None:
        import_legacy_state(self.repo)
        first = activate_runtime_cutover(self.repo)
        self.assertFalse(first["replayed"])
        self.assertEqual(first["seeded_documents"], 2)
        marker = runtime_cutover_marker(self.repo)
        self.assertEqual(marker["activation_id"], first["activation_id"])
        status = runtime_cutover_status(self.repo)
        self.assertTrue(status["active"])
        with self.assertRaisesRegex(RuntimeStateError, "legacy JSON runtime is read-only"):
            assert_legacy_runtime_writable(self.repo)
        older_plugin = self.base / "older-orka"
        self.write_json(
            older_plugin / ".claude-plugin/plugin.json", {"version": "1.8.23"}
        )
        self.write_json(
            older_plugin / ".codex-plugin/plugin.json", {"version": "1.8.23"}
        )
        with self.assertRaisesRegex(
            RuntimeStateError, "below transactional cutover minimum"
        ):
            assert_legacy_runtime_writable(self.repo, plugin_root=older_plugin)
        controller = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/sprint-controller.py"),
                "summary",
                "--sprint",
                "65",
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(controller.returncode, 2, controller.stderr)
        self.assertIn("active supervisor has no valid lease fence", controller.stderr)
        handshake = self.base / "cutover-supervisor-handshake.json"
        supervisor = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/sprint-supervisor.py"),
                "_run",
                "--repo",
                str(self.repo),
                "--handshake",
                str(handshake),
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(supervisor.returncode, 2)
        self.assertIn(
            "unsupported supervisor state schema",
            json.loads(handshake.read_text(encoding="utf-8"))["error"],
        )

        second = activate_runtime_cutover(self.repo)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["activation_id"], second["activation_id"])

    def test_cutover_rollback_is_replay_safe_before_first_write(self) -> None:
        for boundary in ("after_store_rollback", "after_marker_removal"):
            with self.subTest(boundary=boundary):
                repository = self.make_repository(self.base / f"rollback-{boundary}")
                self.write_legacy_state(repository)
                import_legacy_state(repository)
                activate_runtime_cutover(repository)

                def crash(current: str) -> None:
                    if current == boundary:
                        raise RuntimeError(f"crash:{boundary}")

                with self.assertRaisesRegex(RuntimeError, f"crash:{boundary}"):
                    rollback_runtime_cutover(
                        repository,
                        reason="pre-write validation failed",
                        crash_hook=crash,
                    )
                replay = rollback_runtime_cutover(
                    repository, reason="pre-write validation failed"
                )
                self.assertTrue(replay["replayed"])
                self.assertIsNone(runtime_cutover_marker(repository))
                self.assertFalse(runtime_cutover_status(repository)["active"])
                assert_legacy_runtime_writable(repository)
                with self.assertRaisesRegex(
                    MigrationError, "rolled back by another receipt"
                ):
                    rollback_runtime_cutover(
                        repository, reason="a different rollback receipt"
                    )

    def test_cutover_cannot_roll_back_after_authoritative_write(self) -> None:
        import_legacy_state(self.repo)
        activation = activate_runtime_cutover(self.repo)
        identity = repository_identity(self.repo)
        writer = "supervisor:test:fence"
        with TransactionalEventStore(
            self.database_path(), writer_identity=writer
        ) as store:
            snapshot = store.runtime_snapshot(
                repository_id=identity["repository_uuid"]
            )
            controller = next(
                item
                for item in snapshot["documents"]
                if item["document_type"] == "controller"
            )
            store.write_runtime_document(
                repository_id=identity["repository_uuid"],
                activation_id=activation["activation_id"],
                document_type="controller",
                document_id=controller["document_id"],
                expected_generation=controller["generation"],
                payload=controller["payload"],
                supervisor_fence="lease:1",
                idempotency_key="controller:first-authoritative-write",
                writer_identity=writer,
            )
        with self.assertRaisesRegex(MigrationError, "first authoritative write"):
            rollback_runtime_cutover(self.repo, reason="too late")

    def test_cutover_activation_crash_boundaries_replay(self) -> None:
        for boundary in ("after_marker_install", "after_store_activation"):
            with self.subTest(boundary=boundary):
                repository = self.make_repository(self.base / f"cutover-{boundary}")
                self.write_legacy_state(repository)
                import_legacy_state(repository)

                def crash(current: str) -> None:
                    if current == boundary:
                        raise RuntimeError(f"crash:{boundary}")

                with self.assertRaisesRegex(RuntimeError, f"crash:{boundary}"):
                    activate_runtime_cutover(repository, crash_hook=crash)
                resumed = activate_runtime_cutover(repository)
                self.assertTrue(runtime_cutover_status(repository)["active"])
                self.assertEqual(
                    resumed["replayed"], boundary == "after_store_activation"
                )

    def test_ambiguous_parent_state_requires_exact_identity_proof(self) -> None:
        source = self.make_repository(self.base / "bare-source")
        bare = self.base / "authority.git"
        self.git("clone", "-q", "--bare", str(source), str(bare), cwd=self.base)
        initialize_repository_identity(
            bare,
            policy_ref="refs/heads/main",
            policy_path=".orchestration/config.yaml",
        )
        parent_state = self.base / ".orchestration/.sprint-state/99.json"
        self.write_json(parent_state, {"tickets": {"PROJ-9": {"state": "pending"}}})
        with self.assertRaisesRegex(MigrationError, "ownership is ambiguous"):
            inventory_legacy_state(bare)

        identity = repository_identity(bare)
        self.write_json(
            self.base / ".orchestration/repository-binding.json",
            {
                "repository_uuid": identity["repository_uuid"],
                "common_directory": identity["common_directory"],
                "object_directory_identity": identity["object_directory_identity"],
            },
        )
        inventory = inventory_legacy_state(bare)
        self.assertEqual(len(inventory["sources"]), 1)
        self.assertTrue(inventory["sources"][0]["source_path"].startswith("identity-proof:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
