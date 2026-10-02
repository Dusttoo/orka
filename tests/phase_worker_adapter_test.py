#!/usr/bin/env python3
"""Cross-adapter conformance for the disposable phase-worker boundary."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from phase_execution import PhaseExecutionRuntime  # noqa: E402
from phase_worker_adapter import (  # noqa: E402
    AdapterProtocolError,
    capability_offer,
    classify_route_failure,
)
from phase_worker_contract import validate_envelope  # noqa: E402
from supervisor_dispatch import (  # noqa: E402
    ControllerDispatchAdapter,
    DispatchError,
    SupervisorDispatcher,
)


MANDATORY = {
    "attempt-fencing",
    "cancellation-ack-or-fence",
    "fresh-context-per-dispatch",
    "immutable-job-identity",
    "structured-progress",
    "structured-terminal-result",
}


class RefusingAdapter:
    """Advertises an old protocol and proves reservation is never reached."""

    origin = "desktop"

    def __init__(self) -> None:
        self.reservations = 0

    def phase_capability_offer(self, _contract: dict) -> dict:
        return capability_offer(
            "codex-desktop", adapter_version="old", protocol_version=0
        )

    def reserve(self, *_args) -> dict:
        self.reservations += 1
        raise AssertionError("reservation must follow negotiation")


class AdapterConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="orka-phase-adapter-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.contract = json.loads(
            (ROOT / "contracts/phase-worker-protocol-v1.json").read_text()
        )

    def test_all_adapters_advertise_the_shared_contract(self) -> None:
        fixtures = json.loads(
            (ROOT / "tests/fixtures/phase-worker-protocol-v1.json").read_text()
        )
        fixture_offers = {
            case["envelope"]["adapter"]: case["envelope"]
            for case in fixtures["cases"]
            if case.get("valid")
            and case.get("envelope", {}).get("kind") == "capability_offer"
        }
        expected_profiles = {
            "codex-desktop": "desktop-subscription",
            "claude-desktop": "desktop-subscription",
            "api": "provider-receipts",
        }
        for profile, profile_capability in expected_profiles.items():
            with self.subTest(profile=profile):
                offer = capability_offer(profile, adapter_version="fixture-1")
                validate_envelope(self.contract, offer)
                self.assertEqual(offer["protocol_version"], 1)
                self.assertTrue(offer["fresh_context_per_dispatch"])
                self.assertEqual(
                    set(offer["supported_capabilities"]),
                    MANDATORY | {profile_capability},
                )
                self.assertEqual(
                    set(offer["supported_capabilities"]),
                    set(fixture_offers[profile]["supported_capabilities"]),
                )

    def test_controller_adapters_derive_profiles_from_authoritative_routes(self) -> None:
        repository = self.temp / "repo"
        runtime = repository / ".orchestration/.supervisor"
        runtime.mkdir(parents=True)
        adapter = ControllerDispatchAdapter(repository, runtime)
        routes = (
            ({"execution": "desktop", "provider": "openai"}, "codex-desktop"),
            (
                {"execution": "desktop", "provider": "anthropic"},
                "claude-desktop",
            ),
            ({"execution": "api", "provider": "openai"}, "api"),
        )
        for route, expected in routes:
            with self.subTest(route=route), patch(
                "supervisor_dispatch.llm_route_from_config", return_value=route
            ):
                offer = adapter.phase_capability_offer(self.contract)
            self.assertEqual(offer["adapter"], expected)
            validate_envelope(self.contract, offer)

    def test_adapter_does_not_echo_a_future_supervisor_protocol(self) -> None:
        repository = self.temp / "repo"
        runtime = repository / ".orchestration/.supervisor"
        runtime.mkdir(parents=True)
        adapter = ControllerDispatchAdapter(repository, runtime)
        future_contract = {**self.contract, "protocol_version": 2}
        with patch(
            "supervisor_dispatch.llm_route_from_config",
            return_value={"execution": "desktop", "provider": "openai"},
        ):
            offer = adapter.phase_capability_offer(future_contract)
        self.assertEqual(offer["protocol_version"], 1)

    def test_incompatible_adapter_fails_before_reservation(self) -> None:
        repository = self.temp / "repo"
        runtime = repository / ".orchestration/.supervisor"
        runtime.mkdir(parents=True)
        adapter = RefusingAdapter()
        dispatcher = SupervisorDispatcher(
            repository,
            runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            adapter,
        )

        with self.assertRaisesRegex(DispatchError, "protocol incompatibility"):
            dispatcher.launch("7", "PROJ-1")
        self.assertEqual(adapter.reservations, 0)

    def test_each_adapter_uses_identical_lifecycle_semantics(self) -> None:
        for profile in ("codex-desktop", "claude-desktop", "api"):
            with self.subTest(profile=profile):
                ids: dict[str, int] = {}

                def next_id(prefix: str) -> str:
                    ids[prefix] = ids.get(prefix, 0) + 1
                    return f"{prefix}-{ids[prefix]}"

                runtime = PhaseExecutionRuntime(
                    ROOT / "contracts/phase-worker-protocol-v1.json",
                    repository_id="repo:fixture",
                    supervisor_fence="lease:fixture",
                    id_factory=next_id,
                )
                state = runtime.create_job(
                    ticket_id="PROJ-1",
                    phase="review",
                    attempt_token="attempt-1",
                    worktree_id="worktree:fixture",
                    sanitized_input={"objective": "Review the accepted slice"},
                )
                execution = runtime.dispatch(
                    state, capability_offer(profile, adapter_version="fixture-1")
                )
                identity = execution["identity"]
                runtime.ingest(
                    state,
                    {
                        **identity,
                        "kind": "progress",
                        "protocol_version": 1,
                        "sequence": 1,
                        "milestone": "reviewed",
                        "evidence": {"digest": "fixture"},
                    },
                )
                cancellation = runtime.request_cancel(
                    state, reason="drain", deadline="2026-10-01T00:01:00Z"
                )
                result = runtime.ingest(
                    state,
                    {
                        **identity,
                        "kind": "cancellation_ack",
                        "protocol_version": 1,
                        "cancellation_id": cancellation["cancellation_id"],
                        "status": "acknowledged",
                        "terminal_receipt": {"returncode": 0},
                    },
                )
                self.assertEqual(result["outcome"], "cancelled_attempt")

                malformed = runtime.create_job(
                    ticket_id="PROJ-2",
                    phase="implement",
                    attempt_token="attempt-2",
                    worktree_id="worktree:fixture",
                    sanitized_input={"objective": "Implement the accepted slice"},
                )
                runtime.dispatch(
                    malformed,
                    capability_offer(profile, adapter_version="fixture-1"),
                )
                rejected = runtime.reject_output(malformed, "invalid JSON result")
                self.assertEqual(rejected["outcome"], "malformed_result")
                self.assertEqual(malformed["status"], "replaceable")

    def test_route_failure_classification_separates_protocol_and_provider(self) -> None:
        protocol = classify_route_failure(
            AdapterProtocolError("unsupported_protocol", "v2")
        )
        outage = classify_route_failure(TimeoutError("provider unavailable"))

        self.assertEqual(protocol["class"], "protocol_incompatibility")
        self.assertEqual(protocol["scope"], "route")
        self.assertFalse(protocol["provider_outage"])
        self.assertEqual(outage["class"], "provider_outage")
        self.assertEqual(outage["scope"], "provider")
        self.assertTrue(outage["provider_outage"])


if __name__ == "__main__":
    unittest.main()
