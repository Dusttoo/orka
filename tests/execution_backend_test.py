#!/usr/bin/env python3
"""Conformance tests for Orka's replaceable execution-backend boundary."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from execution_backend import (  # noqa: E402
    BackendContractError,
    BackendCoordinator,
    DeterministicFakeBackend,
)
from execution_backend_contract import load_contract, validate_contract  # noqa: E402


class ExecutionBackendContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract(ROOT / "contracts/execution-backend-v1.json")
        self.binding = {
            "repository_id": "repository:fixture",
            "job_id": "job-1",
            "phase": "implement",
            "attempt_token": "attempt-1",
            "dispatch_id": "dispatch-1",
            "execution_unit_id": "execution-1",
            "worktree_id": "worktree:fixture",
            "supervisor_fence": "lease:fixture",
        }
        self.envelope = {
            "kind": "job",
            "protocol_version": 1,
            **self.binding,
            "ticket_id": "PROJ-1",
            "sanitized_input": {"objective": "Implement the accepted slice"},
            "required_capabilities": ["structured-terminal-result"],
            "fresh_context": True,
        }

    def coordinator(self, **backend_options: object) -> tuple[BackendCoordinator, DeterministicFakeBackend]:
        backend = DeterministicFakeBackend(**backend_options)
        coordinator = BackendCoordinator(self.contract, backend)
        coordinator.negotiate(
            required={
                "containment": {"filesystem": "worktree"},
                "lifecycle": [
                    "launch",
                    "attach",
                    "heartbeat",
                    "progress",
                    "cancel",
                    "inspect",
                    "terminal",
                ],
            }
        )
        return coordinator, backend

    def test_contract_and_shared_fixture_are_valid(self) -> None:
        summary = validate_contract(self.contract)
        fixture = json.loads(
            (ROOT / "tests/fixtures/execution-backend-v1.json").read_text()
        )
        self.assertEqual(summary["contract_id"], "orka.execution-backend")
        self.assertEqual(summary["protocol_version"], 1)
        self.assertEqual(fixture["contract_id"], summary["contract_id"])
        self.assertEqual(fixture["binding"], self.binding)

    def test_missing_mandatory_capability_fails_before_launch(self) -> None:
        backend = DeterministicFakeBackend(
            containment={
                "process": "isolated-process",
                "filesystem": "none",
                "network": "unrestricted",
                "credentials": "ambient",
                "process_control": "inspect-cancel-fence",
            }
        )
        coordinator = BackendCoordinator(self.contract, backend)
        with self.assertRaisesRegex(BackendContractError, "filesystem"):
            coordinator.negotiate(
                required={"containment": {"filesystem": "worktree"}}
            )
        self.assertEqual(backend.launch_count, 0)

    def test_launch_requires_fresh_phase_envelope_and_immutable_binding(self) -> None:
        coordinator, backend = self.coordinator()
        receipt = coordinator.launch(self.binding, self.envelope)

        self.assertEqual(receipt["binding"], self.binding)
        self.assertEqual(backend.launch_count, 1)
        self.assertTrue(backend.last_envelope["fresh_context"])
        self.assertNotIn("conversation_id", json.dumps(backend.last_envelope))

        reused = {**self.envelope, "resume_session": "old-session"}
        with self.assertRaisesRegex(BackendContractError, "fresh phase context"):
            coordinator.launch(
                {**self.binding, "dispatch_id": "dispatch-2", "execution_unit_id": "execution-2"},
                reused,
            )

    def test_stale_or_replaced_execution_cannot_report_progress_or_terminal(self) -> None:
        coordinator, _backend = self.coordinator()
        coordinator.launch(self.binding, self.envelope)
        replacement = {
            **self.binding,
            "dispatch_id": "dispatch-2",
            "execution_unit_id": "execution-2",
        }
        coordinator.fence(self.binding, reason="replacement admitted")
        coordinator.launch(replacement, {**self.envelope, **replacement})

        with self.assertRaisesRegex(BackendContractError, "stale or fenced"):
            coordinator.progress(self.binding)
        with self.assertRaisesRegex(BackendContractError, "stale or fenced"):
            coordinator.terminal(self.binding)
        self.assertEqual(coordinator.progress(replacement)["status"], "progress")

    def test_unknown_inspection_never_becomes_absence(self) -> None:
        coordinator, backend = self.coordinator(inspect_status="unknown")
        coordinator.launch(self.binding, self.envelope)
        observation = coordinator.inspect(self.binding)

        self.assertEqual(observation["status"], "unknown")
        self.assertFalse(observation["replacement_safe"])
        self.assertNotIn("absence_receipt", observation)
        self.assertEqual(backend.inspect_count, 1)

    def test_timeout_is_unknown_and_process_reuse_proves_only_original_absence(self) -> None:
        coordinator, backend = self.coordinator(inspect_status="timeout")
        launch = coordinator.launch(self.binding, self.envelope)
        timeout = coordinator.inspect(self.binding)
        self.assertEqual(timeout["status"], "unknown")
        self.assertEqual(timeout["reason"], "inspection_timeout")

        backend.reuse_process(launch["backend_handle"])
        reused = coordinator.inspect(self.binding)
        self.assertEqual(reused["status"], "absent")
        self.assertEqual(reused["reason"], "process_identity_reused")
        self.assertTrue(reused["replacement_safe"])
        self.assertEqual(
            reused["absence_receipt"]["original_process_identity"],
            launch["process_identity"],
        )

    def test_cancel_requires_authenticated_acknowledgement_or_supervisor_fence(self) -> None:
        coordinator, backend = self.coordinator(cancel_mode="unknown")
        coordinator.launch(self.binding, self.envelope)
        uncertain = coordinator.cancel(self.binding, reason="operator drain")
        self.assertEqual(uncertain["status"], "unknown")
        self.assertFalse(uncertain["replacement_safe"])

        fenced = coordinator.fence(self.binding, reason="cancel acknowledgement absent")
        self.assertEqual(fenced["status"], "fenced")
        self.assertTrue(fenced["replacement_safe"])

        second_binding = {
            **self.binding,
            "dispatch_id": "dispatch-2",
            "execution_unit_id": "execution-2",
        }
        backend.cancel_mode = "acknowledged"
        coordinator.launch(second_binding, {**self.envelope, **second_binding})
        acknowledged = coordinator.cancel(second_binding, reason="operator drain")
        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertTrue(acknowledged["replacement_safe"])
        self.assertIn("cancellation_receipt", acknowledged)

    def test_containment_and_provider_capabilities_are_independent(self) -> None:
        coordinator, backend = self.coordinator()
        offer = coordinator.offer
        self.assertEqual(offer["containment"]["filesystem"], "worktree")
        self.assertEqual(offer["provider_capabilities"], [])
        self.assertNotIn("provider", offer["containment"])
        self.assertFalse(hasattr(backend, "schedule"))
        self.assertFalse(hasattr(backend, "authorize_merge"))


if __name__ == "__main__":
    unittest.main()
