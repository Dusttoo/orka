"""Phase envelopes share the existing atomic admission and settlement ledger."""
import concurrent.futures
import fcntl
import threading
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from api_agent import AgentError, BudgetError, UsageLedger, budgets_from_config


class PhaseBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = UsageLedger(Path(self.temp.name))
        self.limits = budgets_from_config({"llm": {"budgets": {
            "max_usd_per_design_phase": 1, "max_usd_per_implementation_phase": 2,
            "max_usd_per_code_review_phase": 1, "max_usd_per_security_review_phase": 1}}})

    def reserve(self, amount, *, role="design-reviewer", ticket="T-1", run="run"):
        return self.ledger.reserve(projected=Decimal(amount), limits=self.limits,
            run_id=run, ticket=ticket, sprint="1", provider="anthropic", model="test", role=role)

    def settle(self, reservation, amount, *, role=None):
        self.ledger.settle(reservation, run_id="run", ticket="T-1", sprint="1",
            provider="anthropic", model="test", response_id=reservation,
            usage={}, cost=Decimal(amount), role=role)

    def test_transfer_preserves_total_envelope_and_review_capacity(self):
        self.settle(self.reserve(".4"), ".4")
        self.assertTrue(self.ledger.transfer_design_budget("T-1", self.limits, "pass"))
        self.assertFalse(self.ledger.transfer_design_budget("T-1", self.limits, "pass-again"))
        limits = self.ledger.phase_limits(self.ledger.snapshot(), "T-1", self.limits)
        self.assertEqual(limits["implementation"], Decimal("2.6"))
        self.assertEqual(limits["code_review"], Decimal("1"))
        self.assertEqual(limits["security_review"], Decimal("1"))
        self.assertEqual(sum(limits.values()), Decimal("5"))
        with self.assertRaises(BudgetError):
            self.reserve(".01")
        self.assertTrue(self.reserve("2.6", role="implementer"))
        with self.assertRaises(BudgetError):
            self.reserve(".01", role="implementer")

    def test_transfer_waits_for_uncertain_design_and_respects_tighter_configuration(self):
        reservation = self.reserve(".4")
        self.assertFalse(self.ledger.transfer_design_budget("T-1", self.limits, "pass"))
        self.ledger.release(reservation, "run", "rejected")
        self.assertTrue(self.ledger.transfer_design_budget("T-1", self.limits, "pass"))
        limits = dict(self.limits, max_usd_per_design_phase=Decimal(".2"))
        effective = self.ledger.phase_limits(self.ledger.snapshot(), "T-1", limits)
        self.assertEqual(effective["implementation"], Decimal("2.2"))

    def test_spent_design_does_not_pause_another_phase_or_ticket(self):
        self.settle(self.reserve("1"), "1")
        with self.assertRaisesRegex(BudgetError, "max_usd_per_design_phase"):
            self.reserve(".01")
        self.assertTrue(self.reserve(".1", role="code-reviewer"))
        self.assertTrue(self.reserve(".1", ticket="T-2"))
        self.assertFalse(any(e["kind"] == "ticket_budget_pause" for e in self.ledger._events()))

    def test_atomic_concurrent_reservations_cannot_overbook(self):
        def attempt(index):
            try:
                return self.reserve(".6", run=f"run-{index}")
            except BudgetError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        reservation = next(result for result in results if result)
        self.ledger.release(reservation, "run", "provider rejected")
        self.assertTrue(self.reserve("1"))

    def test_actual_settlement_reclaims_unused_reservation(self):
        self.settle(self.reserve("1"), ".2")
        self.assertTrue(self.reserve(".8"))
        totals = self.ledger.phase_totals(self.ledger._events(), "T-1")["design"]
        self.assertEqual(totals, {"spent_usd": Decimal(".2"), "reserved_usd": Decimal(".8")})

    def test_reconciliation_inherits_reserved_role(self):
        self.settle(self.reserve("1", role="security-reviewer"), "1")
        event = self.ledger._events()[-1]
        self.assertEqual(event["phase"], "security_review")
        with self.assertRaisesRegex(BudgetError, "max_usd_per_security_review_phase"):
            self.reserve(".01", role="security-reviewer")
        self.assertTrue(self.reserve(".1", role="code-reviewer"))

    def test_unknown_roles_share_implementation_envelope(self):
        self.reserve("1.5", role=None)
        with self.assertRaisesRegex(BudgetError, "max_usd_per_implementation_phase"):
            self.reserve(".6", role="implementer")

    def test_caps_cannot_be_disabled_or_increased_past_hard_caps(self):
        for value in (0, -1):
            with self.assertRaises(AgentError):
                budgets_from_config({"llm": {"budgets": {"max_usd_per_design_phase": value}}})
        limits = budgets_from_config({"llm": {"budgets": {"max_usd_per_design_phase": 999}}})
        self.assertEqual(limits["max_usd_per_design_phase"], Decimal("10"))
        defaults = budgets_from_config({})
        for key in ("max_usd_per_design_phase", "max_usd_per_code_review_phase",
                    "max_usd_per_security_review_phase"):
            self.assertEqual(defaults[key], Decimal("5"))
        self.assertEqual(defaults["max_usd_per_implementation_phase"], Decimal("12"))

    def test_review_and_design_phases_raise_only_to_hard_cap(self):
        raised = budgets_from_config({"llm": {"budgets": {
            "max_usd_per_code_review_phase": 8, "max_usd_per_security_review_phase": 50,
            "max_usd_per_design_phase": "9.5", "max_usd_per_implementation_phase": 50,
            "max_usd_per_run": 200}}})
        self.assertEqual(raised["max_usd_per_code_review_phase"], Decimal("8"))
        self.assertEqual(raised["max_usd_per_security_review_phase"], Decimal("10"))
        self.assertEqual(raised["max_usd_per_design_phase"], Decimal("9.5"))
        self.assertEqual(raised["max_usd_per_implementation_phase"], Decimal("12"))
        self.assertEqual(raised["max_usd_per_run"], Decimal("10"))
        limits = self.ledger.phase_limits([], "T-1", raised)
        self.assertEqual(limits["code_review"], Decimal("8"))
        self.assertEqual(limits["security_review"], Decimal("10"))
        self.assertEqual(limits["implementation"], Decimal("12"))
        # A hand-built limits map cannot bypass the hard cap either.
        forged = dict(raised, max_usd_per_code_review_phase=Decimal("40"),
                      max_usd_per_implementation_phase=Decimal("40"))
        limits = self.ledger.phase_limits([], "T-1", forged)
        self.assertEqual(limits["code_review"], Decimal("10"))
        self.assertEqual(limits["implementation"], Decimal("12"))
        self.assertTrue(self.ledger.reserve(projected=Decimal("7.5"), limits=raised,
            run_id="review-1", ticket="T-1", sprint="1", provider="anthropic",
            model="test", role="code-reviewer"))
        with self.assertRaisesRegex(BudgetError, "max_usd_per_code_review_phase"):
            self.ledger.reserve(projected=Decimal("0.6"), limits=raised, run_id="review-2",
                ticket="T-1", sprint="1", provider="anthropic", model="test",
                role="code-reviewer")
        self.assertTrue(self.ledger.reserve(projected=Decimal("0.5"), limits=raised,
            run_id="review-2", ticket="T-1", sprint="1", provider="anthropic",
            model="test", role="code-reviewer"))

    def test_raised_design_phase_cannot_enlarge_implementation_transfer(self):
        limits = budgets_from_config({"llm": {"budgets": {"max_usd_per_design_phase": 10}}})
        self.ledger.settle(self.ledger.reserve(projected=Decimal("1"), limits=limits,
            run_id="design", ticket="T-1", sprint="1", provider="anthropic", model="test",
            role="design-reviewer"), run_id="design", ticket="T-1", sprint="1",
            provider="anthropic", model="test", response_id="d", usage={},
            cost=Decimal("1"), role="design-reviewer")
        self.assertTrue(self.ledger.transfer_design_budget("T-1", limits, "pass"))
        effective = self.ledger.phase_limits(self.ledger.snapshot(), "T-1", limits)
        # Only the compiled $5 design default can move: 12 + (5 - 1).
        self.assertEqual(effective["implementation"], Decimal("16"))
        self.assertEqual(effective["design"], Decimal("6"))

    def test_budget_cap_violations_report_silent_reductions(self):
        from api_agent import budget_cap_violations
        self.assertEqual(budget_cap_violations({}), [])
        self.assertEqual(budget_cap_violations({"llm": {"budgets": {
            "max_usd_per_run": 5, "max_usd_per_code_review_phase": 8}}}), [])
        violations = budget_cap_violations({"llm": {"budgets": {
            "max_usd_per_run": 200, "max_usd_per_ticket": 400, "max_usd_per_sprint": 4000,
            "max_usd_per_code_review_phase": 50, "max_usd_per_implementation_phase": 13,
            "max_output_tokens_per_turn": 4096, "max_output_tokens_per_review_turn": 8192}}})
        by_key = {item["key"]: item for item in violations}
        self.assertEqual(by_key["max_usd_per_run"],
                         {"key": "max_usd_per_run", "configured": "200",
                          "effective": "10.00", "cap": "10.00"})
        self.assertEqual(by_key["max_usd_per_ticket"]["effective"], "30.00")
        self.assertEqual(by_key["max_usd_per_sprint"]["effective"], "300.00")
        self.assertEqual(by_key["max_usd_per_code_review_phase"]["cap"], "10")
        self.assertEqual(by_key["max_usd_per_implementation_phase"]["effective"], "12")
        # Derived output bounds follow their documented minimum, not a hard cap.
        self.assertEqual([item["key"] for item in violations], [
            "max_usd_per_run", "max_usd_per_ticket", "max_usd_per_sprint",
            "max_usd_per_implementation_phase", "max_usd_per_code_review_phase"])

    def test_snapshot_waits_for_complete_append(self):
        self.ledger.directory.mkdir(parents=True)
        entered = threading.Event()
        def read():
            entered.set()
            return self.ledger.snapshot()
        with self.ledger.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.ledger.path.write_text('{"kind":')
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(read)
                self.assertTrue(entered.wait(timeout=2))
                self.assertFalse(future.done())
                with self.ledger.path.open("a") as output:
                    output.write('"release"}\n')
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                self.assertEqual(future.result(timeout=2), [{"kind": "release"}])

    def test_legacy_usage_role_is_recovered_from_reservation(self):
        totals = self.ledger.phase_totals([
            {"kind": "reservation", "reservation_id": "r", "ticket": "T-1",
             "role": "code-reviewer", "projected_cost_usd": "1"},
            {"kind": "usage", "reservation_id": "r", "ticket": "T-1", "cost_usd": ".5"},
        ], "T-1")
        self.assertEqual(totals["code_review"]["spent_usd"], Decimal(".5"))
        self.assertEqual(totals["implementation"]["spent_usd"], 0)


if __name__ == "__main__":
    unittest.main()
