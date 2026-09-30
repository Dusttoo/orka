#!/usr/bin/env python3
"""Resource exclusion, capacity, replay, and legacy migration coverage."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from supervisor_admission import (  # noqa: E402
    AdmissionError,
    CLAIM_SCHEMA,
    RESOURCE_KINDS,
    automatic_claims,
    claim_set_digest,
    conflicts,
    migrate_legacy_active_jobs,
    normalize_claims,
    release_claims,
    validate_persisted_jobs,
)


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = ROOT

    def claims(self, ticket: str, additional=None, *, heavy=2):
        return automatic_claims(
            self.repository,
            ticket,
            concurrency=3,
            heavy_capacity=heavy,
            route_identity="desktop-codex",
            additional=additional,
        )

    def job(self, ticket: str, claims, state="running"):
        return {
            "ticket": ticket,
            "state": state,
            "resource_claim_schema": CLAIM_SCHEMA,
            "resource_claims": claims,
            "resource_claim_digest": claim_set_digest(claims),
        }

    def exclusive(self, kind: str, key: str):
        return {
            "kind": kind,
            "key": key,
            "units": 1,
            "capacity": 1,
            "source": "controller",
        }

    def test_same_cycle_exclusions_block_without_consuming_the_candidate(self) -> None:
        first = self.claims("PROJ-1", [self.exclusive("migration", "primary")])
        second = self.claims("PROJ-2", [self.exclusive("migration", "primary")])
        jobs = {"run-1": self.job("PROJ-1", first)}
        blocked = conflicts(second, jobs)
        self.assertEqual(
            [(item["kind"], item["key"]) for item in blocked],
            [("migration", "primary")],
        )
        self.assertEqual(list(jobs), ["run-1"])

    def test_pr_worktree_provider_and_visual_qa_conflicts_are_typed(self) -> None:
        for kind in ("pr", "worktree", "provider_route", "visual_qa"):
            with self.subTest(kind=kind):
                claim = self.exclusive(kind, "shared")
                first = self.claims("PROJ-1", [claim])
                second = self.claims("PROJ-2", [claim])
                blocked = conflicts(second, {"run-1": self.job("PROJ-1", first)})
                self.assertTrue(any(item["kind"] == kind for item in blocked))

    def test_heavy_capacity_releases_after_terminal_transition(self) -> None:
        first = self.claims("PROJ-1", heavy=1)
        second = self.claims("PROJ-2", heavy=1)
        job = self.job("PROJ-1", first)
        jobs = {"run-1": job}
        self.assertTrue(conflicts(second, jobs))
        job["state"] = "completed"
        receipt = release_claims(job, observed_at=10.0)
        self.assertEqual(conflicts(second, jobs), [])
        self.assertEqual(release_claims(job, observed_at=20.0), receipt)

    def test_malformed_duplicate_and_capacity_inconsistent_claims_fail_closed(self) -> None:
        duplicate = self.claims("PROJ-1")
        duplicate.append(copy.deepcopy(duplicate[0]))
        with self.assertRaises(AdmissionError):
            normalize_claims(duplicate)

        first = self.claims("PROJ-1", heavy=1)
        second = self.claims("PROJ-2", heavy=2)
        with self.assertRaises(AdmissionError):
            conflicts(second, {"run-1": self.job("PROJ-1", first)})

    def test_restart_reconstructs_claims_and_legacy_jobs_migrate_once(self) -> None:
        jobs = {"old-run": {"ticket": "PROJ-1", "state": "running"}}

        def factory(ticket):
            return self.claims(ticket, heavy=1)

        self.assertEqual(migrate_legacy_active_jobs(jobs, factory), ["old-run"])
        self.assertEqual(migrate_legacy_active_jobs(jobs, factory), [])
        restored = copy.deepcopy(jobs)
        blocked = conflicts(self.claims("PROJ-2", heavy=1), restored)
        self.assertTrue(any(item["kind"] == "heavy_process" for item in blocked))

    def test_tampered_claim_digest_is_rejected(self) -> None:
        claims = self.claims("PROJ-1")
        job = self.job("PROJ-1", claims)
        job["resource_claims"][0]["key"] = "changed"
        with self.assertRaises(AdmissionError):
            conflicts(self.claims("PROJ-2"), {"run-1": job})

    def test_restart_validation_allows_legacy_but_rejects_partial_modern_state(self) -> None:
        validate_persisted_jobs(
            {"old-run": {"ticket": "PROJ-1", "state": "running"}}
        )
        with self.assertRaises(AdmissionError):
            validate_persisted_jobs(
                {
                    "bad-run": {
                        "ticket": "PROJ-1",
                        "state": "running",
                        "resource_claim_schema": CLAIM_SCHEMA,
                    }
                }
            )

    def test_machine_readable_contract_matches_runtime(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/resource-claims-v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["claim_schema"], CLAIM_SCHEMA)
        self.assertEqual(set(contract["resource_kinds"]), RESOURCE_KINDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
