#!/usr/bin/env python3
"""Contract and fixture coverage for deterministic recovery eligibility."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from recovery_eligibility import (  # noqa: E402
    PRESERVED_FIELDS,
    PROFILES,
    REASONS,
    SCHEMA,
    VERDICTS,
    WORK_KINDS,
    RecoveryEvidenceError,
    evaluate_recovery,
)


class RecoveryEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            (ROOT / "contracts/recovery-eligibility-v1.json").read_text()
        )
        cls.fixtures = json.loads(
            (ROOT / "tests/fixtures/recovery-eligibility-v1.json").read_text()
        )

    def test_machine_contract_matches_runtime(self) -> None:
        self.assertEqual(self.contract["schema"], SCHEMA)
        self.assertEqual(set(self.contract["profiles"]), set(PROFILES))
        self.assertEqual(set(self.contract["work_kinds"]), set(WORK_KINDS))
        self.assertEqual(set(self.contract["verdicts"]), set(VERDICTS))
        self.assertEqual(
            self.contract["reason_codes"],
            {key: value[0] for key, value in REASONS.items()},
        )
        self.assertEqual(
            self.contract["preservation_invariants"], list(PRESERVED_FIELDS)
        )

    def test_all_machine_readable_fixtures_are_deterministic(self) -> None:
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture["name"]):
                first = evaluate_recovery(fixture["evidence"])
                second = evaluate_recovery(copy.deepcopy(fixture["evidence"]))
                self.assertEqual(first, second)
                self.assertEqual(first["verdict"], fixture["expected_verdict"])
                self.assertEqual(first["reason_codes"], fixture["expected_reasons"])
                self.assertEqual(first["eligible"], first["verdict"] == "eligible")

    def test_multiple_failures_are_reported_in_one_evaluation(self) -> None:
        evidence = copy.deepcopy(self.fixtures[0]["evidence"])
        evidence["execution"].update(
            status="unknown", identity_bound=False, descendants="unknown"
        )
        evidence["provider"]["state"] = "ambiguous"
        evidence["work"].update(
            binding_complete=False, worktree="unknown", revision_match=False
        )
        evidence["history"]["preserved_fields"] = []
        result = evaluate_recovery(evidence)
        self.assertEqual(result["verdict"], "operator_action")
        self.assertEqual(
            result["reason_codes"],
            [
                "execution_unknown",
                "execution_identity_mismatch",
                "descendants_unknown",
                "provider_ambiguous",
                "binding_incomplete",
                "worktree_unknown",
                "revision_mismatch",
                "history_incomplete",
            ],
        )

    def test_malformed_evidence_fails_closed(self) -> None:
        evidence = copy.deepcopy(self.fixtures[0]["evidence"])
        evidence["execution"]["identity_bound"] = "yes"
        with self.assertRaisesRegex(RecoveryEvidenceError, "must be boolean"):
            evaluate_recovery(evidence)
        evidence = copy.deepcopy(self.fixtures[0]["evidence"])
        evidence["schema"] = "future"
        with self.assertRaisesRegex(RecoveryEvidenceError, SCHEMA):
            evaluate_recovery(evidence)

    def test_preserved_pr_requires_a_clean_bound_worktree(self) -> None:
        evidence = copy.deepcopy(self.fixtures[0]["evidence"])
        evidence["work"].update(kind="preserved_pr", worktree="not_applicable")
        result = evaluate_recovery(evidence)
        self.assertEqual(result["verdict"], "operator_action")
        self.assertEqual(result["reason_codes"], ["worktree_missing"])

    def test_evaluation_never_mutates_evidence(self) -> None:
        evidence = copy.deepcopy(self.fixtures[0]["evidence"])
        original = copy.deepcopy(evidence)
        evaluate_recovery(evidence)
        self.assertEqual(evidence, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
