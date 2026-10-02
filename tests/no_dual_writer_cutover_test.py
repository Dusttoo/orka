#!/usr/bin/env python3
"""End-to-end qualification for the Orka 2 transactional cutover."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / "scripts/sprint-supervisor.py"
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_state import (  # noqa: E402
    RuntimeStateError,
    assert_cutover_runtime_compatible,
    initialize_repository_identity,
    repository_layout,
)
from authoritative_supervisor_state import read_authoritative_state  # noqa: E402
from state_migration import (  # noqa: E402
    MigrationError,
    activate_runtime_cutover,
    import_legacy_state,
    rollback_runtime_cutover,
    runtime_cutover_status,
)


class NoDualWriterCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
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
                    "sprint": {"id": "65", "name": "Qualification"},
                    "tickets": {},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        try:
            status = self.supervisor("status", check=False)
            if status.returncode == 0:
                value = json.loads(status.stdout)
                if value.get("lifecycle_state") != "stopped":
                    self.supervisor("stop", "--request-id", "cleanup", check=False)
        finally:
            self.temporary.cleanup()

    def git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments], cwd=self.repo, check=True, capture_output=True
        )

    def supervisor(
        self, command: str, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                sys.executable,
                str(SUPERVISOR),
                command,
                "--repo",
                str(self.repo),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def value(self, command: str, *arguments: str) -> dict:
        return json.loads(self.supervisor(command, *arguments).stdout)

    def qualify_legacy_checkpoint(self) -> tuple[Path, bytes, Path, bytes]:
        self.value("start")
        self.value("pause", "--request-id", "legacy-pause")
        self.value("resume", "--request-id", "legacy-resume")
        self.value("stop", "--request-id", "legacy-stop")
        controller = self.repo / ".orchestration/.sprint-state/65.json"
        supervisor = self.repo / ".orchestration/.supervisor/state.json"
        return controller, controller.read_bytes(), supervisor, supervisor.read_bytes()

    def activate(self) -> dict:
        import_legacy_state(self.repo)
        return activate_runtime_cutover(self.repo)

    def test_running_legacy_supervisor_blocks_activation_without_a_marker(self) -> None:
        import_legacy_state(self.repo)
        self.value("start")
        with self.assertRaisesRegex(MigrationError, "supervisor lease is held"):
            activate_runtime_cutover(self.repo)
        self.assertFalse(runtime_cutover_status(self.repo)["active"])
        self.value("stop", "--request-id", "legacy-stop")

    def test_old_runtime_refuses_before_touching_transactional_or_legacy_state(self) -> None:
        controller, controller_bytes, supervisor, supervisor_bytes = (
            self.qualify_legacy_checkpoint()
        )
        self.activate()
        layout = repository_layout(self.repo)
        database = layout.state_root / "orka-state.sqlite3"
        before = database.read_bytes()
        old_plugin = self.base / "orka-old"
        for manifest in (
            old_plugin / ".claude-plugin/plugin.json",
            old_plugin / ".codex-plugin/plugin.json",
        ):
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text('{"version":"1.8.30"}\n', encoding="utf-8")

        with self.assertRaisesRegex(RuntimeStateError, "below transactional cutover minimum"):
            assert_cutover_runtime_compatible(self.repo, plugin_root=old_plugin)

        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(controller.read_bytes(), controller_bytes)
        self.assertEqual(supervisor.read_bytes(), supervisor_bytes)

    def test_cutover_pause_resume_status_restart_and_recovery_are_database_only(self) -> None:
        controller, controller_bytes, supervisor, supervisor_bytes = (
            self.qualify_legacy_checkpoint()
        )
        self.activate()

        started = self.value("start")
        self.assertEqual(started["lifecycle_state"], "active")
        generation = started["lease_generation"]
        paused = self.value("pause", "--request-id", "cutover-pause")
        self.assertEqual(paused["lifecycle_state"], "paused")
        self.assertEqual(self.value("status")["lifecycle_state"], "paused")
        resumed = self.value("resume", "--request-id", "cutover-resume")
        self.assertEqual(resumed["lifecycle_state"], "active")

        pid = int(resumed["process"]["pid"])
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        restarted = self.value("start")
        self.assertEqual(restarted["lease_generation"], generation + 1)
        durable = read_authoritative_state(self.repo)
        self.assertIsNotNone(durable)
        events = [entry["event"] for entry in durable["history"]]
        self.assertIn("operator_paused", events)
        self.assertIn("operator_resumed", events)
        self.assertEqual(durable["lease"]["generation"], generation + 1)
        self.value("stop", "--request-id", "cutover-stop")

        self.assertEqual(controller.read_bytes(), controller_bytes)
        self.assertEqual(supervisor.read_bytes(), supervisor_bytes)
        with self.assertRaisesRegex(MigrationError, "first authoritative write"):
            rollback_runtime_cutover(self.repo, reason="too late")


if __name__ == "__main__":
    unittest.main(verbosity=2)
