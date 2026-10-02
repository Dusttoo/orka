#!/usr/bin/env python3
"""Conformance tests for Orka's replaceable execution-backend boundary."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from execution_backend import (  # noqa: E402
    BackendContractError,
    BackendCoordinator,
    DeterministicFakeBackend,
    TransactionalBackendState,
)
from event_store import RepositoryBinding, TransactionalEventStore  # noqa: E402
from execution_backend_contract import (  # noqa: E402
    ContractError as BackendDefinitionError,
    load_contract,
    validate_contract,
)


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
            "required_capabilities": [
                "attempt-fencing",
                "cancellation-ack-or-fence",
                "fresh-context-per-dispatch",
                "immutable-job-identity",
                "structured-progress",
                "structured-terminal-result",
            ],
            "fresh_context": True,
        }

    def coordinator(self, **backend_options: object) -> tuple[BackendCoordinator, DeterministicFakeBackend]:
        backend = DeterministicFakeBackend(**backend_options)
        coordinator = BackendCoordinator(
            self.contract, backend, allow_test_backend=True
        )
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

    def test_contract_rejects_weakened_tombstone_fence_and_terminal_rules(self) -> None:
        mutations = (
            ("tombstone", lambda value: value["execution_tombstones"].update(full_execution_key_reuse_allowed=True)),
            ("fence", lambda value: value["cancellation"].update(backend_may_assert_fence=True)),
            ("terminal", lambda value: value["inspection"].update(terminal_requires_canonical_phase_envelope=False)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(self.contract)
                mutate(changed)
                with self.assertRaises(BackendDefinitionError):
                    validate_contract(changed)

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
        coordinator = BackendCoordinator(
            self.contract, backend, allow_test_backend=True
        )
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
        with self.assertRaisesRegex(BackendContractError, "resume_session"):
            coordinator.launch(
                {**self.binding, "dispatch_id": "dispatch-2", "execution_unit_id": "execution-2"},
                reused,
            )

    def test_launch_delegates_to_canonical_phase_worker_validation(self) -> None:
        invalid_cases = (
            ({key: value for key, value in self.envelope.items() if key != "ticket_id"}, "ticket_id"),
            ({**self.envelope, "required_capabilities": ["structured-terminal-result"]}, "mandatory capability"),
            ({**self.envelope, "sanitized_input": "not-an-object"}, "sanitized_input"),
        )
        for envelope, message in invalid_cases:
            with self.subTest(message=message):
                coordinator, backend = self.coordinator()
                with self.assertRaisesRegex(BackendContractError, message):
                    coordinator.launch(self.binding, envelope)
                self.assertEqual(backend.launch_count, 0)

    def test_launch_rejects_nested_session_state_before_backend_dispatch(self) -> None:
        invalid_inputs = (
            {"metadata": {"conversation_id": "conversation-1"}},
            {"metadata": [{"previous_conversation_id": "conversation-0"}]},
            {"steps": [{"provider": {"provider_session_id": "session-1"}}]},
            {"steps": ["ordinary", {"resume_session": "session-0"}]},
        )
        for sanitized_input in invalid_inputs:
            with self.subTest(sanitized_input=sanitized_input):
                coordinator, backend = self.coordinator()
                envelope = {**self.envelope, "sanitized_input": sanitized_input}
                with self.assertRaisesRegex(BackendContractError, "forbidden field"):
                    coordinator.launch(self.binding, envelope)
                self.assertEqual(backend.launch_count, 0)

    def test_session_field_names_inside_string_values_are_allowed(self) -> None:
        coordinator, backend = self.coordinator()
        envelope = {
            **self.envelope,
            "sanitized_input": {
                "objective": (
                    "Document conversation_id, previous_conversation_id, "
                    "provider_session_id, and resume_session without resuming state"
                )
            },
        }

        coordinator.launch(self.binding, envelope)

        self.assertEqual(backend.launch_count, 1)

    def test_launch_receipt_is_mechanically_bound_to_envelope_and_process(self) -> None:
        for mode in ("missing", "mismatched", "envelope-mismatch"):
            with self.subTest(mode=mode):
                coordinator, backend = self.coordinator(launch_receipt_mode=mode)
                with self.assertRaisesRegex(BackendContractError, "launch receipt"):
                    coordinator.launch(self.binding, self.envelope)
                self.assertEqual(backend.launch_count, 1)
                with self.assertRaisesRegex(BackendContractError, "previously launched"):
                    coordinator.launch(self.binding, self.envelope)
                self.assertEqual(backend.launch_count, 1)

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

    def test_fenced_execution_key_is_a_permanent_tombstone(self) -> None:
        coordinator, backend = self.coordinator()
        coordinator.launch(self.binding, self.envelope)
        coordinator.fence(self.binding, reason="replacement admitted")

        with self.assertRaisesRegex(BackendContractError, "previously launched"):
            coordinator.launch(self.binding, self.envelope)
        self.assertEqual(backend.launch_count, 1)

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

    def test_false_or_mismatched_cancellation_acknowledgement_is_rejected(self) -> None:
        for mode in ("mismatched", "false-verification"):
            with self.subTest(mode=mode):
                coordinator, _backend = self.coordinator(cancel_mode=mode)
                coordinator.launch(self.binding, self.envelope)
                with self.assertRaisesRegex(
                    BackendContractError, "cancellation acknowledgement"
                ):
                    coordinator.cancel(self.binding, reason="operator drain")
                self.assertEqual(
                    coordinator.inspect(self.binding)["status"], "live"
                )

    def test_backend_cannot_assert_a_supervisor_fence(self) -> None:
        coordinator, _backend = self.coordinator(cancel_mode="fenced")
        coordinator.launch(self.binding, self.envelope)
        with self.assertRaisesRegex(BackendContractError, "unsupported status"):
            coordinator.cancel(self.binding, reason="operator drain")

    def test_terminal_requires_canonical_exact_identity_envelope(self) -> None:
        coordinator, backend = self.coordinator()
        coordinator.launch(self.binding, self.envelope)
        backend.terminal_mode = "mismatched"
        with self.assertRaisesRegex(BackendContractError, "terminal evidence"):
            coordinator.terminal(self.binding)

    def test_valid_canonical_terminal_is_accepted_directly_and_by_inspection(self) -> None:
        coordinator, backend = self.coordinator()
        coordinator.launch(self.binding, self.envelope)
        terminal = coordinator.terminal(self.binding)
        self.assertEqual(terminal["terminal_envelope"]["outcome"], "recoverable")

        backend.inspect_status = "terminal"
        observed = coordinator.inspect(self.binding)
        self.assertEqual(observed["status"], "terminal")
        self.assertTrue(observed["replacement_safe"])

    def test_terminal_inspection_without_valid_evidence_fails_closed(self) -> None:
        coordinator, backend = self.coordinator(inspect_status="terminal")
        coordinator.launch(self.binding, self.envelope)
        backend.inspect_terminal_evidence = False
        observation = coordinator.inspect(self.binding)
        self.assertEqual(observation["status"], "unknown")
        self.assertFalse(observation["replacement_safe"])
        self.assertEqual(observation["reason"], "terminal_evidence_invalid")

    def test_containment_and_provider_capabilities_are_independent(self) -> None:
        coordinator, backend = self.coordinator()
        offer = coordinator.offer
        self.assertEqual(offer["containment"]["filesystem"], "worktree")
        self.assertEqual(offer["provider_capabilities"], [])
        self.assertNotIn("provider", offer["containment"])
        self.assertFalse(hasattr(backend, "schedule"))
        self.assertFalse(hasattr(backend, "authorize_merge"))

    def test_fake_backend_is_rejected_outside_explicit_conformance_mode(self) -> None:
        backend = DeterministicFakeBackend()
        coordinator = BackendCoordinator(self.contract, backend)
        with self.assertRaisesRegex(BackendContractError, "test-only"):
            coordinator.negotiate()

    def transactional_state(
        self,
    ) -> tuple[tempfile.TemporaryDirectory[str], TransactionalEventStore, TransactionalBackendState]:
        temporary = tempfile.TemporaryDirectory()
        store = TransactionalEventStore(
            Path(temporary.name) / "state.sqlite3", writer_identity="supervisor:test"
        )
        store.bind_repository(
            RepositoryBinding(
                repository_id="repository:fixture",
                common_directory="/repo/.git",
                object_directory_id="objects",
                policy_ref="refs/heads/main",
                policy_path="orchestration.yml",
                policy_commit="a" * 40,
                policy_blob="b" * 40,
                policy_digest="policy",
                created_at="2026-10-02T00:00:00Z",
            ),
            writer_identity="supervisor:test",
        )
        return (
            temporary,
            store,
            TransactionalBackendState(store, writer_identity="supervisor:test"),
        )

    def test_transactional_tombstone_survives_supervisor_restart(self) -> None:
        temporary, store, state = self.transactional_state()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(store.close)
        backend = DeterministicFakeBackend()
        first = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        first.negotiate()
        launch = first.launch(self.binding, self.envelope)
        first.attach(self.binding)

        restarted = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        restarted.negotiate()
        restored = restarted.restore(self.binding, self.envelope)
        self.assertEqual(restored["launch_receipt"], launch["launch_receipt"])
        self.assertEqual(restarted.progress(self.binding)["status"], "progress")
        terminal = restarted.terminal(self.binding)
        self.assertEqual(terminal["status"], "terminal")
        record = store.execution_record(self.binding)
        self.assertEqual(record["state"], "terminal")
        after_terminal = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        after_terminal.negotiate()
        after_terminal.restore(self.binding, self.envelope)
        replay = after_terminal.terminal(self.binding)
        self.assertEqual(replay, terminal)
        self.assertEqual(store.execution_record(self.binding)["version"], record["version"])

    def test_failed_launch_leaves_transactional_uncertain_tombstone(self) -> None:
        temporary, store, state = self.transactional_state()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(store.close)
        backend = DeterministicFakeBackend(launch_receipt_mode="mismatched")
        coordinator = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        coordinator.negotiate()
        with self.assertRaisesRegex(BackendContractError, "launch receipt"):
            coordinator.launch(self.binding, self.envelope)
        self.assertEqual(store.execution_record(self.binding)["state"], "uncertain")
        replacement = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        replacement.negotiate()
        with self.assertRaisesRegex(BackendContractError, "already consumed"):
            replacement.launch(self.binding, self.envelope)

    def test_transactional_cancellation_acknowledgement_is_fenced(self) -> None:
        temporary, store, state = self.transactional_state()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(store.close)
        backend = DeterministicFakeBackend(cancel_mode="acknowledged")
        coordinator = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        coordinator.negotiate()
        coordinator.launch(self.binding, self.envelope)
        coordinator.attach(self.binding)
        result = coordinator.cancel(self.binding, reason="operator drain")
        self.assertTrue(result["replacement_safe"])
        self.assertEqual(result["supervisor_fence"]["status"], "fenced")
        record = store.execution_record(self.binding)
        self.assertEqual(record["state"], "fenced")
        self.assertEqual(
            record["cancellation_receipt"]["status"], "acknowledged"
        )
        self.assertEqual(record["fence_receipt"]["status"], "fenced")
        with self.assertRaisesRegex(BackendContractError, "stale or fenced"):
            coordinator.progress(self.binding)

    def test_transactional_activity_does_not_clear_pending_cancellation(self) -> None:
        temporary, store, state = self.transactional_state()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(store.close)
        backend = DeterministicFakeBackend(cancel_mode="unknown")
        coordinator = BackendCoordinator(
            self.contract, backend, allow_test_backend=True, state_store=state
        )
        coordinator.negotiate()
        coordinator.launch(self.binding, self.envelope)
        coordinator.attach(self.binding)
        coordinator.cancel(self.binding, reason="operator drain")
        self.assertEqual(store.execution_record(self.binding)["state"], "cancelling")

        coordinator.progress(self.binding)

        self.assertEqual(store.execution_record(self.binding)["state"], "cancelling")


if __name__ == "__main__":
    unittest.main()
