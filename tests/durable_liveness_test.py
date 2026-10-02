#!/usr/bin/env python3
"""Conformance and adversarial tests for the durable liveness contract."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from durable_liveness import LivenessError, load_json, verify  # noqa: E402


CONTRACT = ROOT / "contracts/durable-liveness-v1.json"
FIXTURES = ROOT / "tests/fixtures/durable-liveness-v1.json"


class DurableLivenessTests(unittest.TestCase):
    def contract(self) -> dict:
        return load_json(CONTRACT, "test contract")

    def verify(self, contract: dict, *, fixtures: bool = True) -> dict:
        return verify(
            contract,
            root=ROOT,
            fixtures_path=FIXTURES if fixtures else None,
        )

    def transition(self, contract: dict, identifier: str) -> dict:
        return next(item for item in contract["transitions"] if item["id"] == identifier)

    def test_canonical_contract_covers_every_exit_class_and_regression(self) -> None:
        result = self.verify(self.contract())
        self.assertEqual(result["contract_id"], "orka.durable-liveness")
        self.assertEqual(set(result["exit_classes"]), {
            "automatic",
            "timer_driven",
            "authenticated_external_evidence",
            "operator_authorized",
            "terminal_failure",
        })
        self.assertGreaterEqual(result["fixtures"], 15)
        self.assertGreater(result["reachable_states"], result["fixtures"])

    def test_shortest_counterexample_traces_one_step_reachable_sink(self) -> None:
        contract = self.contract()
        contract["transitions"] = [
            item for item in contract["transitions"] if item["id"] != "attach-worker"
        ]
        with self.assertRaises(LivenessError) as raised:
            self.verify(contract, fixtures=False)
        self.assertEqual(
            raised.exception.report["error"],
            "reachable nonterminal state has no supported exit",
        )
        self.assertEqual(raised.exception.report["seed"], "fresh-queued-ticket")
        self.assertEqual(raised.exception.report["trace"], ["admit-ticket"])
        self.assertEqual(raised.exception.report["state"]["ticket_state"], "reserved")

    def test_tokenless_repair_without_recovery_is_a_counterexample(self) -> None:
        contract = self.contract()
        fixture = next(
            case
            for case in load_json(FIXTURES, "fixtures")["cases"]
            if case["name"] == "historical-tokenless-repair"
        )
        contract["initial_states"] = [
            {"name": fixture["name"], "state": fixture["state"]}
        ]
        contract["transitions"] = [
            item
            for item in contract["transitions"]
            if item["id"] != "recover-tokenless-repair"
        ]
        with self.assertRaises(LivenessError) as raised:
            self.verify(contract, fixtures=False)
        self.assertEqual(raised.exception.report["seed"], fixture["name"])
        self.assertEqual(raised.exception.report["trace"], [])

    def test_operator_exit_requires_a_real_command_and_named_authority(self) -> None:
        contract = self.contract()
        transition = self.transition(contract, "recover-tokenless-repair")
        transition.pop("authority")
        with self.assertRaisesRegex(LivenessError, "lacks a named authority"):
            self.verify(contract)

        contract = self.contract()
        contract["operator_authorities"]["root_capability"]["commands"].remove(
            "controller:recover-terminal"
        )
        with self.assertRaisesRegex(LivenessError, "not granted by root_capability"):
            self.verify(contract)

    def test_contract_references_must_exist_in_authoritative_sources(self) -> None:
        contract = self.contract()
        self.transition(contract, "admit-ticket")["source_refs"].append(
            "supervisor:event:invented"
        )
        with self.assertRaisesRegex(LivenessError, "undefined source refs"):
            self.verify(contract)

    def test_terminal_reactivation_requires_explicit_recovery(self) -> None:
        contract = self.contract()
        self.transition(contract, "restart-cancelled-ticket")["explicit_recovery"] = False
        with self.assertRaises(LivenessError) as raised:
            self.verify(contract)
        self.assertEqual(
            raised.exception.report["error"],
            "terminal state has a non-recovery mutation",
        )
        self.assertIn(
            "restart-cancelled-ticket", raised.exception.report["illegal_transitions"]
        )

    def test_ticket_local_hold_cannot_block_an_independent_ticket(self) -> None:
        contract = self.contract()
        fixture = next(
            case
            for case in load_json(FIXTURES, "fixtures")["cases"]
            if case["name"] == "independent-ticket-progress"
        )
        contract["initial_states"] = [
            {"name": fixture["name"], "state": fixture["state"]}
        ]
        contract["transitions"] = [
            item
            for item in contract["transitions"]
            if item["id"] != "dispatch-independent-ticket"
        ]
        with self.assertRaises(LivenessError) as raised:
            self.verify(contract, fixtures=False)
        self.assertEqual(
            raised.exception.report["error"],
            "ticket-local hold blocks an independent queued ticket",
        )

    def test_every_historical_fixture_names_a_supported_exit(self) -> None:
        contract = self.contract()
        transition_ids = {item["id"] for item in contract["transitions"]}
        fixtures = load_json(FIXTURES, "fixtures")["cases"]
        historical = [case for case in fixtures if case["name"].startswith("historical-")]
        self.assertEqual(
            {case["name"] for case in historical},
            {
                "historical-tokenless-repair",
                "historical-escalated-pending-review",
                "historical-moved-pr-head",
                "historical-stale-attempt-identity",
                "historical-preserved-pr-recovery",
            },
        )
        self.assertTrue(
            all(case["expected_transition"] in transition_ids for case in historical)
        )

    def test_stale_attempt_recovery_cannot_settle_ambiguous_provider_usage(self) -> None:
        contract = self.contract()
        reconcile = self.transition(contract, "reconcile-ambiguous-reservation")
        recovery = self.transition(contract, "recover-stale-attempt")
        self.assertEqual(
            reconcile["exit_class"], "authenticated_external_evidence"
        )
        self.assertEqual(reconcile["set"], {"reservation_state": "settled"})
        self.assertEqual(recovery["when"]["reservation_state"], "settled")
        self.assertNotIn("reservation_state", recovery["set"])

    def test_sprint_budget_exit_uses_standing_policy_then_exact_resume(self) -> None:
        contract = self.contract()
        raise_policy = self.transition(contract, "raise-sprint-budget-policy")
        observe = self.transition(contract, "observe-sprint-budget-resolution")
        resume = self.transition(contract, "resume-after-sprint-budget")
        self.assertEqual(
            raise_policy["operation"], "operator_authority:set-budget-policy"
        )
        self.assertEqual(
            raise_policy["set"]["sprint_breaker"], "paused_budget_policy_raised"
        )
        self.assertEqual(
            observe["set"]["sprint_breaker"], "paused_budget_resolved"
        )
        self.assertEqual(resume["operation"], "supervisor_cli:resume")
        self.assertEqual(resume["when"]["sprint_breaker"], "paused_budget_resolved")
        self.assertEqual(resume["set"]["sprint_breaker"], "active")
        self.assertNotIn(
            "controller:grant-budget",
            contract["operator_authorities"]["sprint_scoped_root"]["commands"],
        )

    def test_contract_and_fixture_inputs_are_not_mutated(self) -> None:
        contract = self.contract()
        before = copy.deepcopy(contract)
        self.verify(contract)
        self.assertEqual(contract, before)
        json.dumps(contract, sort_keys=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
