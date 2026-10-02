#!/usr/bin/env python3
"""Deterministic dispatch, binding, replay, and refill coverage."""

from __future__ import annotations

import json
import copy
import os
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
    TerminalAuthorityError,
)
from supervisor_planning import canonical_digest  # noqa: E402
from event_store import RepositoryBinding, TransactionalEventStore  # noqa: E402


class FakeAdapter:
    def __init__(self, root: Path, origin: str = "desktop") -> None:
        self.root = root
        self.origin = origin
        self.count = 0
        self.finished: list[dict] = []
        self.requeued: list[dict] = []
        self.identity_pid = 99999999

    def phase_capability_offer(self, contract: dict) -> dict:
        profile = (
            "api"
            if self.origin == "api"
            else "claude-desktop" if self.origin == "claude" else "codex-desktop"
        )
        profile_capability = (
            "provider-receipts" if profile == "api" else "desktop-subscription"
        )
        return {
            "kind": "capability_offer",
            "protocol_version": contract["protocol_version"],
            "adapter": profile,
            "adapter_version": "fixture-1",
            "supported_capabilities": [
                *contract["capabilities"]["mandatory"],
                profile_capability,
            ],
            "fresh_context_per_dispatch": True,
        }

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
                "containment": "test-supervisor",
                "pid": self.identity_pid,
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

    def requeue(self, sprint: str, job: dict, reason: str) -> dict:
        value = {"ticket": job["ticket"], "sprint": sprint, "reason": reason}
        self.requeued.append(value)
        return {"ticket": job["ticket"], "state": "pending"}

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


class UnsupportedBackendAdapter(FakeAdapter):
    def phase_capability_offer(self, contract: dict) -> dict:
        offer = super().phase_capability_offer(contract)
        offer["adapter"] = "remote"
        return offer


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
                    "spawned": True,
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

    def test_launch_records_a_fresh_protocol_phase_execution(self) -> None:
        first = self.dispatcher.launch("99", "PNP-1", phase="implement")
        second = self.dispatcher.launch("99", "PNP-2", phase="implement")

        first_execution = first["phase_execution"]["executions"][0]
        second_execution = second["phase_execution"]["executions"][0]
        self.assertEqual(first_execution["job"]["phase"], "implement")
        self.assertTrue(first_execution["job"]["fresh_context"])
        self.assertNotEqual(
            first_execution["identity"]["execution_unit_id"],
            second_execution["identity"]["execution_unit_id"],
        )
        self.assertNotIn("conversation_id", json.dumps(first["phase_execution"]))
        self.assertEqual(
            first_execution["attachment"]["identity"]["attempt_token"],
            first["attempt_token"],
        )
        prompt = Path(first["paths"]["prompt"]).read_text(encoding="utf-8")
        self.assertIn('"kind":"job"', prompt)
        self.assertIn('"phase":"implement"', prompt)

    def test_current_codex_claude_and_api_routes_share_one_backend_contract(
        self,
    ) -> None:
        for origin, profile in (
            ("desktop", "codex-desktop"),
            ("claude", "claude-desktop"),
            ("api", "api"),
        ):
            with self.subTest(origin=origin):
                adapter = FakeAdapter(self.temp, origin)
                dispatcher = SupervisorDispatcher(
                    self.repo,
                    self.runtime,
                    ROOT / "contracts/supervisor-lifecycle-v1.json",
                    adapter,
                )
                job = dispatcher.launch("99", f"PNP-{adapter.count + 1}")
                backend = job["execution_backend"]
                self.assertEqual(
                    backend["offer"]["backend_id"],
                    f"orka-current-route/{profile}",
                )
                phase_identity = job["phase_execution"]["active"]["identity"]
                self.assertEqual(
                    backend["launch_receipt"]["binding"],
                    {
                        field: phase_identity[field]
                        for field in dispatcher.execution_backend_contract[
                            "identity_fields"
                        ]
                    },
                )
                self.assertEqual(
                    backend["attachment_receipt"]["worker_identity"],
                    job["execution_identity"],
                )
                self.assertNotIn(
                    f"launch-{job['ticket']}-{adapter.count}",
                    json.dumps(backend),
                )
                heartbeat = dispatcher.heartbeat_execution(job)
                progress = dispatcher.progress_execution(job)
                self.assertEqual(heartbeat["observed_status"], "unknown")
                self.assertEqual(progress["observed_status"], "unknown")

    def test_unsupported_backend_fails_before_reservation(self) -> None:
        adapter = UnsupportedBackendAdapter(self.temp)
        dispatcher = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            adapter,
        )

        with self.assertRaisesRegex(DispatchError, "unsupported adapter profile"):
            dispatcher.launch("99", "PNP-1")

        self.assertEqual(adapter.count, 0)

    def test_transactional_dispatch_restores_exact_backend_after_restart(self) -> None:
        repository_id = "repository:dispatch-fixture"
        store = TransactionalEventStore(
            self.temp / "orka-state.sqlite3", writer_identity="supervisor:test"
        )
        self.addCleanup(store.close)
        store.bind_repository(
            RepositoryBinding(
                repository_id=repository_id,
                common_directory=str(self.repo / ".git"),
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
        first = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            event_store=store,
            writer_identity="supervisor:test",
            repository_id=repository_id,
        )
        job = first.launch("99", "PNP-1", phase="implement")
        binding_value = job["execution_backend"]["binding"]
        self.assertEqual(store.execution_record(binding_value)["state"], "attached")
        self.write_result(job, self.envelope(job))

        restarted = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            event_store=store,
            writer_identity="supervisor:test",
            repository_id=repository_id,
        )
        applied = restarted.apply_terminal(job)
        self.assertTrue(applied["applied"])
        self.assertEqual(store.execution_record(binding_value)["state"], "terminal")

    def test_adjacent_legacy_phase_job_completes_once_then_new_launch_uses_backend(
        self,
    ) -> None:
        job = self.dispatcher.launch("99", "PNP-1", phase="implement")
        job.pop("execution_backend")
        source_digest = canonical_digest(
            {
                field: job.get(field)
                for field in (
                    "ticket",
                    "sprint",
                    "run_ref",
                    "attempt_token",
                    "phase_execution",
                    "execution_identity",
                )
            }
        )
        job["legacy_execution_backend_provenance"] = {
            "schema_version": 1,
            "migration_id": f"execution-backend-v1:{source_digest}",
            "source_schema_version": 2,
            "source_job_digest": source_digest,
        }
        self.write_result(job, self.envelope(job))

        restarted = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
        )
        first = restarted.apply_terminal(job)
        second = restarted.apply_terminal(job)

        self.assertTrue(first["applied"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.adapter.finished), 1)
        self.assertEqual(job["phase_execution"]["status"], "terminal")
        self.assertNotIn("execution_backend", job)

        fresh = restarted.launch("99", "PNP-2", phase="implement")
        self.assertEqual(fresh["state"], "running")
        self.assertEqual(fresh["execution_backend"]["offer"]["protocol_version"], 1)

    def test_missing_backend_without_migration_provenance_fails_closed(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1", phase="implement")
        job.pop("execution_backend")
        self.write_result(job, self.envelope(job))

        with self.assertRaisesRegex(DispatchError, "binding"):
            self.dispatcher.apply_terminal(job)

        self.assertEqual(self.adapter.finished, [])
        self.assertEqual(job["terminal"], {})

    def test_persisted_backend_terminal_replays_after_controller_finish_crash(
        self,
    ) -> None:
        repository_id = "repository:terminal-replay-fixture"
        store = TransactionalEventStore(
            self.temp / "orka-terminal-replay.sqlite3",
            writer_identity="supervisor:test",
        )
        self.addCleanup(store.close)
        store.bind_repository(
            RepositoryBinding(
                repository_id=repository_id,
                common_directory=str(self.repo / ".git"),
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
        first = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            event_store=store,
            writer_identity="supervisor:test",
            repository_id=repository_id,
        )
        job = first.launch("99", "PNP-1", phase="implement")
        binding_value = job["execution_backend"]["binding"]
        self.write_result(job, self.envelope(job))
        self.write_tombstone(job)

        backend, coordinator, binding = first._restore_execution_backend(job)
        verified = self.envelope(job)
        verified["evidence"]["preserved_work_identity"] = (
            self.adapter.verify_work_identity(verified)
        )
        verified["evidence"]["merge_receipt"] = self.adapter.verify_completion(verified)
        backend.bind_terminal_result(verified)
        persisted_terminal = coordinator.terminal(binding)
        self.assertEqual(persisted_terminal["status"], "terminal")
        self.assertEqual(store.execution_record(binding_value)["state"], "terminal")
        self.assertEqual(self.adapter.finished, [])

        restarted = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            event_store=store,
            writer_identity="supervisor:test",
            repository_id=repository_id,
        )
        applied = restarted.apply_process_exit(job)
        duplicate = restarted.apply_process_exit(job)

        self.assertTrue(applied["applied"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.adapter.finished), 1)
        self.assertEqual(store.execution_record(binding_value)["state"], "terminal")

    def test_terminal_replay_rejects_changed_material_without_controller_mutation(
        self,
    ) -> None:
        repository_id = "repository:terminal-authority-fixture"
        store = TransactionalEventStore(
            self.temp / "orka-terminal-authority.sqlite3",
            writer_identity="supervisor:test",
        )
        self.addCleanup(store.close)
        store.bind_repository(
            RepositoryBinding(
                repository_id=repository_id,
                common_directory=str(self.repo / ".git"),
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
        for index, mutation in enumerate(("evidence", "summary"), start=1):
            with self.subTest(mutation=mutation):
                dispatcher = SupervisorDispatcher(
                    self.repo,
                    self.runtime,
                    ROOT / "contracts/supervisor-lifecycle-v1.json",
                    self.adapter,
                    event_store=store,
                    writer_identity="supervisor:test",
                    repository_id=repository_id,
                )
                job = dispatcher.launch("99", f"PNP-{index}", phase="implement")
                original = self.envelope(job)
                self.write_result(job, original)
                self.write_tombstone(job)
                backend, coordinator, binding = dispatcher._restore_execution_backend(job)
                verified = copy.deepcopy(original)
                verified["evidence"]["preserved_work_identity"] = (
                    self.adapter.verify_work_identity(verified)
                )
                verified["evidence"]["merge_receipt"] = (
                    self.adapter.verify_completion(verified)
                )
                backend.bind_terminal_result(verified)
                coordinator.terminal(binding)
                receipt_before = copy.deepcopy(store.execution_record(binding))

                changed = copy.deepcopy(original)
                if mutation == "evidence":
                    changed["evidence"]["unexpected"] = "different"
                else:
                    changed["summary"] = "changed after terminal persistence"
                self.write_result(job, changed)
                restarted = SupervisorDispatcher(
                    self.repo,
                    self.runtime,
                    ROOT / "contracts/supervisor-lifecycle-v1.json",
                    self.adapter,
                    event_store=store,
                    writer_identity="supervisor:test",
                    repository_id=repository_id,
                )

                with self.assertRaisesRegex(
                    TerminalAuthorityError, "differs from supervisor evidence"
                ):
                    restarted.apply_process_exit(job)

                self.assertEqual(self.adapter.finished, [])
                self.assertEqual(job["terminal"], {})
                self.assertEqual(job["phase_execution"]["status"], "running")
                self.assertEqual(store.execution_record(binding), receipt_before)

    def test_mutated_backend_metadata_is_rejected_on_restore(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1", phase="implement")
        job["execution_backend"]["launch_receipt"]["backend_metadata"][
            "worker_identity"
        ]["invocation_id"] = "replacement-invocation"
        self.write_result(job, self.envelope(job))

        with self.assertRaisesRegex(
            TerminalAuthorityError, "process identity digest"
        ):
            self.dispatcher.apply_terminal(job)
        self.assertEqual(self.adapter.finished, [])
        self.assertEqual(job["terminal"], {})
        self.assertEqual(job["phase_execution"]["status"], "running")

    def test_terminal_tombstone_without_mechanical_death_is_not_absence(self) -> None:
        self.adapter.identity_pid = os.getpid()
        job = self.dispatcher.launch("99", "PNP-1", phase="implement")
        self.write_tombstone(job)

        observation = self.dispatcher.observe_execution(job)

        self.assertEqual(observation["status"], "unknown")
        self.assertEqual(observation["reason"], "mechanical_absence_unverified")
        self.assertFalse(observation["replacement_safe"])

    def test_transactional_process_exit_without_result_fences_execution(self) -> None:
        repository_id = "repository:dispatch-exit-fixture"
        store = TransactionalEventStore(
            self.temp / "orka-exit-state.sqlite3", writer_identity="supervisor:test"
        )
        self.addCleanup(store.close)
        store.bind_repository(
            RepositoryBinding(
                repository_id=repository_id,
                common_directory=str(self.repo / ".git"),
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
        dispatcher = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            event_store=store,
            writer_identity="supervisor:test",
            repository_id=repository_id,
        )
        job = dispatcher.launch("99", "PNP-1", phase="implement")
        binding_value = job["execution_backend"]["binding"]
        self.write_tombstone(job)

        applied = dispatcher.apply_process_exit(job)

        self.assertEqual(applied["terminal"]["event"], "worker_result_invalid")
        self.assertEqual(store.execution_record(binding_value)["state"], "fenced")

        malformed = dispatcher.launch("99", "PNP-2", phase="implement")
        malformed_binding = malformed["execution_backend"]["binding"]
        self.write_result(malformed, {"not": "the terminal schema"})
        self.write_tombstone(malformed)
        malformed_result = dispatcher.apply_process_exit(malformed)
        self.assertEqual(malformed_result["terminal"]["event"], "worker_result_invalid")
        self.assertEqual(store.execution_record(malformed_binding)["state"], "fenced")

    def test_transactional_current_route_cancellation_persists_request_and_fence(
        self,
    ) -> None:
        repository_id = "repository:dispatch-cancel-fixture"
        store = TransactionalEventStore(
            self.temp / "orka-cancel-state.sqlite3", writer_identity="supervisor:test"
        )
        self.addCleanup(store.close)
        store.bind_repository(
            RepositoryBinding(
                repository_id=repository_id,
                common_directory=str(self.repo / ".git"),
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
        dispatcher = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            event_store=store,
            writer_identity="supervisor:test",
            repository_id=repository_id,
        )
        job = dispatcher.launch("99", "PNP-1", phase="implement")
        binding_value = job["execution_backend"]["binding"]

        result = dispatcher.cancel_execution(job, reason="operator drain")

        self.assertTrue(result["replacement_safe"])
        record = store.execution_record(binding_value)
        self.assertEqual(record["state"], "fenced")
        self.assertEqual(record["cancellation_receipt"]["status"], "requested")
        self.assertEqual(record["fence_receipt"]["status"], "fenced")

    def test_legacy_terminal_is_attributed_to_protocol_execution(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1", phase="implement")
        self.write_result(job, self.envelope(job))
        applied = self.dispatcher.apply_terminal(job)

        self.assertTrue(applied["applied"])
        self.assertEqual(job["phase_execution"]["status"], "terminal")
        phase_terminal = job["phase_execution"]["terminal"]["envelope"]
        self.assertEqual(phase_terminal["outcome"], "completed")
        self.assertEqual(
            phase_terminal["execution_unit_id"],
            job["phase_execution"]["executions"][0]["identity"]["execution_unit_id"],
        )

    def test_pre_phase_runtime_job_remains_terminal_compatible(self) -> None:
        job = self.dispatcher.launch("99", "PNP-1")
        job.pop("phase_execution")
        self.write_result(job, self.envelope(job))

        applied = self.dispatcher.apply_terminal(job)

        self.assertTrue(applied["applied"])
        self.assertEqual(applied["terminal"]["outcome"], "completed")

    def test_takeover_attributes_terminal_to_the_persisted_dispatch_fence(self) -> None:
        first_dispatcher = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            supervisor_fence="lease:1",
        )
        job = first_dispatcher.launch("99", "PNP-1", phase="implement")
        self.write_result(job, self.envelope(job))
        successor = SupervisorDispatcher(
            self.repo,
            self.runtime,
            ROOT / "contracts/supervisor-lifecycle-v1.json",
            self.adapter,
            supervisor_fence="lease:2",
        )

        applied = successor.apply_terminal(job)

        self.assertTrue(applied["applied"])
        self.assertEqual(
            job["phase_execution"]["terminal"]["envelope"]["supervisor_fence"],
            "lease:1",
        )

    def test_conflicting_candidate_is_skipped_without_reservation_or_reordering(
        self,
    ) -> None:
        exclusive = {
            "kind": "migration",
            "key": "primary",
            "units": 1,
            "capacity": 1,
            "source": "controller",
        }
        jobs: dict[str, dict] = {}
        first = self.dispatcher.fill(
            "99",
            ["PROJ-1"],
            jobs,
            3,
            heavy_capacity=3,
            claims_by_ticket={"PROJ-1": [exclusive]},
        )
        self.assertEqual([job["ticket"] for job in first], ["PROJ-1"])
        before_reservations = self.adapter.count
        second = self.dispatcher.fill(
            "99",
            ["PROJ-2", "PROJ-3"],
            jobs,
            3,
            heavy_capacity=3,
            claims_by_ticket={"PROJ-2": [exclusive]},
        )
        self.assertEqual([job["ticket"] for job in second], ["PROJ-3"])
        self.assertEqual(self.adapter.count, before_reservations + 1)
        self.assertEqual(self.dispatcher.last_skips[0]["ticket"], "PROJ-2")

        self.write_result(first[0], self.envelope(first[0]))
        self.dispatcher.apply_terminal(first[0])
        retry = self.dispatcher.fill(
            "99",
            ["PROJ-2"],
            jobs,
            3,
            heavy_capacity=3,
            claims_by_ticket={"PROJ-2": [exclusive]},
        )
        self.assertEqual([job["ticket"] for job in retry], ["PROJ-2"])

    def test_max_heavy_processes_is_independent_of_lane_capacity(self) -> None:
        jobs: dict[str, dict] = {}
        launched = self.dispatcher.fill(
            "99", ["PROJ-1", "PROJ-2", "PROJ-3"], jobs, 3, heavy_capacity=1
        )
        self.assertEqual([job["ticket"] for job in launched], ["PROJ-1"])
        self.assertEqual(
            [item["ticket"] for item in self.dispatcher.last_skips],
            ["PROJ-2", "PROJ-3"],
        )

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
        self.assertEqual(result["terminal"]["breaker"]["scope"], "ticket")
        self.assertEqual(result["terminal"]["breaker"]["subject"], job["ticket"])
        self.assertEqual(result["terminal"]["breaker"]["class_id"], "ticket_recovery")
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
                None,
            ),
            "external_blocked": (
                "worker_external_blocked",
                {
                    "external_dependency_receipt": {
                        "dependencies": ["EXT-9"],
                        "receipt": "jira-relation-receipt",
                    }
                },
                "ticket_external_wait",
            ),
            "operator_decision": (
                "worker_operator_decision",
                {
                    "decision_class": "product_or_security_policy",
                    "decision_question": "Choose policy",
                },
                "ticket_hard_decision",
            ),
            "blocked": (
                "worker_irrecoverable",
                {"terminal_receipt": "terminal", "failure_class": "irrecoverable"},
                "ticket_irrecoverable",
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
            for index, (outcome, (event, evidence, breaker_class)) in enumerate(
                cases.items(), 1
            ):
                job = dispatcher.launch("99", f"PNP-{index}")
                self.write_result(job, self.envelope(job, outcome, evidence=evidence))
                applied = dispatcher.apply_terminal(job)
                self.assertEqual(applied["terminal"]["event"], event)
                breaker = applied["terminal"].get("breaker")
                if breaker_class is None:
                    self.assertIsNone(breaker)
                else:
                    self.assertEqual(breaker["class_id"], breaker_class)
                    self.assertEqual(breaker["scope"], "ticket")
                    self.assertEqual(breaker["subject"], job["ticket"])

    def test_retry_wait_releases_lane_and_wakes_only_after_deadline(self) -> None:
        jobs: dict[str, dict] = {}
        first = self.dispatcher.fill("99", ["PNP-1"], jobs, 1)[0]
        self.write_result(
            first,
            self.envelope(
                first,
                "timeout_without_progress",
                evidence={"terminal_receipt": "terminal", "spend_receipt": "spend"},
            ),
        )
        self.write_tombstone(first)
        applied = self.dispatcher.apply_process_exit(first)
        self.assertEqual(applied["terminal"]["target_state"], "retry_wait")
        deadline = applied["terminal"]["retry_at"]
        # The parked retry consumes no lane, so independent work starts now.
        second = self.dispatcher.fill("99", ["PNP-2"], jobs, 1)
        self.assertEqual([job["ticket"] for job in second], ["PNP-2"])
        self.assertEqual(
            self.dispatcher.wake_due_retries(jobs, current_time=deadline - 0.01), []
        )
        awakened = self.dispatcher.wake_due_retries(jobs, current_time=deadline)
        self.assertEqual([job["ticket"] for job in awakened], ["PNP-1"])
        self.assertEqual(self.adapter.requeued[-1]["ticket"], "PNP-1")

    def test_retry_wakeup_requires_the_exact_ticket_breaker_binding(self) -> None:
        jobs: dict[str, dict] = {}
        job = self.dispatcher.fill("99", ["PNP-1"], jobs, 1)[0]
        self.write_result(
            job,
            self.envelope(
                job,
                "timeout_without_progress",
                evidence={"terminal_receipt": "terminal", "spend_receipt": "spend"},
            ),
        )
        self.write_tombstone(job)
        applied = self.dispatcher.apply_process_exit(job)
        deadline = applied["terminal"]["retry_at"]
        applied["terminal"]["breaker"]["subject"] = "PNP-OTHER"

        self.assertEqual(
            self.dispatcher.wake_due_retries(jobs, current_time=deadline), []
        )
        self.assertEqual(job["state"], "retry_wait")
        self.assertEqual(self.adapter.requeued, [])
        self.assertIn("exact ticket breaker", self.dispatcher.last_errors[-1]["error"])

    def test_failed_retry_wakeup_is_rescheduled_without_consuming_a_lane(self) -> None:
        jobs: dict[str, dict] = {}
        first = self.dispatcher.fill("99", ["PNP-1"], jobs, 1)[0]
        self.write_result(
            first,
            self.envelope(
                first,
                "timeout_without_progress",
                evidence={"terminal_receipt": "terminal", "spend_receipt": "spend"},
            ),
        )
        self.write_tombstone(first)
        applied = self.dispatcher.apply_process_exit(first)
        deadline = applied["terminal"]["retry_at"]

        def reject_requeue(_sprint, _job, _reason):
            raise DispatchError("worker absence proof unavailable")

        self.adapter.requeue = reject_requeue
        self.assertEqual(
            self.dispatcher.wake_due_retries(jobs, current_time=deadline), []
        )
        self.assertEqual(first["state"], "retry_wait")
        self.assertEqual(
            first["terminal"]["retry_at"],
            deadline + self.dispatcher.retry_delay_seconds,
        )
        second = self.dispatcher.fill("99", ["PNP-2"], jobs, 1)
        self.assertEqual([job["ticket"] for job in second], ["PNP-2"])

    def test_external_and_decision_classification_fail_closed(self) -> None:
        external = self.dispatcher.launch("99", "PNP-1")
        self.write_result(
            external,
            self.envelope(
                external,
                "external_blocked",
                evidence={"external_dependency_receipt": "unstructured"},
            ),
        )
        applied = self.dispatcher.apply_terminal(external)
        self.assertEqual(applied["terminal"]["event"], "worker_result_invalid")

        decision = self.dispatcher.launch("99", "PNP-2")
        self.write_result(
            decision,
            self.envelope(
                decision,
                "operator_decision",
                evidence={
                    "decision_class": "ordinary_tool_failure",
                    "decision_question": "Retry it?",
                },
            ),
        )
        applied = self.dispatcher.apply_terminal(decision)
        self.assertEqual(applied["terminal"]["event"], "worker_result_invalid")

    def test_repair_and_recovery_are_requeued_without_blocking_each_other(self) -> None:
        jobs: dict[str, dict] = {}
        repair = self.dispatcher.fill("99", ["PNP-1"], jobs, 2)[0]
        recovery = self.dispatcher.fill("99", ["PNP-2"], jobs, 2)[0]
        for job, outcome, evidence in (
            (
                repair,
                "needs_repair",
                {"pr_identity": "45", "review_ledger_digest": "ledger"},
            ),
            (
                recovery,
                "recoverable",
                {
                    "terminal_receipt": "terminal",
                    "preserved_work_identity": "worktree",
                },
            ),
        ):
            self.write_result(job, self.envelope(job, outcome, evidence=evidence))
            self.write_tombstone(job)
            self.dispatcher.apply_process_exit(job)
        prepared = self.dispatcher.prepare_continuations(
            "99", {"repair": ["PNP-1"], "recovery": ["PNP-2"]}, jobs
        )
        self.assertEqual([job["ticket"] for job in prepared], ["PNP-1", "PNP-2"])
        self.assertEqual(
            [item["ticket"] for item in self.adapter.requeued], ["PNP-1", "PNP-2"]
        )
        self.assertTrue(all(job["state"] == "queued" for job in prepared))


if __name__ == "__main__":
    unittest.main(verbosity=2)
