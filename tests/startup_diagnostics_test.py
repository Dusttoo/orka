#!/usr/bin/env python3
"""Falsifying tests for fail-closed startup diagnostic admission."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "scripts/sprint-supervisor.py"
CONTRACT = ROOT / "contracts/startup-diagnostic-v1.json"
sys.path.insert(0, str(ROOT / "scripts"))

from event_store import (  # noqa: E402
    EventStoreError,
    TransactionalEventStore,
    WriterAuthorityError,
    canonical_json,
)
from runtime_state import (  # noqa: E402
    initialize_repository_identity,
    materialize_policy_snapshot,
    repository_layout,
    resolve_canonical_policy,
)
from startup_diagnostics import (  # noqa: E402
    CHECK_ORDER,
    prepare_startup_admission,
    run_startup_diagnostics,
)
from state_migration import activate_runtime_cutover, import_legacy_state  # noqa: E402


class QuickCheckFailureConnection:
    def __init__(self, path: Path) -> None:
        self.database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.database.execute("PRAGMA query_only = ON")

    def execute(self, sql: str, *arguments: object) -> object:
        if sql.strip() == "PRAGMA quick_check(1)":
            return [("database disk image is malformed",)]
        return self.database.execute(sql, *arguments)

    def rollback(self) -> None:
        self.database.rollback()

    def close(self) -> None:
        self.database.close()


class StartupDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, repository: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
        )

    def repository(self, name: str, *, cutover: bool = True) -> Path:
        repository = self.base / name
        repository.mkdir()
        self.git(repository, "init", "-q", "-b", "main")
        self.git(repository, "config", "user.email", "test@example.com")
        self.git(repository, "config", "user.name", "Test")
        config = repository / ".orchestration/config.yaml"
        config.parent.mkdir()
        config.write_text(
            "integration_branch: main\n",
            encoding="utf-8",
        )
        self.git(repository, "add", ".")
        self.git(repository, "commit", "-q", "-m", "initial")
        if not cutover:
            return repository
        initialize_repository_identity(
            repository,
            policy_ref="refs/heads/main",
            policy_path=".orchestration/config.yaml",
        )
        checkpoint = repository / ".orchestration/.sprint-state/99.json"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "repository": str(repository),
                    "sprint": {"id": "99", "name": "Diagnostic"},
                    "tickets": {},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        import_legacy_state(repository)
        activate_runtime_cutover(repository)
        return repository

    @staticmethod
    def check(receipt: dict, check_id: str) -> dict:
        return next(item for item in receipt["checks"] if item["id"] == check_id)

    @staticmethod
    def runtime_snapshot(repository: Path) -> dict[str, tuple[int, bytes]]:
        root = repository_layout(repository).state_root
        return {
            str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    @staticmethod
    def runtime_artifact_snapshot(repository: Path) -> dict[str, tuple]:
        root = repository_layout(repository).state_root
        snapshot: dict[str, tuple] = {}
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            relative = str(path.relative_to(root))
            mode = metadata.st_mode
            if path.is_symlink():
                snapshot[relative] = ("symlink", mode, os.readlink(path))
            elif path.is_file():
                snapshot[relative] = ("file", mode, path.read_bytes())
            elif path.is_dir():
                snapshot[relative] = ("directory", mode)
        return snapshot

    @staticmethod
    def database(repository: Path) -> Path:
        return repository_layout(repository).state_root / "orka-state.sqlite3"

    @staticmethod
    def update_database(repository: Path, sql: str, parameters: tuple = ()) -> None:
        database = sqlite3.connect(StartupDiagnosticTests.database(repository))
        try:
            database.execute("PRAGMA foreign_keys = OFF")
            database.execute(sql, parameters)
            database.commit()
        finally:
            database.close()

    @staticmethod
    def downgrade_database_to_v3(repository: Path) -> None:
        """Recreate the exact adjacent 1.8.33 event-store boundary."""

        database = sqlite3.connect(StartupDiagnosticTests.database(repository))
        try:
            database.executescript("""
                DROP INDEX execution_backends_attempt;
                DROP TABLE execution_backends;
                DELETE FROM schema_migrations WHERE version = 4;
                UPDATE metadata SET value = '3' WHERE key = 'schema_version';
                """)
            database.commit()
        finally:
            database.close()

    def test_legacy_receipt_is_healthy_and_preserves_1x_state(self) -> None:
        repository = self.repository("legacy", cutover=False)
        before = {
            str(path.relative_to(repository)): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        first = run_startup_diagnostics(repository, plugin_root=ROOT)
        second = run_startup_diagnostics(repository, plugin_root=ROOT)
        after = {
            str(path.relative_to(repository)): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

        self.assertTrue(first["healthy"])
        self.assertEqual(first, second)
        self.assertEqual(first["mode"], "legacy")
        self.assertEqual(before, after)
        self.assertEqual([item["id"] for item in first["checks"]], list(CHECK_ORDER))

    def test_initialized_pre_cutover_is_deterministic_read_only_and_healthy(
        self,
    ) -> None:
        repository = self.repository("initialized-pre-cutover", cutover=False)
        initialize_repository_identity(
            repository,
            policy_ref="refs/heads/main",
            policy_path=".orchestration/config.yaml",
        )
        before = self.runtime_snapshot(repository)

        first = run_startup_diagnostics(repository, plugin_root=ROOT)
        second = run_startup_diagnostics(repository, plugin_root=ROOT)

        self.assertEqual(first, second)
        self.assertTrue(first["healthy"], first)
        self.assertEqual(first["mode"], "legacy")
        self.assertEqual(before, self.runtime_snapshot(repository))
        self.assertEqual(self.check(first, "repository_identity")["status"], "pass")
        self.assertEqual(self.check(first, "canonical_policy")["status"], "pass")
        for check_id in (
            "quick_check",
            "foreign_keys",
            "schema",
            "migration_ledger",
            "cutover",
            "minimum_version",
        ):
            self.assertEqual(self.check(first, check_id)["status"], "skipped")

    def test_deleted_cutover_marker_cannot_downgrade_an_active_store(self) -> None:
        repository = self.repository("deleted-cutover")
        (repository_layout(repository).state_root / "cutover.json").unlink()

        receipt = run_startup_diagnostics(repository, plugin_root=ROOT)

        self.assertFalse(receipt["healthy"])
        self.assertEqual(receipt["mode"], "transactional")
        self.assertEqual(
            self.check(receipt, "cutover")["reason_code"],
            "cutover_marker_invalid",
        )

    def test_machine_contract_matches_runtime_receipts(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(contract["contract_id"], "orka.startup-diagnostic")
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["check_order"], list(CHECK_ORDER))
        repository = self.repository("contract")
        receipt = run_startup_diagnostics(repository, plugin_root=ROOT)
        self.assertEqual(
            {item["reason_code"] for item in receipt["checks"]}
            - set(contract["reason_codes"]),
            set(),
        )

    def test_transactional_receipt_is_deterministic_sanitized_and_read_only(
        self,
    ) -> None:
        repository = self.repository("healthy")
        before = self.runtime_snapshot(repository)
        first = run_startup_diagnostics(repository, plugin_root=ROOT)
        second = run_startup_diagnostics(repository, plugin_root=ROOT)

        self.assertTrue(first["healthy"], first)
        self.assertEqual(first, second)
        self.assertEqual(before, self.runtime_snapshot(repository))
        self.assertEqual(
            [
                (item["id"], item["status"], item["reason_code"])
                for item in first["checks"]
            ],
            [(check_id, "pass", "ok") for check_id in CHECK_ORDER],
        )
        encoded = canonical_json(first)
        self.assertNotIn(str(repository), encoded)
        self.assertNotIn("integration_branch", encoded)
        self.assertEqual(len(first["receipt_digest"]), 64)

    def test_healthy_supervisor_retains_the_exact_admission_receipt(self) -> None:
        repository = self.repository("supervisor-receipt")

        def command(name: str, *extra: str) -> dict:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    name,
                    "--repo",
                    str(repository),
                    *extra,
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return json.loads(result.stdout)

        started = command("start")
        try:
            self.assertTrue(started["startup_diagnostic"]["healthy"])
            status = command("status")
            self.assertEqual(
                status["startup_diagnostic"], started["startup_diagnostic"]
            )
        finally:
            command("stop", "--request-id", "diagnostic-test-cleanup")

    def test_supervisor_upgrades_reviewed_v3_store_before_admission(self) -> None:
        repository = self.repository("supervisor-v3-upgrade")
        self.downgrade_database_to_v3(repository)
        before = self.runtime_snapshot(repository)

        admission = prepare_startup_admission(repository, plugin_root=ROOT)

        self.assertFalse(admission.receipt["healthy"])
        self.assertTrue(admission.receipt["upgrade_required"])
        self.assertEqual(
            self.check(admission.receipt, "schema")["status"], "upgrade_required"
        )
        self.assertEqual(
            self.check(admission.receipt, "migration_ledger")["status"],
            "upgrade_required",
        )
        self.assertEqual(before, self.runtime_snapshot(repository))

        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "start", "--repo", str(repository)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        started = json.loads(result.stdout)
        try:
            self.assertTrue(started["startup_diagnostic"]["healthy"])
            self.assertFalse(started["startup_diagnostic"]["upgrade_required"])
            self.assertEqual(
                started["startup_diagnostic"]["upgraded_from_schema_version"], 3
            )
            database = sqlite3.connect(self.database(repository))
            try:
                self.assertEqual(
                    database.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone(),
                    ("4",),
                )
                self.assertEqual(
                    database.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall(),
                    [(1,), (2,), (3,), (4,)],
                )
            finally:
                database.close()
        finally:
            subprocess.run(
                [
                    sys.executable,
                    str(SUPERVISOR),
                    "stop",
                    "--repo",
                    str(repository),
                    "--request-id",
                    "v3-upgrade-cleanup",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )

    def test_each_fault_has_a_stable_blocking_reason(self) -> None:
        def bad_permissions(repository: Path) -> None:
            os.chmod(self.database(repository), 0o644)

        def bad_foreign_key(repository: Path) -> None:
            self.update_database(
                repository,
                """
                INSERT INTO events(
                    event_id, repository_id, aggregate_type, aggregate_id,
                    aggregate_version, event_type, idempotency_key,
                    payload_json, occurred_at
                ) VALUES ('bad-event', 'missing-repository', 'job', 'bad', 1,
                          'bad', 'bad-event', '{}', '2026-01-01T00:00:00Z')
                """,
            )

        def bad_schema(repository: Path) -> None:
            self.update_database(repository, "DROP INDEX events_aggregate_version")

        def bad_migration(repository: Path) -> None:
            self.update_database(
                repository,
                "UPDATE schema_migrations SET source_digest = 'bad' WHERE version = 2",
            )

        def bad_identity(repository: Path) -> None:
            self.update_database(
                repository,
                "UPDATE repositories SET object_directory_id = 'wrong'",
            )

        def bad_policy(repository: Path) -> None:
            self.update_database(
                repository,
                "UPDATE repositories SET policy_digest = 'wrong'",
            )

        def bad_cutover(repository: Path) -> None:
            self.update_database(
                repository,
                "UPDATE runtime_cutovers SET marker_digest = 'wrong'",
            )

        cases: list[tuple[str, Callable[[Path], None], str, str]] = [
            (
                "permissions",
                bad_permissions,
                "runtime_permissions",
                "database_permissions_invalid",
            ),
            ("foreign-key", bad_foreign_key, "foreign_keys", "foreign_key_violation"),
            ("schema", bad_schema, "schema", "schema_mismatch"),
            (
                "migration",
                bad_migration,
                "migration_ledger",
                "migration_ledger_mismatch",
            ),
            (
                "identity",
                bad_identity,
                "repository_identity",
                "repository_binding_mismatch",
            ),
            (
                "policy",
                bad_policy,
                "canonical_policy",
                "canonical_policy_binding_mismatch",
            ),
            ("cutover", bad_cutover, "cutover", "cutover_binding_mismatch"),
        ]
        for name, mutate, check_id, reason in cases:
            with self.subTest(name=name):
                repository = self.repository(name)
                mutate(repository)
                receipt = run_startup_diagnostics(repository, plugin_root=ROOT)
                self.assertFalse(receipt["healthy"], receipt)
                self.assertEqual(self.check(receipt, check_id)["reason_code"], reason)

        repository = self.repository("quick-check")
        receipt = run_startup_diagnostics(
            repository,
            plugin_root=ROOT,
            connection_factory=QuickCheckFailureConnection,
        )
        self.assertFalse(receipt["healthy"])
        self.assertEqual(
            self.check(receipt, "quick_check")["reason_code"], "quick_check_failed"
        )

    def test_minimum_version_fault_is_distinct_and_blocks(self) -> None:
        repository = self.repository("minimum-version")
        layout = repository_layout(repository)
        marker_path = layout.state_root / "cutover.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["minimum_orka_version"] = "999.0.0"
        material = {
            key: marker[key] for key in sorted(marker) if key != "activation_id"
        }
        marker["activation_id"] = (
            "cutover-"
            + hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
        )
        marker_path.write_text(canonical_json(marker) + "\n", encoding="utf-8")
        os.chmod(marker_path, 0o600)
        self.update_database(
            repository,
            """
            UPDATE runtime_cutovers
            SET activation_id = ?, marker_digest = ?, minimum_version = ?
            """,
            (
                marker["activation_id"],
                hashlib.sha256(canonical_json(marker).encode("utf-8")).hexdigest(),
                marker["minimum_orka_version"],
            ),
        )

        receipt = run_startup_diagnostics(repository, plugin_root=ROOT)
        self.assertFalse(receipt["healthy"])
        self.assertEqual(
            self.check(receipt, "minimum_version")["reason_code"],
            "minimum_version_not_met",
        )

    def test_checked_database_identity_rejects_a_path_swap(self) -> None:
        repository = self.repository("database-swap")
        admission = prepare_startup_admission(repository, plugin_root=ROOT)
        self.assertTrue(admission.receipt["healthy"])
        self.assertIsNotNone(admission.database_identity)
        database = self.database(repository)
        replacement = database.with_name("replacement.sqlite3")
        shutil.copy2(database, replacement)
        os.chmod(replacement, 0o600)
        os.replace(replacement, database)

        identity = admission.database_identity
        assert identity is not None
        with self.assertRaisesRegex(EventStoreError, "identity changed"):
            TransactionalEventStore(
                database,
                writer_identity="supervisor:test",
                expected_database_identity=(identity.device, identity.inode),
            )

    def test_writer_owned_revalidation_rejects_same_inode_authority_tampering(
        self,
    ) -> None:
        cases = [
            (
                "policy-row",
                "UPDATE repositories SET policy_digest = 'tampered'",
            ),
            (
                "cutover-row",
                "UPDATE runtime_cutovers SET marker_digest = 'tampered'",
            ),
            (
                "schema",
                "DROP INDEX events_aggregate_version",
            ),
            (
                "migration-ledger",
                "UPDATE schema_migrations SET source_digest = 'tampered' WHERE version = 2",
            ),
        ]
        for name, sql in cases:
            with self.subTest(name=name):
                repository = self.repository(f"writer-revalidation-{name}")
                admission = prepare_startup_admission(repository, plugin_root=ROOT)
                self.assertTrue(admission.receipt["healthy"])
                identity = admission.database_identity
                assert identity is not None
                database_path = self.database(repository)
                inode = database_path.stat().st_ino
                self.update_database(repository, sql)
                self.assertEqual(database_path.stat().st_ino, inode)

                def dump() -> str:
                    connection = sqlite3.connect(
                        f"file:{database_path}?mode=ro", uri=True
                    )
                    try:
                        return "\n".join(connection.iterdump())
                    finally:
                        connection.close()

                before = dump()
                with self.assertRaisesRegex(EventStoreError, "authority changed"):
                    TransactionalEventStore(
                        database_path,
                        writer_identity="supervisor:test",
                        expected_database_identity=(identity.device, identity.inode),
                        startup_validator=admission.validate_writer_connection,
                    )
                self.assertEqual(dump(), before)

    def test_active_runtime_requires_an_unchanged_private_writer_lock(self) -> None:
        repository = self.repository("unsafe-existing-writer-lock")
        admission = prepare_startup_admission(repository, plugin_root=ROOT)
        identity = admission.database_identity
        assert identity is not None
        lock = Path(f"{self.database(repository)}.writer.lock")
        lock.write_text("existing-lock", encoding="utf-8")
        os.chmod(lock, 0o644)
        before = self.runtime_artifact_snapshot(repository)
        receipt = run_startup_diagnostics(repository, plugin_root=ROOT)

        self.assertFalse(receipt["healthy"])
        self.assertEqual(
            self.check(receipt, "runtime_permissions")["reason_code"],
            "database_permissions_invalid",
        )

        with self.assertRaises(WriterAuthorityError):
            TransactionalEventStore(
                self.database(repository),
                writer_identity="supervisor:test",
                expected_database_identity=(identity.device, identity.inode),
                startup_validator=admission.validate_writer_connection,
                require_existing_lock=True,
            )

        self.assertEqual(self.runtime_artifact_snapshot(repository), before)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        self.assertEqual(lock.read_text(encoding="utf-8"), "existing-lock")

        repository = self.repository("missing-active-writer-lock")
        lock = Path(f"{self.database(repository)}.writer.lock")
        lock.unlink()
        before = self.runtime_artifact_snapshot(repository)
        receipt = run_startup_diagnostics(repository, plugin_root=ROOT)

        self.assertFalse(receipt["healthy"])
        self.assertEqual(
            self.check(receipt, "runtime_permissions")["reason_code"],
            "database_permissions_invalid",
        )
        with self.assertRaises(WriterAuthorityError):
            TransactionalEventStore(
                self.database(repository),
                writer_identity="supervisor:test",
                require_existing_lock=True,
            )

        self.assertFalse(lock.exists() or lock.is_symlink())
        self.assertEqual(self.runtime_artifact_snapshot(repository), before)

    def test_writer_lock_symlink_is_never_followed_changed_or_removed(self) -> None:
        repository = self.repository("symlinked-writer-lock")
        admission = prepare_startup_admission(repository, plugin_root=ROOT)
        identity = admission.database_identity
        assert identity is not None
        lock = Path(f"{self.database(repository)}.writer.lock")
        lock.unlink()
        target = lock.with_name(lock.name + ".target")
        target.write_text("target", encoding="utf-8")
        os.chmod(target, 0o600)
        lock.symlink_to(target.name)
        before = self.runtime_artifact_snapshot(repository)

        with self.assertRaises(WriterAuthorityError):
            TransactionalEventStore(
                self.database(repository),
                writer_identity="supervisor:test",
                expected_database_identity=(identity.device, identity.inode),
                startup_validator=admission.validate_writer_connection,
                require_existing_lock=True,
            )

        self.assertEqual(self.runtime_artifact_snapshot(repository), before)

    def test_bootstrap_creates_one_durable_writer_lock(self) -> None:
        database_path = self.base / "bootstrap" / "state.sqlite3"
        database_path.parent.mkdir()
        lock = Path(f"{database_path}.writer.lock")

        with TransactionalEventStore(database_path, writer_identity="bootstrap:first"):
            self.assertTrue(lock.is_file() and not lock.is_symlink())
            first = lock.stat()
            self.assertEqual(first.st_mode & 0o777, 0o600)

        with TransactionalEventStore(database_path, writer_identity="bootstrap:second"):
            second = lock.stat()

        self.assertEqual((second.st_dev, second.st_ino), (first.st_dev, first.st_ino))
        self.assertEqual(second.st_mode & 0o777, 0o600)

    def test_failed_bootstrap_preserves_lock_inode_for_waiters(self) -> None:
        repository = self.repository("durable-writer-lock-handoff")
        admission = prepare_startup_admission(repository, plugin_root=ROOT)
        identity = admission.database_identity
        assert identity is not None
        database_path = self.database(repository)
        lock = Path(f"{database_path}.writer.lock")
        lock.unlink()
        runtime_before = self.runtime_artifact_snapshot(repository)
        database_before = (database_path.stat().st_mode, database_path.read_bytes())
        waiter: int | None = None
        opened_identity: tuple[int, int] | None = None

        def fail_with_waiter(database: sqlite3.Connection, checked_path: Path) -> None:
            nonlocal waiter, opened_identity
            del database, checked_path
            flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            waiter = os.open(lock, flags)
            metadata = os.fstat(waiter)
            opened_identity = (metadata.st_dev, metadata.st_ino)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise RuntimeError("injected validation failure")

        try:
            with self.assertRaisesRegex(EventStoreError, "authority changed"):
                TransactionalEventStore(
                    database_path,
                    writer_identity="bootstrap:failed",
                    expected_database_identity=(identity.device, identity.inode),
                    startup_validator=fail_with_waiter,
                )
            assert waiter is not None
            assert opened_identity is not None
            current = lock.lstat()
            self.assertEqual((current.st_dev, current.st_ino), opened_identity)
            fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(WriterAuthorityError):
                TransactionalEventStore(
                    database_path,
                    writer_identity="runtime:third",
                    expected_database_identity=(identity.device, identity.inode),
                    require_existing_lock=True,
                )
            current = lock.lstat()
            self.assertEqual((current.st_dev, current.st_ino), opened_identity)
            runtime_after = self.runtime_artifact_snapshot(repository)
            self.assertEqual(
                {
                    key: value
                    for key, value in runtime_after.items()
                    if key != lock.name
                },
                runtime_before,
            )
            self.assertEqual(
                (database_path.stat().st_mode, database_path.read_bytes()),
                database_before,
            )
        finally:
            if waiter is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(waiter, fcntl.LOCK_UN)
                os.close(waiter)

    def test_checked_policy_blob_survives_policy_ref_advance(self) -> None:
        repository = self.repository("policy-race")
        admission = prepare_startup_admission(repository, plugin_root=ROOT)
        self.assertTrue(admission.receipt["healthy"])
        checked = admission.policy
        assert checked is not None
        config = repository / ".orchestration/config.yaml"
        config.write_text("integration_branch: changed\n", encoding="utf-8")
        self.git(repository, "add", str(config.relative_to(repository)))
        self.git(repository, "commit", "-q", "-m", "advance policy")
        self.assertNotEqual(resolve_canonical_policy(repository).blob, checked.blob)

        materialized = materialize_policy_snapshot(repository, checked)

        self.assertEqual(materialized.read_bytes(), checked.content)
        self.assertEqual(
            hashlib.sha256(materialized.read_bytes()).hexdigest(), checked.digest
        )

    def test_runtime_path_faults_are_distinct_sanitized_and_stop_admission(
        self,
    ) -> None:
        def chmod_path(path_getter: Callable[[Path], Path], mode: int) -> Callable:
            def mutate(repository: Path) -> Callable[[], None]:
                path = path_getter(repository)
                original = path.stat().st_mode & 0o777
                os.chmod(path, mode)
                return lambda: os.chmod(path, original)

            return mutate

        def symlink_path(path_getter: Callable[[Path], Path]) -> Callable:
            def mutate(repository: Path) -> Callable[[], None]:
                path = path_getter(repository)
                saved = path.with_name(path.name + ".saved")
                path.rename(saved)
                path.symlink_to(saved.name)

                def restore() -> None:
                    path.unlink()
                    saved.rename(path)

                return restore

            return mutate

        def malformed(path_getter: Callable[[Path], Path]) -> Callable:
            def mutate(repository: Path) -> Callable[[], None]:
                path = path_getter(repository)
                original = path.read_bytes()
                path.write_text("{", encoding="utf-8")
                os.chmod(path, 0o600)
                return lambda: path.write_bytes(original)

            return mutate

        def unavailable_database(repository: Path) -> Callable[[], None]:
            path = self.database(repository)
            saved = path.with_name(path.name + ".saved")
            path.rename(saved)
            return lambda: saved.rename(path)

        def sidecar(repository: Path, *, symlink: bool) -> Callable[[], None]:
            path = Path(f"{self.database(repository)}-journal")
            if symlink:
                target = path.with_name(path.name + ".target")
                target.write_text("sidecar", encoding="utf-8")
                os.chmod(target, 0o600)
                path.symlink_to(target.name)

                def restore() -> None:
                    path.unlink()
                    target.unlink()

                return restore
            path.write_text("sidecar", encoding="utf-8")
            os.chmod(path, 0o644)
            return lambda: path.unlink()

        root = lambda repo: repository_layout(repo).state_root
        identity = lambda repo: root(repo) / "repository.json"
        marker = lambda repo: root(repo) / "cutover.json"
        cases = [
            (
                "state-root-mode",
                chmod_path(root, 0o755),
                "runtime_permissions",
                "runtime_path_permissions_invalid",
            ),
            (
                "state-root-symlink",
                symlink_path(root),
                "runtime_permissions",
                "runtime_path_permissions_invalid",
            ),
            (
                "identity-mode",
                chmod_path(identity, 0o644),
                "repository_identity",
                "repository_identity_invalid",
            ),
            (
                "identity-symlink",
                symlink_path(identity),
                "repository_identity",
                "repository_identity_invalid",
            ),
            (
                "identity-malformed",
                malformed(identity),
                "repository_identity",
                "repository_identity_invalid",
            ),
            (
                "cutover-mode",
                chmod_path(marker, 0o644),
                "cutover",
                "cutover_marker_invalid",
            ),
            (
                "cutover-symlink",
                symlink_path(marker),
                "cutover",
                "cutover_marker_invalid",
            ),
            (
                "cutover-malformed",
                malformed(marker),
                "cutover",
                "cutover_marker_invalid",
            ),
            (
                "database-unavailable",
                unavailable_database,
                "quick_check",
                "database_unavailable",
            ),
            (
                "sidecar-mode",
                lambda repo: sidecar(repo, symlink=False),
                "runtime_permissions",
                "database_permissions_invalid",
            ),
            (
                "sidecar-symlink",
                lambda repo: sidecar(repo, symlink=True),
                "runtime_permissions",
                "database_permissions_invalid",
            ),
        ]
        for name, mutate, check_id, reason in cases:
            with self.subTest(name=name):
                repository = self.repository(name)
                cleanup = mutate(repository)
                try:
                    receipt = run_startup_diagnostics(repository, plugin_root=ROOT)
                    handshake = self.base / f"{name}-handshake.json"
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SUPERVISOR),
                            "_run",
                            "--repo",
                            str(repository),
                            "--handshake",
                            str(handshake),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    response = json.loads(handshake.read_text(encoding="utf-8"))
                finally:
                    cleanup()
                self.assertFalse(receipt["healthy"], receipt)
                self.assertEqual(self.check(receipt, check_id)["reason_code"], reason)
                self.assertNotIn(str(repository), canonical_json(receipt))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(response["status"], "error")
                self.assertFalse(response["startup_diagnostic"]["healthy"])
                database = sqlite3.connect(self.database(repository))
                try:
                    self.assertEqual(
                        database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
                    )
                    self.assertEqual(
                        database.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
                        0,
                    )
                finally:
                    database.close()

    def test_invalid_authority_inputs_keep_distinct_reason_codes(self) -> None:
        def invalid_identity(repository: Path) -> None:
            path = repository_layout(repository).state_root / "repository.json"
            path.write_text("{}\n", encoding="utf-8")

        def invalid_policy(repository: Path) -> None:
            self.git(repository, "update-ref", "-d", "refs/heads/main")

        def invalid_cutover(repository: Path) -> None:
            path = repository_layout(repository).state_root / "cutover.json"
            path.write_text("{}\n", encoding="utf-8")

        def invalid_minimum(repository: Path) -> None:
            path = repository_layout(repository).state_root / "cutover.json"
            marker = json.loads(path.read_text(encoding="utf-8"))
            marker["minimum_orka_version"] = "not-a-version"
            material = {
                key: marker[key] for key in sorted(marker) if key != "activation_id"
            }
            marker["activation_id"] = (
                "cutover-"
                + hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
            )
            path.write_text(canonical_json(marker) + "\n", encoding="utf-8")
            self.update_database(
                repository,
                "UPDATE runtime_cutovers SET activation_id = ?, marker_digest = ?, minimum_version = ?",
                (
                    marker["activation_id"],
                    hashlib.sha256(canonical_json(marker).encode("utf-8")).hexdigest(),
                    marker["minimum_orka_version"],
                ),
            )

        cases = [
            (
                "identity-invalid",
                invalid_identity,
                "repository_identity",
                "repository_identity_invalid",
            ),
            (
                "policy-invalid",
                invalid_policy,
                "canonical_policy",
                "canonical_policy_invalid",
            ),
            ("cutover-invalid", invalid_cutover, "cutover", "cutover_marker_invalid"),
            (
                "minimum-invalid",
                invalid_minimum,
                "minimum_version",
                "minimum_version_invalid",
            ),
        ]
        for name, mutate, check_id, reason in cases:
            with self.subTest(name=name):
                repository = self.repository(name)
                mutate(repository)
                receipt = run_startup_diagnostics(repository, plugin_root=ROOT)
                self.assertFalse(receipt["healthy"], receipt)
                self.assertEqual(self.check(receipt, check_id)["reason_code"], reason)
                self.assertNotIn(str(repository), canonical_json(receipt))

    def test_failed_receipt_stops_before_planning_reservation_or_launch(self) -> None:
        repository = self.repository("admission")
        self.update_database(
            repository,
            "UPDATE schema_migrations SET source_digest = 'bad' WHERE version = 1",
        )
        database = self.database(repository)

        def database_dump() -> str:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                return "\n".join(connection.iterdump())
            finally:
                connection.close()

        before = database_dump()
        identity_before = (
            repository_layout(repository).state_root / "repository.json"
        ).read_bytes()
        marker_before = (
            repository_layout(repository).state_root / "cutover.json"
        ).read_bytes()
        handshake = self.base / "failed-handshake.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SUPERVISOR),
                "_run",
                "--repo",
                str(repository),
                "--handshake",
                str(handshake),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        response = json.loads(handshake.read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "error")
        self.assertFalse(response["startup_diagnostic"]["healthy"])
        self.assertEqual(database_dump(), before)
        self.assertEqual(
            (repository_layout(repository).state_root / "repository.json").read_bytes(),
            identity_before,
        )
        self.assertEqual(
            (repository_layout(repository).state_root / "cutover.json").read_bytes(),
            marker_before,
        )
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
