#!/usr/bin/env python3
"""Lifecycle tests for supervisor-managed disposable phase executions."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from phase_execution import PhaseExecutionError, PhaseExecutionRuntime  # noqa: E402


class PhaseExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = PhaseExecutionRuntime(
            ROOT / "contracts/phase-worker-protocol-v1.json",
            repository_id="repo:orka",
            supervisor_fence="lease:7",
            id_factory=self._id,
        )
        self.ids: dict[str, int] = {}

    def _id(self, prefix: str) -> str:
        self.ids[prefix] = self.ids.get(prefix, 0) + 1
        return f"{prefix}-{self.ids[prefix]}"

    def offer(self) -> dict:
        return {
            "kind": "capability_offer",
            "protocol_version": 1,
            "adapter": "codex-desktop",
            "adapter_version": "test",
            "supported_capabilities": [
                "attempt-fencing",
                "cancellation-ack-or-fence",
                "desktop-subscription",
                "fresh-context-per-dispatch",
                "immutable-job-identity",
                "structured-progress",
                "structured-terminal-result",
            ],
            "fresh_context_per_dispatch": True,
        }

    def create(self) -> dict:
        return self.runtime.create_job(
            ticket_id="ORKA-114",
            phase="implement",
            attempt_token="attempt-7",
            worktree_id="worktree:114",
            sanitized_input={"ticket": "Implement the accepted phase contract"},
        )

    def test_each_dispatch_is_fresh_and_replacement_keeps_job_identity(self) -> None:
        state = self.create()
        first = self.runtime.dispatch(state, self.offer())
        self.assertTrue(first["job"]["fresh_context"])
        self.assertNotIn("conversation_id", json.dumps(state))

        invalid = self.runtime.reject_output(state, "worker returned prose")
        self.assertEqual(invalid["outcome"], "malformed_result")
        self.assertEqual(state["status"], "replaceable")

        second = self.runtime.dispatch(state, self.offer())
        self.assertEqual(second["job"]["job_id"], first["job"]["job_id"])
        self.assertEqual(
            second["external_operation_key"], first["external_operation_key"]
        )
        self.assertNotEqual(second["job"]["dispatch_id"], first["job"]["dispatch_id"])
        self.assertNotEqual(
            second["job"]["execution_unit_id"], first["job"]["execution_unit_id"]
        )

    def test_progress_heartbeat_and_terminal_are_fully_bound(self) -> None:
        state = self.create()
        execution = self.runtime.dispatch(state, self.offer())
        identity = execution["identity"]

        progress = {
            **identity,
            "kind": "progress",
            "protocol_version": 1,
            "sequence": 1,
            "milestone": "tests-red",
            "evidence": {"digest": "red"},
        }
        applied = self.runtime.ingest(state, progress)
        self.assertTrue(applied["applied"])
        self.assertEqual(state["active"]["last_progress_sequence"], 1)
        self.assertTrue(self.runtime.ingest(state, progress)["duplicate"])

        stale = {**progress, "sequence": 2, "supervisor_fence": "lease:6"}
        with self.assertRaisesRegex(PhaseExecutionError, "identity field"):
            self.runtime.ingest(state, stale)

        heartbeat = {
            **identity,
            "kind": "heartbeat",
            "protocol_version": 1,
            "observed_at": "2026-10-01T00:00:00Z",
        }
        self.runtime.ingest(state, heartbeat)
        self.assertEqual(
            state["active"]["last_heartbeat_at"], "2026-10-01T00:00:00Z"
        )

        terminal = {
            **identity,
            "kind": "terminal",
            "protocol_version": 1,
            "repository_id": "repo:orka",
            "worktree_id": "worktree:114",
            "outcome": "completed",
            "summary": "phase complete",
            "artifacts": {"head": "a" * 40},
            "evidence": {"receipt": "verified"},
        }
        result = self.runtime.ingest(state, terminal)
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(state["status"], "terminal")
        self.assertTrue(self.runtime.ingest(state, terminal)["duplicate"])

    def test_attachment_is_attempt_and_fence_bound_once(self) -> None:
        state = self.create()
        execution = self.runtime.dispatch(state, self.offer())
        binding = self.runtime.bind_attachment(
            state, {"invocation_id": "host-unit-1", "origin": "desktop"}
        )
        self.assertEqual(binding["identity"], execution["identity"])
        self.assertEqual(binding["identity"]["attempt_token"], "attempt-7")
        self.assertEqual(binding["identity"]["supervisor_fence"], "lease:7")
        self.assertTrue(
            self.runtime.bind_attachment(
                state, {"invocation_id": "host-unit-1", "origin": "desktop"}
            )["duplicate"]
        )
        with self.assertRaisesRegex(PhaseExecutionError, "already bound"):
            self.runtime.bind_attachment(
                state, {"invocation_id": "host-unit-2", "origin": "desktop"}
            )

    def test_cancel_requires_bound_ack_or_fences_before_replacement(self) -> None:
        state = self.create()
        first = self.runtime.dispatch(state, self.offer())
        cancellation = self.runtime.request_cancel(
            state,
            reason="operator drain",
            deadline="2026-10-01T00:01:00Z",
        )
        self.assertEqual(state["status"], "cancelling")

        stale_ack = {
            **first["identity"],
            "kind": "cancellation_ack",
            "protocol_version": 1,
            "cancellation_id": "other",
            "status": "acknowledged",
            "terminal_receipt": {"digest": "receipt"},
        }
        with self.assertRaisesRegex(PhaseExecutionError, "cancellation id"):
            self.runtime.ingest(state, stale_ack)

        fenced = self.runtime.fence_cancellation(
            state, cancellation["cancellation_id"], "deadline elapsed"
        )
        self.assertEqual(fenced["status"], "fenced")
        self.assertEqual(state["status"], "replaceable")
        second = self.runtime.dispatch(state, self.offer())
        self.assertNotEqual(
            second["identity"]["execution_unit_id"],
            first["identity"]["execution_unit_id"],
        )

    def test_acknowledged_cancellation_is_terminal(self) -> None:
        state = self.create()
        execution = self.runtime.dispatch(state, self.offer())
        cancellation = self.runtime.request_cancel(
            state,
            reason="operator pause",
            deadline="2026-10-01T00:01:00Z",
        )
        acknowledgement = {
            **execution["identity"],
            "kind": "cancellation_ack",
            "protocol_version": 1,
            "cancellation_id": cancellation["cancellation_id"],
            "status": "acknowledged",
            "terminal_receipt": {"digest": "cancelled"},
        }
        result = self.runtime.ingest(state, acknowledgement)
        self.assertEqual(result["outcome"], "cancelled_attempt")
        self.assertEqual(state["status"], "terminal")
        with self.assertRaisesRegex(PhaseExecutionError, "terminal"):
            self.runtime.dispatch(state, self.offer())

    def test_restart_reconciliation_never_duplicates_a_live_worker(self) -> None:
        state = self.create()
        first = self.runtime.dispatch(state, self.offer())
        observed = self.runtime.reconcile_restart(
            state,
            {
                "dispatch_id": first["identity"]["dispatch_id"],
                "execution_unit_id": first["identity"]["execution_unit_id"],
                "status": "live",
            },
        )
        self.assertEqual(observed["action"], "keep-running")
        with self.assertRaisesRegex(PhaseExecutionError, "active execution"):
            self.runtime.dispatch(state, self.offer())

        recovered = self.runtime.reconcile_restart(
            state,
            {
                "dispatch_id": first["identity"]["dispatch_id"],
                "execution_unit_id": first["identity"]["execution_unit_id"],
                "status": "absent",
                "receipt": "process-absence-proof",
            },
        )
        self.assertEqual(recovered["action"], "replace")
        replacement = self.runtime.dispatch(state, self.offer())
        self.assertEqual(replacement["job"]["job_id"], first["job"]["job_id"])


if __name__ == "__main__":
    unittest.main()
