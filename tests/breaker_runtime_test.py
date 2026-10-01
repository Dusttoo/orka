#!/usr/bin/env python3
"""Ticket and route breaker runtime binding and replay coverage."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from breaker_runtime import BreakerRuntime, BreakerRuntimeError  # noqa: E402


class BreakerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = BreakerRuntime()

    def test_every_ticket_terminal_breaker_binds_its_exact_durable_state(self) -> None:
        expected = {
            "needs_decomposition": ("ticket_decomposition", "decomposition_ready"),
            "external_blocked": ("ticket_external_wait", "parked_external"),
            "operator_decision": ("ticket_hard_decision", "parked_decision"),
            "blocked": ("ticket_irrecoverable", "blocked"),
            "malformed_result": ("ticket_recovery", "recovery_ready"),
            "timeout_with_progress": ("ticket_recovery", "recovery_ready"),
            "timeout_without_progress": ("ticket_retry_wait", "retry_wait"),
            "cancelled_attempt": ("ticket_recovery", "recovery_ready"),
        }
        for outcome, (class_id, target) in expected.items():
            with self.subTest(outcome=outcome):
                record = self.runtime.terminal_record(
                    outcome,
                    target_state=target,
                    ticket="PROJ-1",
                    evidence={"receipt": outcome},
                )
                self.assertEqual(record["class_id"], class_id)
                self.assertEqual(record["scope"], "ticket")
                self.assertEqual(record["durable_state"], target)
                self.assertEqual(record["subject"], "PROJ-1")
                self.assertEqual(json.loads(json.dumps(record)), record)

    def test_non_breaker_terminal_outcomes_remain_unclassified(self) -> None:
        for outcome in ("completed", "needs_repair"):
            self.assertIsNone(
                self.runtime.terminal_record(
                    outcome,
                    target_state="completed",
                    ticket="PROJ-1",
                    evidence={"receipt": outcome},
                )
            )

    def test_terminal_target_drift_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            BreakerRuntimeError, "contract requires retry_wait"
        ):
            self.runtime.terminal_record(
                "timeout_without_progress",
                target_state="recovery_ready",
                ticket="PROJ-1",
                evidence={"receipt": "terminal"},
            )

    def test_route_holds_bind_exact_role_route_and_strength(self) -> None:
        transient = self.runtime.route_record(
            {
                "provider": "openai",
                "role": "sprint-worker",
                "route_identity": "route-worker",
                "state": "rate_limited",
                "incident": "incident-1",
                "retry_at": 123,
            }
        )
        hard = self.runtime.route_record(
            {
                "provider": "openai",
                "role": "ticket-scoper",
                "route_identity": "route-scoper",
                "state": "authentication",
                "incident": "incident-2",
            }
        )
        self.assertEqual(
            (transient["class_id"], transient["strength"], transient["subject"]),
            ("route_transient_hold", "soft", "route-worker"),
        )
        self.assertEqual(
            (hard["class_id"], hard["strength"], hard["subject"]),
            ("route_hard_hold", "hard", "route-scoper"),
        )
        self.assertNotEqual(transient["evidence_digest"], hard["evidence_digest"])

    def test_route_hold_without_exact_identity_fails_closed(self) -> None:
        with self.assertRaisesRegex(BreakerRuntimeError, "exact route identity"):
            self.runtime.route_record(
                {
                    "provider": "openai",
                    "role": "sprint-worker",
                    "state": "transport",
                }
            )

    def test_ticket_and_route_records_replay_deterministically(self) -> None:
        arguments = {
            "source_id": "max_usd_per_ticket",
            "subject": "PROJ-2",
            "evidence": {"spent": "30", "ceiling": "30"},
        }
        first = self.runtime.record(**arguments)
        second = self.runtime.record(**arguments)
        self.assertEqual(first, second)
        self.assertEqual(first["class_id"], "ticket_hard_decision")
        self.assertEqual(first["authority"], "ticket_scoped_operator")

    def test_sprint_breakers_are_global_and_target_only_declared_states(self) -> None:
        pressure = self.runtime.sprint_record(
            "unfinished_pr_pressure",
            sprint="99",
            evidence={"pressure_class": "wip", "capacity_snapshot": {"used": 3}},
        )
        budget = self.runtime.sprint_record(
            "max_usd_per_sprint",
            sprint="99",
            evidence={"budget_receipt": "receipt"},
        )
        self.assertEqual(
            (pressure["scope"], pressure["durable_state"], pressure["strength"]),
            ("sprint", "degraded", "soft"),
        )
        self.assertEqual(
            (budget["scope"], budget["durable_state"], budget["strength"]),
            ("sprint", "paused", "hard"),
        )
        self.assertTrue(pressure["global_transition"])
        self.assertEqual(
            pressure["record_digest"],
            self.runtime.sprint_record(
                "unfinished_pr_pressure",
                sprint="99",
                evidence={"pressure_class": "wip", "capacity_snapshot": {"used": 3}},
            )["record_digest"],
        )

    def test_ticket_or_route_source_cannot_be_promoted_to_sprint(self) -> None:
        with self.assertRaisesRegex(BreakerRuntimeError, "cannot change sprint state"):
            self.runtime.sprint_record(
                "provider_transport",
                sprint="99",
                evidence={"route": "worker"},
            )


if __name__ == "__main__":
    unittest.main()
