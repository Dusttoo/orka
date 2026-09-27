#!/usr/bin/env python3
"""Integration coverage for the host-owned supervisor lifecycle slice."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / "scripts/sprint-supervisor.py"


class SupervisorProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="orka-supervisor-test-"))
        self.repositories: list[Path] = []

    def tearDown(self) -> None:
        for repository in self.repositories:
            state = self.read_state(repository, required=False)
            if state and self.process_live(state):
                self.run_cli(
                    "stop",
                    repository,
                    "--request-id",
                    "test-cleanup",
                    "--reason",
                    "test cleanup",
                    check=False,
                )
                self.wait_for(lambda: not self.process_live(state), timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def repository(self, *, with_config: bool = True) -> Path:
        repository = self.temp / f"repo-{len(self.repositories)}"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        if with_config:
            config = repository / ".orchestration/config.yaml"
            config.parent.mkdir()
            config.write_text(
                "schema_version: 1\nconcurrency_max: 2\n", encoding="utf-8"
            )
        self.repositories.append(repository)
        return repository

    def run_cli(
        self,
        command: str,
        repository: Path,
        *extra: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "python3",
                str(SUPERVISOR),
                command,
                "--repo",
                str(repository),
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            self.fail(
                f"{command} failed ({result.returncode}): {result.stderr or result.stdout}"
            )
        return result

    def output(self, result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    def read_state(self, repository: Path, *, required: bool = True) -> dict | None:
        path = repository / ".orchestration/.supervisor/state.json"
        if not path.exists():
            if required:
                self.fail(f"missing supervisor state: {path}")
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def process_live(self, state: dict) -> bool:
        identity = state.get("process") or {}
        pid = identity.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_for(self, predicate, *, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("condition did not become true before timeout")

    def test_start_detaches_and_duplicate_start_is_rejected(self) -> None:
        repository = self.repository()
        started = self.output(self.run_cli("start", repository))
        self.assertEqual(started["lifecycle_state"], "active")
        state = self.read_state(repository)
        self.assertNotEqual(state["process"]["session_id"], os.getsid(0))
        self.assertTrue(self.process_live(state))

        duplicate = self.run_cli("start", repository, check=False)
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("lease is held", duplicate.stderr)
        self.assertTrue(self.process_live(state))

    def test_pause_resume_drain_stop_and_clean_restart(self) -> None:
        repository = self.repository()
        first = self.output(self.run_cli("start", repository))
        first_pid = first["process"]["pid"]

        paused = self.output(
            self.run_cli("pause", repository, "--request-id", "pause-1")
        )
        self.assertEqual(paused["lifecycle_state"], "paused")
        replayed = self.output(
            self.run_cli("pause", repository, "--request-id", "pause-1")
        )
        self.assertEqual(replayed, paused)

        conflict = self.run_cli(
            "resume", repository, "--request-id", "pause-1", check=False
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("another command", conflict.stderr)

        resumed = self.output(
            self.run_cli("resume", repository, "--request-id", "resume-1")
        )
        self.assertEqual(resumed["lifecycle_state"], "active")
        drained = self.output(
            self.run_cli("drain", repository, "--request-id", "drain-1")
        )
        self.assertEqual(drained["lifecycle_state"], "paused")

        stopped = self.output(
            self.run_cli(
                "stop",
                repository,
                "--request-id",
                "stop-1",
                "--reason",
                "operator requested maintenance",
            )
        )
        self.assertEqual(stopped["lifecycle_state"], "stopped")
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        self.wait_for(lambda: not self.process_live({"process": {"pid": first_pid}}))
        state = self.read_state(repository)
        self.assertEqual(state["lease"]["release_count"], 1)
        self.assertTrue(state["lease"]["released_at"])
        events = [item["event"] for item in state["history"]]
        self.assertIn("operator_paused", events)
        self.assertIn("operator_resumed", events)
        self.assertIn("drain_requested", events)
        self.assertIn("drain_completed", events)
        self.assertIn("operator_stopped", events)
        self.assertEqual(events.count("lease_released"), 1)

        second = self.output(self.run_cli("start", repository))
        self.assertEqual(second["lease_generation"], 2)
        self.assertNotEqual(second["lease_id"], first["lease_id"])
        restarted = self.read_state(repository)
        self.assertEqual(
            [item["event"] for item in restarted["history"]].count("lease_released"),
            1,
        )

    def test_status_identifies_process_and_lease_without_secrets(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        status = self.output(self.run_cli("status", repository))
        self.assertEqual(status["lifecycle_state"], "active")
        self.assertEqual(status["process_status"], "live")
        self.assertEqual(status["lease_generation"], 1)
        self.assertRegex(status["lease_id"], r"^[0-9a-f-]{36}$")
        encoded = json.dumps(status).casefold()
        self.assertNotIn("token", encoded)
        self.assertNotIn("password", encoded)
        self.assertNotIn("credential", encoded)

    def test_replacing_the_lease_inode_stops_fail_closed(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        state = self.read_state(repository)
        lease_path = Path(state["lease"]["lock_path"])
        lease_path.unlink()
        lease_path.write_text("replacement\n", encoding="utf-8")

        self.wait_for(
            lambda: self.read_state(repository)["lifecycle_state"] == "stopped"
        )
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        final = self.read_state(repository)
        self.assertEqual(final["last_event"], "lease_lost")
        self.wait_for(lambda: not self.process_live(final))
        self.assertFalse(self.process_live(final))

    def test_external_state_mutation_stops_fail_closed(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        state_path = repository / ".orchestration/.supervisor/state.json"
        state = self.read_state(repository)
        state["updated_at"] = "forged"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        self.wait_for(
            lambda: self.read_state(repository)["lifecycle_state"] == "stopped"
        )
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        final = self.read_state(repository)
        self.assertEqual(final["last_event"], "durable_state_invalid")
        self.wait_for(lambda: not self.process_live(final))
        self.assertFalse(self.process_live(final))

    def test_missing_config_records_failed_preflight_and_releases_once(self) -> None:
        repository = self.repository(with_config=False)
        result = self.run_cli("start", repository, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("config is missing", result.stderr)
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        state = self.read_state(repository)
        self.assertEqual(state["lifecycle_state"], "stopped")
        self.assertIn("preflight_failed", [item["event"] for item in state["history"]])
        self.assertEqual(state["lease"]["release_count"], 1)
        self.assertFalse(self.process_live(state))

    def test_unclean_process_death_requires_future_takeover_authority(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        state = self.read_state(repository)
        os.kill(state["process"]["pid"], signal.SIGKILL)
        self.wait_for(lambda: not self.process_live(state))

        replacement = self.run_cli("start", repository, check=False)
        self.assertEqual(replacement.returncode, 2)
        self.assertIn("takeover authority", replacement.stderr)
        unchanged = self.read_state(repository)
        self.assertEqual(unchanged["lease"]["id"], state["lease"]["id"])
        self.assertEqual(unchanged["lifecycle_state"], "active")


if __name__ == "__main__":
    unittest.main(verbosity=2)
