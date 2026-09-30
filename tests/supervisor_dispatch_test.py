#!/usr/bin/env python3
"""Deterministic dispatch, binding, replay, and refill coverage."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from supervisor_dispatch import (  # noqa: E402
    DispatchError,
    RESULT_SCHEMA,
    StaleResultError,
    SupervisorDispatcher,
)


class FakeAdapter:
    def __init__(self, root: Path, origin: str = "desktop") -> None:
        self.root = root
        self.origin = origin
        self.count = 0
        self.finished: list[dict] = []

    def reserve(self, sprint: str, ticket: str, run_ref: str) -> dict:
        self.count += 1
        return {
            "run_ref": run_ref,
            "attempt_token": f"attempt-{ticket}-{self.count}",
            "attempt_capability": f"cap-{ticket}-{self.count}",
            "attach_capability": f"attach-{ticket}-{self.count}",
        }

    def launch(self, sprint, ticket, reservation, prompt, output, result):
        return {"launch_evidence": f"launch-{ticket}-{self.count}"}

    def attach(self, sprint: str, ticket: str, launch_evidence: str) -> dict:
        invocation = f"invocation-{ticket}-{self.count}"
        tombstone = self.root / f"{invocation}.terminal.json"
        return {
            "worker_identity": {
                "kind": "execution_unit",
                "invocation_id": invocation,
                "tombstone_path": str(tombstone),
                "origin": self.origin,
            }
        }

    def finish(self, sprint, ticket, result, *, outcome=None, summary=None):
        applied = {
            "ticket": ticket,
            "outcome": outcome or result["outcome"],
            "summary": summary or result["summary"],
        }
        self.finished.append(applied)
        return {"ticket": ticket, "state": applied["outcome"]}

    def verify_completion(self, result: dict) -> dict:
        return {
            "url": result["pr"],
            "merged_at": "2026-09-30T00:00:00Z",
            "merge_commit": "a" * 40,
            "head_ref": result["branch"],
            "receipt_digest": "receipt",
        }

    def verify_work_identity(self, result: dict) -> dict | None:
        if not result.get("worktree"):
            return None
        return {
            "worktree": result["worktree"],
            "branch": result["branch"],
            "head": "b" * 40,
            "receipt_digest": "worktree-receipt",
        }


class SelectiveFailureAdapter(FakeAdapter):
    def reserve(self, sprint: str, ticket: str, run_ref: str) -> dict:
        if ticket == "PNP-1":
            raise DispatchError("ticket-local reservation rejection")
        return super().reserve(sprint, ticket, run_ref)


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="orka-dispatch-test-"))
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        self.runtime = self.repo / ".orchestration/.supervisor"
        self.runtime.mkdir(parents=True)
        self.adapter = FakeAdapter(self.temp)
        self.dispatcher = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def envelope(self, job: dict, outcome: str = "completed", **changes) -> dict:
        value = {
            "schema_version": RESULT_SCHEMA,
            "ticket": job["ticket"],
            "sprint": job["sprint"],
            "attempt_token": job["attempt_token"],
            "invocation_id": job["execution_identity"]["invocation_id"],
            "outcome": outcome,
            "summary": f"{outcome} result",
            "branch": "85-test" if outcome == "completed" else "",
            "worktree": str(self.repo) if outcome == "completed" else "",
            "pr": "https://github.test/pr/85" if outcome == "completed" else "",
            "evidence": {
                "origin": job["execution_identity"].get("origin"),
                "merge_receipt": {"merged": True},
            },
        }
        value.update(changes)
        return value

    def write_result(self, job: dict, value: dict) -> None:
        Path(job["paths"]["result"]).write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )

    def write_tombstone(self, job: dict) -> None:
        identity = job["execution_identity"]
        Path(identity["tombstone_path"]).write_text(
            json.dumps(
                {
                    "phase": "terminal",
                    "invocation_id": identity["invocation_id"],
                    "returncode": 0,
                }
            ),
            encoding="utf-8",
        )

    def test_fill_terminal_and_immediate_refill(self) -> None:
        jobs: dict[str, dict] = {}
        launched = self.dispatcher.fill("99", ["PNP-1", "PNP-2", "PNP-3"], jobs, 2)
        self.assertEqual([job["ticket"] for job in launched], ["PNP-1", "PNP-2"])
        first = launched[0]
        self.write_result(first, self.envelope(first))
        self.write_tombstone(first)
        applied = self.dispatcher.apply_process_exit(first)
        self.assertTrue(applied["applied"])
        # The supervisor forces an authoritative replan before refilling; the
        # completed ticket is therefore absent from the next launch list.
        refill = self.dispatcher.fill("99", ["PNP-3"], jobs, 2)
        self.assertEqual([job["ticket"] for job in refill], ["PNP-3"])

    def test_ticket_local_dispatch_rejection_does_not_stop_independent_work(
        self,
    ) -> None:
        adapter = SelectiveFailureAdapter(self.temp)
        dispatcher = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            adapter,
        )
        jobs: dict[str, dict] = {}
        launched = dispatcher.fill("99", ["PNP-1", "PNP-2"], jobs, 1)
        self.assertEqual([job["ticket"] for job in launched], ["PNP-2"])
        self.assertEqual(dispatcher.last_errors[0]["ticket"], "PNP-1")

    def test_duplicate_terminal_delivery_is_no_op(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1")
        self.write_result(job, self.envelope(job))
        first = self.dispatcher.apply_terminal(job)
        second = self.dispatcher.apply_terminal(job)
        self.assertTrue(first["applied"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.adapter.finished), 1)

    def test_stale_attempt_and_execution_results_fail_closed(self) -> None:
        for field, value in (
            ("attempt_token", "attempt-stale"),
            ("invocation_id", "invocation-stale"),
        ):
            with self.subTest(field=field):
                job = self.dispatcher.launch("99", f"PNP-{self.adapter.count + 1}")
                self.write_result(job, self.envelope(job, **{field: value}))
                with self.assertRaises(StaleResultError):
                    self.dispatcher.apply_terminal(job)
        self.assertEqual(self.adapter.finished, [])

    def test_malformed_result_moves_only_ticket_to_recovery(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1")
        self.write_result(job, {"not": "the terminal schema"})
        self.write_tombstone(job)
        result = self.dispatcher.apply_process_exit(job)
        self.assertEqual(result["terminal"]["event"], "worker_result_invalid")
        self.assertEqual(self.adapter.finished[-1]["outcome"], "recoverable")

    def test_process_exit_without_result_moves_ticket_to_recovery(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1")
        self.write_tombstone(job)
        result = self.dispatcher.apply_process_exit(job)
        self.assertEqual(result["terminal"]["event"], "worker_result_invalid")
        self.assertIn("without a structured", self.adapter.finished[-1]["summary"])

    def test_mixed_outcomes_map_through_contract_for_both_origins(self) -> None:
        cases = {
            "needs_repair": (
                "worker_needs_repair",
                {"pr_identity": "45", "review_ledger_digest": "review"},
            ),
            "external_blocked": (
                "worker_external_blocked",
                {"external_dependency_receipt": "dependency"},
            ),
            "operator_decision": (
                "worker_operator_decision",
                {
                    "decision_class": "product_or_security_policy",
                    "decision_question": "Choose policy",
                },
            ),
            "blocked": (
                "worker_irrecoverable",
                {"terminal_receipt": "terminal", "failure_class": "irrecoverable"},
            ),
        }
        for origin in ("desktop", "api"):
            adapter = FakeAdapter(self.temp, origin)
            dispatcher = SupervisorDispatcher(
                self.repo,
                self.runtime,
                ROOT / "contracts/supervisor-lifecycle-v1.json",
                adapter,
            )
            for index, (outcome, (event, evidence)) in enumerate(cases.items(), 1):
                job = dispatcher.launch("99", f"PNP-{index}")
                self.write_result(job, self.envelope(job, outcome, evidence=evidence))
                applied = dispatcher.apply_terminal(job)
                self.assertEqual(applied["terminal"]["event"], event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
