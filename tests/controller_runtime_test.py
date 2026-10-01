#!/usr/bin/env python3
"""Transactional controller routing and generation-fence tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from controller_runtime import (  # noqa: E402
    ControllerRuntimeError,
    execute_request,
    supervisor_request,
)
from event_store import TransactionalEventStore  # noqa: E402
from runtime_state import (  # noqa: E402
    initialize_repository_identity,
    repository_identity,
)
from state_migration import (  # noqa: E402
    DATABASE_NAME,
    activate_runtime_cutover,
    import_legacy_state,
)


class ControllerRuntimeTests(unittest.TestCase):
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
        self.checkpoint = self.repo / ".orchestration/.sprint-state/65.json"
        self.checkpoint.parent.mkdir(parents=True)
        self.payload = {
            "schema_version": 2,
            "repository": str(self.repo),
            "sprint": {"id": "65", "name": "Sprint 65"},
            "tickets": {},
        }
        self.checkpoint.write_text(json.dumps(self.payload) + "\n", encoding="utf-8")
        import_legacy_state(self.repo)
        self.activation = activate_runtime_cutover(self.repo)
        self.private_root = self.base / "private"
        self.controller = self.base / "fake-controller.py"
        self.controller.write_text(
            """#!/usr/bin/env python3
import argparse, json
from pathlib import Path
p = argparse.ArgumentParser(); p.add_argument('--state-dir', required=True)
p.add_argument('command'); p.add_argument('--sprint', required=True)
a = p.parse_args(); path = next(Path(a.state_dir).glob('*.json'))
value = json.loads(path.read_text()); value['command_count'] = value.get('command_count', 0) + 1
path.write_text(json.dumps(value) + '\\n'); print(json.dumps({'ticket_count': len(value['tickets'])}))
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def snapshot(self) -> dict:
        identity = repository_identity(self.repo)
        writer = "reader-for-test"
        with TransactionalEventStore(
            self.repo / ".git/orka-runtime" / DATABASE_NAME,
            writer_identity=writer,
        ) as store:
            return store.runtime_snapshot(repository_id=identity["repository_uuid"])

    def test_command_materializes_privately_and_commits_one_generation(self) -> None:
        fence = "lease-1:1"
        request = supervisor_request(
            self.repo,
            ["mutate", "--sprint", "65"],
            fence,
            command_id="command-1",
        )
        result = execute_request(
            self.repo,
            request,
            supervisor_fence=fence,
            writer_identity="supervisor:lease-1:1",
            private_root=self.private_root,
            controller_path=self.controller,
        )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["commits"][0]["generation"], 1)
        document = self.snapshot()["documents"][0]
        self.assertEqual(document["payload"]["command_count"], 1)
        self.assertEqual(json.loads(self.checkpoint.read_text()), self.payload)
        self.assertEqual(list(self.private_root.iterdir()), [])

    def test_exact_duplicate_replays_and_changed_material_fails_closed(self) -> None:
        fence = "lease-1:1"
        request = supervisor_request(
            self.repo,
            ["mutate", "--sprint", "65"],
            fence,
            command_id="command-duplicate",
        )
        first = execute_request(
            self.repo,
            request,
            supervisor_fence=fence,
            writer_identity="supervisor:lease-1:1",
            private_root=self.private_root,
            controller_path=self.controller,
        )
        replay = execute_request(
            self.repo,
            request,
            supervisor_fence=fence,
            writer_identity="supervisor:lease-1:1",
            private_root=self.private_root,
            controller_path=self.controller,
        )
        self.assertFalse(first["commits"][0]["replayed"])
        self.assertTrue(replay["commits"][0]["replayed"])
        changed = dict(request)
        changed["argv"] = ["different", "--sprint", "65"]
        with self.assertRaisesRegex(
            ControllerRuntimeError, "reused with different material"
        ):
            execute_request(
                self.repo,
                changed,
                supervisor_fence=fence,
                writer_identity="supervisor:lease-1:1",
                private_root=self.private_root,
                controller_path=self.controller,
            )

    def test_stale_repository_fence_and_generation_are_rejected(self) -> None:
        request = supervisor_request(
            self.repo,
            ["mutate", "--sprint", "65"],
            "lease-1:1",
            command_id="command-stale",
        )
        with self.assertRaisesRegex(
            ControllerRuntimeError, "supervisor fence is stale"
        ):
            execute_request(
                self.repo,
                request,
                supervisor_fence="lease-2:2",
                writer_identity="supervisor:lease-2:2",
                private_root=self.private_root,
                controller_path=self.controller,
            )
        stale = dict(request)
        stale["expected_controller_generations"] = {"65": 99}
        with self.assertRaisesRegex(ControllerRuntimeError, "generation is stale"):
            execute_request(
                self.repo,
                stale,
                supervisor_fence="lease-1:1",
                writer_identity="supervisor:lease-1:1",
                private_root=self.private_root,
                controller_path=self.controller,
            )

    def test_state_and_policy_path_overrides_fail_closed(self) -> None:
        for suffix in (
            ["--state-dir=/tmp/escape"],
            ["--state-dir", "/tmp/escape"],
            ["--config=other.yaml"],
            ["--config", "other.yaml"],
        ):
            with self.subTest(suffix=suffix):
                argv = ["mutate", "--sprint", "65", *suffix]
                request = supervisor_request(
                    self.repo,
                    argv,
                    "lease-1:1",
                    command_id=f"override-{len('-'.join(suffix))}",
                )
                with self.assertRaisesRegex(
                    ControllerRuntimeError,
                    "cannot override policy or materialized state",
                ):
                    execute_request(
                        self.repo,
                        request,
                        supervisor_fence="lease-1:1",
                        writer_identity="supervisor:lease-1:1",
                        private_root=self.private_root,
                        controller_path=self.controller,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
