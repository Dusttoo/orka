#!/usr/bin/env python3
"""Falsifying tests for fail-closed startup diagnostic admission."""

from __future__ import annotations

import hashlib
import json
import os
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

from event_store import canonical_json  # noqa: E402
from runtime_state import (  # noqa: E402
    initialize_repository_identity,
    repository_layout,
)
from startup_diagnostics import CHECK_ORDER, run_startup_diagnostics  # noqa: E402
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
