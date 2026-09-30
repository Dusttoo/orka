#!/usr/bin/env python3
"""Recovery-specific crash and replay proofs with no external credentials."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import sprint_controller_resilience_test as resilience  # noqa: E402

controller = resilience.controller
REAL_SUBPROCESS_RUN = controller.subprocess.run


def read_only_git(command, *args, **kwargs):
    values = [str(item) for item in command]
    allowed = {"rev-parse", "status", "worktree"}
    if values and values[0] == "git" and any(item in allowed for item in values[1:]):
        return REAL_SUBPROCESS_RUN(command, *args, **kwargs)
    raise AssertionError(
        "recovery must not create Jira transitions, branches, worktrees, PRs, or merges"
    )


class RecoveryCrashBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = resilience.ResilienceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg = self.fixture.cfg
        self.state = self.fixture.state

    def _state_path(self) -> Path:
        return controller.state_path(self.cfg["state_dir"], "1")

    def _preserved_ticket(self) -> tuple[dict, dict, Path]:
        self.cfg["preserved_pr_auto_recovery"] = True
        ticket = self.fixture.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            pr="https://example/pr/123",
            verified_commits={"a" * 40: "b" * 40},
            progress=[
                {
                    "attempt": 2,
                    "milestone": "implementation_commit",
                    "verified": True,
                    "spent_usd": 1,
                }
            ],
        )
        self.fixture.stopped_execution(ticket)
        receipt = {
            "receipt": {
                "branch": "feature",
                "url": "https://example/pr/123",
                "head": "a" * 40,
                "tree": "b" * 40,
            }
        }
        worktree = self.cfg["shared_root"] / ".worktrees/PROJ-1"
        return ticket, receipt, worktree

    def _preserved_patches(self, receipt: dict, worktree: Path):
        stack = contextlib.ExitStack()
        stack.enter_context(patch("github_progress.observe", return_value=receipt))
        stack.enter_context(
            patch.object(controller, "branch_worktree", return_value=worktree)
        )
        stack.enter_context(
            patch.object(controller, "worktree_recovery_status", return_value="clean")
        )
        stack.enter_context(
            patch.object(
                controller,
                "worktree_revision",
                return_value={"head": "a" * 40, "tree": "b" * 40},
            )
        )
        stack.enter_context(
            patch.object(controller, "execution_unit_status", return_value="absent")
        )
        stack.enter_context(
            patch.object(controller, "usage_snapshots", return_value={"PROJ-1": {}})
        )
        stack.enter_context(
            patch.object(
                controller.subprocess,
                "run",
                side_effect=read_only_git,
            )
        )
        return stack

    def test_machine_readable_matrix_declares_every_recovery_boundary(self) -> None:
        value = json.loads(
            (ROOT / "contracts/recovery-crash-boundaries-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(value["schema"], "orka.recovery-crash-boundaries/v1")
        boundaries = value["boundaries"]
        self.assertEqual(
            [item["id"] for item in boundaries],
            [
                "eligibility_observation",
                "recovery_fencing",
                "controller_mutation",
                "worker_reservation",
                "worker_launch",
                "worker_attach",
                "provider_acknowledgement",
                "pr_observation",
                "terminal_application",
            ],
        )
        for item in boundaries:
            self.assertEqual(
                set(item),
                {"id", "durable_state", "next_action", "external_effect"},
            )
            self.assertTrue(all(str(value).strip() for value in item.values()))

    def test_recovery_fence_is_idempotent_and_conflicts_fail_closed(self) -> None:
        ledger = controller.UsageLedger(self.cfg["shared_root"])
        ledger.fence_recovery("PROJ-1", "recovery-same")
        ledger.fence_recovery("PROJ-1", "recovery-same")
        events = ledger.snapshot()
        self.assertEqual(
            len([item for item in events if item.get("kind") == "recovery_fence"]),
            1,
        )
        with self.assertRaisesRegex(
            controller.AgentError, "different active recovery fence"
        ):
            ledger.fence_recovery("PROJ-1", "recovery-conflict")

    def test_dead_worker_crash_before_checkpoint_replays_once(self) -> None:
        ticket = self.fixture.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            pr="",
            branch="feature",
            progress=[
                {
                    "attempt": 2,
                    "milestone": "implementation_commit",
                    "verified": True,
                    "spent_usd": 1,
                }
            ],
        )
        self.fixture.stopped_execution(ticket)
        path = self._state_path()
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            reason="resume after proved process death",
            attempt_token="token",
            operator_capability="",
        )
        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            patch.object(controller, "usage_snapshots", return_value={"PROJ-1": {}}),
            patch.object(
                controller.subprocess,
                "run",
                side_effect=read_only_git,
            ),
            patch.object(controller, "save", side_effect=OSError("injected crash")),
            self.assertRaisesRegex(OSError, "injected crash"),
        ):
            controller.requeue(args, self.cfg)
        self.assertEqual(
            controller.load(path)["tickets"]["PROJ-1"]["state"], "recoverable"
        )

        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            patch.object(controller, "usage_snapshots", return_value={"PROJ-1": {}}),
            patch.object(
                controller.subprocess,
                "run",
                side_effect=read_only_git,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.requeue(args, self.cfg)
        resumed = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(resumed["state"], "pending")
        self.assertEqual(resumed["attempts"], 2)
        transitions = [
            item for item in resumed["history"] if item.get("event") == "requeued"
        ]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(
            transitions[0]["transition_trace"],
            ["eligibility_observation:eligible", "controller_mutation:pending"],
        )
        with self.assertRaisesRegex(controller.SprintError, "from state pending"):
            controller.requeue(args, self.cfg)

    def test_preserved_pr_crash_after_fence_reuses_same_fence(self) -> None:
        ticket, receipt, worktree = self._preserved_ticket()
        path = self._state_path()
        controller.save(path, self.state)
        before = controller.recovery_preservation_snapshot(ticket, {}, self.cfg)
        args = argparse.Namespace(sprint="1", ticket="PROJ-1")
        with (
            self._preserved_patches(receipt, worktree),
            patch.object(controller, "save", side_effect=OSError("injected crash")),
            self.assertRaisesRegex(OSError, "injected crash"),
        ):
            controller.reconcile_preserved_pr(args, self.cfg)

        self.assertEqual(
            controller.load(path)["tickets"]["PROJ-1"]["state"], "recoverable"
        )
        ledger = controller.UsageLedger(self.cfg["shared_root"])
        first_events = ledger.snapshot()
        first_fence = ledger._active_recovery_fence(first_events, "PROJ-1")
        self.assertIsNotNone(first_fence)

        with (
            self._preserved_patches(receipt, worktree),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.reconcile_preserved_pr(args, self.cfg)
        resumed = controller.load(path)["tickets"]["PROJ-1"]
        after = controller.recovery_preservation_snapshot(resumed, {}, self.cfg)
        self.assertEqual(before, after)
        self.assertEqual(resumed["state"], "pending")
        self.assertEqual(
            resumed["recovery_binding"]["recovery_id"],
            first_fence["recovery_id"],
        )
        fence_events = [
            item for item in ledger.snapshot() if item.get("kind") == "recovery_fence"
        ]
        self.assertEqual(len(fence_events), 1)
        transitions = [
            item
            for item in resumed["history"]
            if item.get("event") == "preserved-pr-reconciled"
        ]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(
            transitions[0]["transition_trace"],
            [
                "eligibility_observation:eligible",
                "recovery_fencing:active",
                "controller_mutation:pending",
            ],
        )

        with self.assertRaisesRegex(controller.SprintError, "not eligible"):
            controller.reconcile_preserved_pr(args, self.cfg)

    def test_reserve_and_attach_replay_create_one_attempt_and_identity(self) -> None:
        self.fixture.ticket("PROJ-1", pr="", branch="")
        path = self._state_path()
        controller.save(path, self.state)
        reserve_args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            run_ref="run-1",
            run_id="",
            role="sprint-worker",
            worker_ref="",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            controller.reserve(reserve_args, self.cfg)
        with self.assertRaises(controller.SprintError):
            controller.reserve(reserve_args, self.cfg)
        running = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(running["attempts"], 1)
        self.assertEqual(
            len(
                [item for item in running["history"] if item.get("event") == "reserved"]
            ),
            1,
        )

        identity = {
            "kind": "execution_unit",
            "invocation_id": "invocation-1",
            "containment": "test-supervisor",
            "tombstone_path": str(self.cfg["shared_root"] / "terminal.json"),
        }
        running["launch_evidence"] = {
            "repository": str(self.cfg["shared_root"]),
            "sprint": "1",
            "ticket": "PROJ-1",
            "attempt": 1,
            "attempt_token": running["attempt_token"],
            "token": "attach-once",
            "status": "launching",
            "identity": identity,
            "invocation_id": "invocation-1",
            "containment": "test-supervisor",
            "tombstone_path": identity["tombstone_path"],
        }
        controller.save(path, controller.load(path) | {"tickets": {"PROJ-1": running}})
        attach_args = argparse.Namespace(
            sprint="1", ticket="PROJ-1", launch_evidence="attach-once"
        )
        with (
            patch.object(controller, "execution_unit_status", return_value="live"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.attach(attach_args, self.cfg)
        with (
            patch.object(controller, "execution_unit_status", return_value="live"),
            self.assertRaises(controller.SprintError),
        ):
            controller.attach(attach_args, self.cfg)
        attached = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(attached["worker_identity"]["invocation_id"], "invocation-1")
        self.assertEqual(
            len(
                [
                    item
                    for item in attached["history"]
                    if item.get("event") == "attached"
                ]
            ),
            1,
        )

    def test_ineligible_recovery_does_not_stop_independent_queue(self) -> None:
        self.cfg["preserved_pr_auto_recovery"] = True
        self.fixture.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            worker_identity="",
            verified_commits={"a" * 40: "b" * 40},
        )
        self.fixture.ticket("PROJ-2", pr="", branch="")
        with patch.object(
            controller,
            "observe_preserved_pr",
            side_effect=controller.SprintError("offline fixture"),
        ):
            plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["launch"], ["PROJ-2"])
        self.assertEqual(plan["decision_queue"][0]["key"], "PROJ-1")
        self.assertFalse(plan["pr_reconciliation"])


if __name__ == "__main__":
    unittest.main()
