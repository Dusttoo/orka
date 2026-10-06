#!/usr/bin/env python3
"""Integration coverage for the host-owned supervisor lifecycle slice."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / "scripts/sprint-supervisor.py"
sys.path.insert(0, str(ROOT / "scripts"))

from supervisor_planning import (  # noqa: E402
    classify_cycle,
    due_health_roles,
    planning_cycle,
)
from supervisor_state import (  # noqa: E402
    SupervisorStateError,
    migrate_supervisor_state,
)

SUPERVISOR_SPEC = __import__("importlib.util").util.spec_from_file_location(
    "sprint_supervisor", SUPERVISOR
)
sprint_supervisor = __import__("importlib.util").util.module_from_spec(SUPERVISOR_SPEC)
SUPERVISOR_SPEC.loader.exec_module(sprint_supervisor)


class FakePlanningAdapter:
    def __init__(self, plan: dict, summary: dict):
        self.plan_value = plan
        self.summary_value = summary
        self.probes: list[str] = []
        self.reconciled: list[str] = []

    def health_probe(self, role: str) -> dict:
        self.probes.append(role)
        return {"state": "healthy"}

    def synchronize(self) -> dict:
        return {
            "checkpoint": "/tmp/checkpoint.json",
            "sprint": {"id": "99", "name": "Test Sprint"},
            "tickets": 2,
        }

    def plan(self, sprint: str) -> dict:
        assert sprint == "99"
        return self.plan_value

    def summary(self, sprint: str) -> dict:
        assert sprint == "99"
        return self.summary_value

    def reconcile_preserved_pr(self, sprint: str, ticket: str) -> dict:
        assert sprint == "99"
        self.reconciled.append(ticket)
        self.plan_value = {
            **self.plan_value,
            "pr_reconciliation": [],
            "launch": [ticket],
            "autonomous_work_remaining": True,
        }
        return {
            "ticket": ticket,
            "state": "needs_repair",
            "recovery_binding": {"kind": "preserved_pr", "ticket": ticket},
        }


class PlanningLoopTests(unittest.TestCase):
    def budget(self) -> dict:
        return {
            "spent_usd": "0",
            "reserved_usd": "0",
            "projected_usd": "0",
            "absolute_ceiling_usd": "20",
            "digest": "budget-receipt",
            "exhausted": False,
        }

    def test_independent_blocked_ticket_does_not_hide_ready_work(self) -> None:
        plan = {
            "sprint": {"id": "99"},
            "launch": ["PNP-2"],
            "waiting": [{"key": "PNP-1", "reasons": ["dependency blocked"]}],
            "ticket_breakers": [
                {"subject": "PNP-1", "class_id": "ticket_external_wait"}
            ],
            "route_breakers": [
                {"subject": "route-a", "class_id": "route_transient_hold"}
            ],
            "autonomous_work_remaining": True,
        }
        result = classify_cycle(
            plan,
            {"sprint_complete": False},
            self.budget(),
            current_time=100,
            sync_interval=60,
        )
        self.assertEqual(result["plan"]["launch"], ["PNP-2"])
        self.assertFalse(result["autonomous_work_exhausted"])
        self.assertFalse(result["all_routes_unavailable"])
        self.assertEqual(result["ticket_breakers"], plan["ticket_breakers"])
        self.assertEqual(result["route_breakers"], plan["route_breakers"])

    def test_provider_cooldown_is_waitable_and_sets_exact_deadline(self) -> None:
        plan = {
            "sprint": {"id": "99"},
            "provider_holds": [
                {"provider": "openai", "state": "rate_limited", "retry_at": 125}
            ],
            "health_probes": [
                {"provider": "openai", "role": "sprint-worker", "retry_at": 125}
            ],
            "required_roles": ["sprint-worker"],
            "route_blocked_roles": ["sprint-worker"],
            "route_breakers": [
                {
                    "source_id": "provider_rate_limited",
                    "class_id": "route_transient_hold",
                    "subject": "openai-worker",
                    "role": "sprint-worker",
                }
            ],
            "autonomous_work_remaining": True,
        }
        result = classify_cycle(
            plan,
            {"sprint_complete": False},
            self.budget(),
            current_time=100,
            sync_interval=60,
        )
        self.assertEqual(result["next_wake_epoch"], 125)
        self.assertEqual(result["wait_reason"], "durable-deadline")
        self.assertTrue(result["all_routes_unavailable"])
        self.assertFalse(result["autonomous_work_exhausted"])
        self.assertEqual(
            result["global_breakers"][0]["source_id"], "all_routes_unavailable"
        )
        self.assertEqual(due_health_roles(plan, 125), ["sprint-worker"])

    def test_one_held_route_does_not_pause_work_with_an_eligible_route(self) -> None:
        plan = {
            "sprint": {"id": "99"},
            "scope": ["PNP-2"],
            "required_roles": ["sprint-worker", "ticket-scoper"],
            "route_blocked_roles": ["sprint-worker"],
            "provider_holds": [{"role": "sprint-worker", "state": "transport"}],
            "autonomous_work_remaining": True,
        }
        result = classify_cycle(
            plan,
            {"sprint_complete": False},
            self.budget(),
            current_time=100,
        )
        self.assertFalse(result["all_routes_unavailable"])
        self.assertEqual(result["plan"]["scope"], ["PNP-2"])
        self.assertEqual(result["global_breakers"], [])

    def test_soft_pressure_is_evidence_bearing_without_cancelling_work(self) -> None:
        plan = {
            "sprint": {"id": "99"},
            "concurrency_max": 2,
            "running": ["PNP-1", "PNP-2"],
            "work_in_progress": {
                "fresh_launch_paused": True,
                "count": 2,
                "limit": 2,
            },
            "autonomous_work_remaining": True,
        }
        result = classify_cycle(
            plan,
            {"sprint_complete": False},
            self.budget(),
            current_time=100,
        )
        self.assertEqual(result["running"], ["PNP-1", "PNP-2"])
        self.assertEqual(
            {item["source_id"] for item in result["pressure_breakers"]},
            {"unfinished_pr_pressure", "lane_capacity_pressure"},
        )
        self.assertFalse(result["all_routes_unavailable"])

    def test_true_exhaustion_is_distinct_from_authenticated_completion(self) -> None:
        exhausted = classify_cycle(
            {"sprint": {"id": "99"}, "autonomous_work_remaining": False},
            {"sprint_complete": False},
            self.budget(),
            current_time=100,
        )
        self.assertTrue(exhausted["autonomous_work_exhausted"])
        self.assertFalse(exhausted["sprint_complete"])

        completed = classify_cycle(
            {"sprint": {"id": "99"}, "autonomous_work_remaining": False},
            {"sprint_complete": True},
            self.budget(),
            current_time=100,
        )
        self.assertTrue(completed["sprint_complete"])

    def test_replayed_evidence_produces_the_same_plan_digest(self) -> None:
        repository = Path(tempfile.mkdtemp(prefix="orka-planning-test-"))
        try:
            config = repository / ".orchestration/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "schema_version: 1\nllm:\n  budgets:\n    max_usd_per_sprint: 20\n",
                encoding="utf-8",
            )
            plan = {
                "sprint": {"id": "99"},
                "launch": ["PNP-2"],
                "autonomous_work_remaining": True,
            }
            adapter = FakePlanningAdapter(plan, {"sprint_complete": False})
            first = planning_cycle(
                adapter,
                repository,
                {},
                current_time=100,
                sync_interval=60,
            )
            second = planning_cycle(
                adapter,
                repository,
                first,
                current_time=101,
                sync_interval=60,
            )
            self.assertEqual(first["plan_digest"], second["plan_digest"])
            self.assertEqual(
                first["sync_receipt_digest"], second["sync_receipt_digest"]
            )
        finally:
            shutil.rmtree(repository, ignore_errors=True)

    def test_planning_cycle_reconciles_preserved_pr_then_replans(self) -> None:
        repository = Path(tempfile.mkdtemp(prefix="orka-planning-test-"))
        try:
            config = repository / ".orchestration/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "schema_version: 1\nllm:\n  budgets:\n    max_usd_per_sprint: 20\n",
                encoding="utf-8",
            )
            adapter = FakePlanningAdapter(
                {
                    "sprint": {"id": "99"},
                    "pr_reconciliation": ["PNP-40"],
                    "autonomous_work_remaining": True,
                },
                {"sprint_complete": False},
            )
            result = planning_cycle(
                adapter,
                repository,
                {},
                current_time=100,
                sync_interval=60,
            )
            self.assertEqual(adapter.reconciled, ["PNP-40"])
            self.assertEqual(result["plan"]["pr_reconciliation"], [])
            self.assertEqual(result["plan"]["launch"], ["PNP-40"])
            self.assertEqual(result["reconciled_preserved_prs"][0]["ticket"], "PNP-40")
        finally:
            shutil.rmtree(repository, ignore_errors=True)

    def test_status_separates_queue_retry_parking_and_active_lanes(self) -> None:
        state = {
            "repository": "/tmp/repo",
            "lifecycle_state": "active",
            "lease": {"id": "lease", "generation": 1},
            "process": {},
            "updated_at": "now",
            "last_event": "controller_plan_updated",
            "planning": {
                "enabled": True,
                "allocation_cursor": 4,
                "lane_allocation": {
                    "next_cursor": 5,
                    "selections": [
                        {
                            "slot": 1,
                            "ticket": "PNP-5",
                            "class": "fresh",
                            "action": "launch",
                            "reason": "weighted-fair-share:fresh",
                        }
                    ],
                },
                "plan": {
                    "launch": ["PNP-5"],
                    "scope": [],
                    "decomposition": ["PNP-6"],
                    "repair": [],
                    "recovery": [],
                },
                "retry_waiting": [{"key": "PNP-2", "retry_at": 200}],
                "ticket_breakers": [
                    {"subject": "PNP-3", "class_id": "ticket_hard_decision"}
                ],
                "route_breakers": [
                    {"subject": "route-a", "class_id": "route_transient_hold"}
                ],
                "pressure_breakers": [
                    {
                        "source_id": "unfinished_pr_pressure",
                        "subject": "99",
                        "class_id": "sprint_pressure",
                    }
                ],
                "active_global_breaker": {
                    "source_id": "unfinished_pr_pressure",
                    "generation": "1:1:pressure",
                },
                "decision_queue": [{"key": "PNP-3", "state": "operator_decision"}],
                "waiting": [{"key": "PNP-4", "reasons": ["dependency"]}],
            },
            "dispatch": {
                "jobs": {
                    "run-1": {"ticket": "PNP-1", "state": "running"},
                    "run-2": {"ticket": "PNP-2", "state": "retry_wait"},
                    "run-3": {"ticket": "PNP-3", "state": "parked_decision"},
                    "run-4": {"ticket": "PNP-7", "state": "completed"},
                },
                "launch_count": 3,
                "terminal_count": 2,
            },
        }
        with patch.object(sprint_supervisor, "process_status", return_value="live"):
            result = sprint_supervisor.status_response(state)
        self.assertEqual(result["dispatch"]["active_jobs"], 1)
        self.assertEqual(result["dispatch"]["terminal_jobs"], 1)
        self.assertEqual(result["dispatch"]["queued"], ["PNP-5", "PNP-6"])
        self.assertEqual(result["dispatch"]["retrying"], ["PNP-2"])
        self.assertEqual(result["dispatch"]["parked"], ["PNP-3"])
        self.assertEqual(result["dispatch"]["blocked"], ["PNP-4"])
        self.assertEqual(result["dispatch"]["active"], ["PNP-1"])
        self.assertEqual(result["dispatch"]["route_held"], ["route-a"])
        self.assertEqual(
            result["dispatch"]["pressure_limited"], ["unfinished_pr_pressure"]
        )
        self.assertEqual(result["dispatch"]["globally_paused"], {})
        self.assertEqual(result["dispatch"]["terminal"], ["PNP-7"])
        self.assertEqual(
            set(result["dispatch"]["categories"]),
            {
                "queued",
                "active",
                "retrying",
                "parked",
                "route_held",
                "pressure_limited",
                "globally_paused",
                "blocked",
                "terminal",
            },
        )
        self.assertEqual(
            result["dispatch"]["lane_allocation"]["selections"][0]["reason"],
            "weighted-fair-share:fresh",
        )
        self.assertEqual(result["dispatch"]["ticket_breakers"][0]["subject"], "PNP-3")
        self.assertEqual(result["dispatch"]["route_breakers"][0]["subject"], "route-a")
        self.assertEqual(
            result["dispatch"]["active_global_breaker"]["generation"],
            "1:1:pressure",
        )


class GlobalBreakerTests(unittest.TestCase):
    def state(self) -> dict:
        return {
            "repository": "/tmp/orka-global-breaker-test",
            "lifecycle_state": "active",
            "lease": {"id": "lease-1", "generation": 3},
            "process": {"pid": 123, "start_fingerprint": "process-1"},
            "updated_at": "before",
            "last_event": "preflight_succeeded",
            "planning": {},
            "dispatch": {"jobs": {"run-1": {"ticket": "PNP-1", "state": "running"}}},
            "history": [],
            "requests": [],
        }

    def test_pressure_transition_and_clear_are_generation_bound_and_idempotent(
        self,
    ) -> None:
        _contract, lifecycle, _digest = sprint_supervisor.contract()
        state = self.state()
        breaker = sprint_supervisor.BREAKER_RUNTIME.sprint_record(
            "unfinished_pr_pressure",
            sprint="99",
            evidence={"pressure_class": "wip", "capacity_snapshot": {"used": 3}},
        )
        evidence = {
            "pressure_class": "unfinished_pr_pressure",
            "capacity_snapshot": "capacity-digest",
        }
        self.assertTrue(
            sprint_supervisor.transition_global_breaker(
                state, lifecycle, breaker, "sprint_pressure_applied", evidence
            )
        )
        generation = state["planning"]["active_global_breaker"]["generation"]
        self.assertEqual(state["lifecycle_state"], "degraded")
        self.assertEqual(state["dispatch"]["jobs"]["run-1"]["state"], "running")
        history_length = len(state["history"])
        self.assertFalse(
            sprint_supervisor.transition_global_breaker(
                state, lifecycle, breaker, "sprint_pressure_applied", evidence
            )
        )
        self.assertEqual(len(state["history"]), history_length)
        sprint_supervisor.clear_global_breaker(
            state,
            lifecycle,
            "sprint_pressure_cleared",
            {"capacity_snapshot": "clear-digest"},
        )
        self.assertEqual(state["lifecycle_state"], "active")
        self.assertEqual(
            state["planning"]["global_breaker_history"][-1]["generation"],
            generation,
        )

    def test_hard_budget_requires_resolution_of_its_exact_generation(self) -> None:
        _contract, lifecycle, _digest = sprint_supervisor.contract()
        state = self.state()
        breaker = sprint_supervisor.BREAKER_RUNTIME.sprint_record(
            "max_usd_per_sprint",
            sprint="99",
            evidence={"budget_receipt": "budget-1"},
        )
        sprint_supervisor.transition_global_breaker(
            state,
            lifecycle,
            breaker,
            "hard_sprint_budget_exhausted",
            {"budget_receipt": "budget-1", "absolute_ceiling": "20"},
        )
        state["planning"]["pause_cause"] = "hard_sprint_budget_exhausted"
        with self.assertRaisesRegex(
            sprint_supervisor.SupervisorError, "cannot override"
        ):
            sprint_supervisor.apply_control(
                state,
                lifecycle,
                {"command": "resume", "request_id": "resume-1", "reason": ""},
            )
        active = state["planning"]["active_global_breaker"]
        generation = active["generation"]
        active["resolution_receipt"] = "budget-2"
        active["resolved_at"] = "now"
        response, should_stop = sprint_supervisor.apply_control(
            state,
            lifecycle,
            {"command": "resume", "request_id": "resume-2", "reason": ""},
        )
        self.assertFalse(should_stop)
        self.assertEqual(response["lifecycle_state"], "active")
        self.assertEqual(
            state["planning"]["global_breaker_history"][-1]["generation"],
            generation,
        )
        self.assertEqual(state["planning"]["active_global_breaker"], {})
        self.assertEqual(state["dispatch"]["jobs"]["run-1"]["state"], "running")


class BreakerStateMigrationTests(unittest.TestCase):
    def legacy(self) -> dict:
        return {
            "schema_version": 1,
            "contract_id": "orka.supervisor-lifecycle",
            "contract_schema_version": 1,
            "repository": "/tmp/repo",
            "lifecycle_state": "active",
            "last_event": "controller_plan_updated",
            "started_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T01:00:00+00:00",
            "process": {"pid": 123, "start_fingerprint": "old-process"},
            "lease": {"id": "old-lease", "generation": 4},
            "history": [{"event": "preserved-history", "evidence": {"attempt": 2}}],
            "requests": [{"request_id": "preserved-request"}],
            "runtime_fingerprint": "old-runtime",
            "contract_digest": "old-contract",
            "planning": {
                "sprint": {"id": "99"},
                "decision_queue": [
                    {
                        "key": "PNP-1",
                        "state": "operator_decision",
                        "reasons": ["hard review decision"],
                        "pr": 40,
                    }
                ],
                "provider_holds": [
                    {
                        "state": "rate_limited",
                        "provider": "openai",
                        "role": "sprint-worker",
                        "route_identity": "openai:sprint-worker",
                        "retry_at": 100,
                    }
                ],
                "spend": {"PNP-1": {"spent_usd": "12.30"}},
                "dependency_graph": {"PNP-2": ["PNP-1"]},
                "review_ledgers": {"PNP-1": "sha256:review"},
            },
            "dispatch": {
                "jobs": {
                    "run-1": {
                        "ticket": "PNP-1",
                        "state": "parked_decision",
                        "attempt_token": "attempt-2",
                        "pr": 40,
                    }
                }
            },
        }

    def migrate(self, state: dict) -> tuple[dict, bool]:
        return migrate_supervisor_state(
            state,
            target_runtime_fingerprint="new-runtime",
            target_contract_digest="new-contract",
        )

    def test_migration_is_once_only_and_preserves_history_and_bindings(self) -> None:
        legacy = self.legacy()
        migrated, changed = self.migrate(legacy)
        self.assertTrue(changed)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["dispatch"], legacy["dispatch"])
        self.assertEqual(migrated["requests"], legacy["requests"])
        self.assertEqual(migrated["history"], legacy["history"])
        self.assertEqual(migrated["planning"]["spend"], legacy["planning"]["spend"])
        self.assertEqual(
            migrated["planning"]["dependency_graph"],
            legacy["planning"]["dependency_graph"],
        )
        self.assertEqual(
            migrated["planning"]["review_ledgers"],
            legacy["planning"]["review_ledgers"],
        )
        self.assertEqual(
            migrated["planning"]["ticket_breakers"][0]["class_id"],
            "ticket_hard_decision",
        )
        self.assertEqual(
            migrated["planning"]["route_breakers"][0]["class_id"],
            "route_transient_hold",
        )
        repeated, changed_again = self.migrate(copy.deepcopy(migrated))
        self.assertFalse(changed_again)
        self.assertEqual(repeated, migrated)

    def test_schema_two_migration_proves_adjacent_backendless_execution(self) -> None:
        schema_three, _changed = self.migrate(self.legacy())
        schema_two = copy.deepcopy(schema_three)
        schema_two["schema_version"] = 2
        schema_two.pop("execution_backend_migration", None)
        schema_two["dispatch"]["jobs"] = {
            "run-old": {
                "ticket": "PNP-2",
                "state": "running",
                "phase_execution": {"schema_version": "orka.phase-execution-state/v1"},
                "execution_identity": {"invocation_id": "invocation-old"},
            }
        }

        migrated, changed = migrate_supervisor_state(
            schema_two,
            target_runtime_fingerprint="new-runtime",
            target_contract_digest="new-contract",
            authoritative_source={
                "trust_boundary": "exclusive-event-store-writer/private-local-filesystem",
                "activation_id": "activation-1",
                "source_generation": 4,
                "source_payload_digest": "a" * 64,
                "source_event_sequence": 14,
                "source_event_id": "b" * 64,
                "target_generation": 5,
            },
        )

        self.assertTrue(changed)
        provenance = migrated["dispatch"]["jobs"]["run-old"][
            "legacy_execution_backend_provenance"
        ]
        self.assertEqual(provenance["source_schema_version"], 2)
        self.assertEqual(
            list(migrated["execution_backend_migration"]["imported_jobs"]),
            ["run-old"],
        )

    def test_locked_migration_rejects_a_concurrent_generation(self) -> None:
        class ChangedAuthority:
            def __init__(self) -> None:
                self.persisted: list[dict] = []

            def load_with_authority(self) -> tuple[dict, dict]:
                return {"schema_version": 2, "generation": "newer"}, {
                    "source_generation": 2,
                    "target_generation": 3,
                }

            def persist(self, value: dict) -> None:
                self.persisted.append(value)

        authority = ChangedAuthority()
        with self.assertRaisesRegex(
            sprint_supervisor.SupervisorError, "generation changed"
        ):
            sprint_supervisor.migrate_locked_authoritative_generation(
                authority,
                {"schema_version": 2, "generation": "older"},
                target_runtime_fingerprint="new-runtime",
                target_contract_digest="new-contract",
            )
        self.assertEqual(authority.persisted, [])

    def test_runtime_fingerprint_binds_execution_backend_migration_sql(self) -> None:
        baseline = sprint_supervisor.runtime_fingerprint()
        target = (ROOT / "contracts/event-store-v4-execution-backends.sql").resolve()
        original = Path.read_bytes

        def changed(path: Path) -> bytes:
            value = original(path)
            return value + b"\n-- fingerprint probe" if path.resolve() == target else value

        with patch.object(Path, "read_bytes", changed):
            observed = sprint_supervisor.runtime_fingerprint()

        self.assertNotEqual(observed, baseline)

    def test_unknown_or_ambiguous_legacy_stop_fails_closed(self) -> None:
        unknown = self.legacy()
        unknown["planning"]["decision_queue"][0]["source_id"] = "invented-stop"
        with self.assertRaisesRegex(SupervisorStateError, "unknown breaker source"):
            self.migrate(unknown)
        ambiguous = self.legacy()
        ambiguous["planning"]["decision_queue"][0]["state"] = "mystery"
        with self.assertRaisesRegex(SupervisorStateError, "ambiguous"):
            self.migrate(ambiguous)

    def test_migration_rejects_softening_protected_review_and_security_gates(
        self,
    ) -> None:
        for source_id in (
            "review_gate_failed",
            "security_gate_failed",
            "merge_gate_failed",
        ):
            with self.subTest(source_id=source_id):
                legacy = self.legacy()
                legacy["planning"]["decision_queue"] = []
                legacy["planning"]["ticket_breakers"] = [
                    {
                        "source_id": source_id,
                        "subject": "PNP-1",
                        "class_id": "ticket_retry_wait",
                        "scope": "ticket",
                        "strength": "soft",
                        "durable_state": "retry_wait",
                    }
                ]
                with self.assertRaisesRegex(SupervisorStateError, "protected"):
                    self.migrate(legacy)

    def test_legacy_route_degradation_becomes_local_without_losing_jobs(self) -> None:
        legacy = self.legacy()
        legacy["lifecycle_state"] = "degraded"
        jobs = copy.deepcopy(legacy["dispatch"]["jobs"])
        migrated, _changed = self.migrate(legacy)
        self.assertEqual(migrated["lifecycle_state"], "active")
        self.assertEqual(migrated["dispatch"]["jobs"], jobs)
        self.assertEqual(
            migrated["history"][-1]["event"],
            "legacy-route-degradation-reclassified",
        )

    def test_hard_budget_pause_migrates_to_exact_global_generation(self) -> None:
        legacy = self.legacy()
        legacy["lifecycle_state"] = "paused"
        legacy["planning"].update(
            {
                "pause_cause": "hard_sprint_budget_exhausted",
                "budget": {
                    "digest": "budget-receipt",
                    "absolute_ceiling_usd": "50",
                },
            }
        )
        migrated, _changed = self.migrate(legacy)
        active = migrated["planning"]["active_global_breaker"]
        self.assertEqual(active["source_id"], "max_usd_per_sprint")
        self.assertEqual(active["strength"], "hard")
        self.assertIn(":migration:", active["generation"])
        self.assertEqual(
            active["transition_evidence"]["budget_receipt"], "budget-receipt"
        )

    def test_existing_global_generation_survives_schema_migration_exactly(self) -> None:
        legacy = self.legacy()
        legacy["lifecycle_state"] = "paused"
        legacy["planning"]["pause_cause"] = "hard_sprint_budget_exhausted"
        breaker = sprint_supervisor.BREAKER_RUNTIME.sprint_record(
            "max_usd_per_sprint",
            sprint="99",
            evidence={"budget_receipt": "existing-budget"},
        )
        breaker.update(
            {
                "generation": "4:7:existing",
                "transition_evidence": {
                    "budget_receipt": "existing-budget",
                    "absolute_ceiling": "75",
                },
                "activated_at": "before",
            }
        )
        legacy["planning"]["active_global_breaker"] = copy.deepcopy(breaker)
        migrated, _changed = self.migrate(legacy)
        self.assertEqual(migrated["planning"]["active_global_breaker"], breaker)

    def test_unknown_global_pause_fails_closed_with_action(self) -> None:
        legacy = self.legacy()
        legacy["lifecycle_state"] = "paused"
        legacy["planning"]["pause_cause"] = "legacy-mystery"
        with self.assertRaisesRegex(SupervisorStateError, "classify it explicitly"):
            self.migrate(legacy)

    def test_schema_two_replay_rejects_tampered_breaker_identity(self) -> None:
        migrated, _changed = self.migrate(self.legacy())
        migrated["planning"]["ticket_breakers"][0]["record_digest"] = "forged"
        with self.assertRaisesRegex(SupervisorStateError, "mismatched record digest"):
            self.migrate(migrated)


class TakeoverStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="orka-takeover-state-test-"))
        subprocess.run(["git", "init", "-q", str(self.temp)], check=True)
        self.contract_digest = "contract"
        self.config_digest = "config"
        self.runtime_digest = "runtime"
        self.settings = {
            "enabled": False,
            "requested_sprint": "",
            "sync_interval_seconds": 60,
            "concurrency_max": 3,
            "ticket_retry_seconds": 30,
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def previous(self, lifecycle_state: str) -> dict:
        jobs = {
            state: {"ticket": f"PROJ-{index}", "state": state}
            for index, state in enumerate(
                (
                    "queued",
                    "reserved",
                    "running",
                    "retry_wait",
                    "repair_ready",
                    "recovery_ready",
                    "decomposition_ready",
                    "parked_decision",
                    "parked_external",
                    "blocked",
                ),
                1,
            )
        }
        value = {
            "schema_version": 1,
            "contract_id": "orka.supervisor-lifecycle",
            "contract_schema_version": 1,
            "repository": str(self.temp),
            "lifecycle_state": lifecycle_state,
            "last_event": "fixture",
            "started_at": "before",
            "updated_at": "before",
            "stopped_at": "",
            "process": {"pid": 99999, "start_fingerprint": "old-process"},
            "lease": {"id": "old-lease", "generation": 4},
            "control_socket": "old-socket",
            "history": [],
            "requests": [
                {
                    "request_id": "pause-1",
                    "command": "pause",
                    "response": {"status": "ok"},
                }
            ],
            "config_digest": self.config_digest,
            "runtime_fingerprint": self.runtime_digest,
            "contract_digest": self.contract_digest,
            "planning": {"pause_cause": "operator_paused", "next_wake_epoch": 99},
            "dispatch": {"jobs": jobs, "launch_count": 10, "terminal_count": 2},
        }
        if lifecycle_state == "takeover_pending":
            value["takeover"] = {"resume_state": "active"}
        return value

    def test_every_nonterminal_supervisor_state_preserves_jobs_on_takeover(
        self,
    ) -> None:
        _contract, lifecycle, _digest = sprint_supervisor.contract()
        for source in (
            "starting",
            "active",
            "degraded",
            "paused",
            "draining",
            "takeover_pending",
        ):
            with self.subTest(source=source):
                previous = self.previous(source)
                jobs = copy.deepcopy(previous["dispatch"]["jobs"])
                with patch.object(
                    sprint_supervisor, "process_status", return_value="absent"
                ):
                    recovered = sprint_supervisor.takeover_state(
                        previous,
                        identity={
                            "pid": 123,
                            "start_fingerprint": "new-process",
                            "session_id": 123,
                        },
                        lease={"id": "new-lease", "generation": 5},
                        lifecycle=lifecycle,
                        config_digest=self.config_digest,
                        runtime_digest=self.runtime_digest,
                        contract_digest=self.contract_digest,
                        settings=self.settings,
                    )
                expected = source if source in {"paused", "draining"} else "active"
                self.assertEqual(recovered["lifecycle_state"], expected)
                self.assertEqual(recovered["dispatch"]["jobs"], jobs)
                self.assertEqual(recovered["requests"], previous["requests"])
                events = [item["event"] for item in recovered["history"]]
                self.assertIn("takeover_requested", events)
                self.assertIn("predecessor_absent", events)

    def test_takeover_restores_the_exact_hard_budget_generation(self) -> None:
        _contract, lifecycle, _digest = sprint_supervisor.contract()
        previous = self.previous("paused")
        previous["planning"]["pause_cause"] = "hard_sprint_budget_exhausted"
        breaker = sprint_supervisor.BREAKER_RUNTIME.sprint_record(
            "max_usd_per_sprint",
            sprint="99",
            evidence={"budget_receipt": "budget-1"},
        )
        breaker.update(
            {
                "generation": "4:1:budget",
                "transition_evidence": {
                    "budget_receipt": "budget-1",
                    "absolute_ceiling": "20",
                },
                "activated_at": "before",
            }
        )
        previous["planning"]["active_global_breaker"] = breaker
        with patch.object(sprint_supervisor, "process_status", return_value="absent"):
            recovered = sprint_supervisor.takeover_state(
                previous,
                identity={
                    "pid": 123,
                    "start_fingerprint": "new-process",
                    "session_id": 123,
                },
                lease={"id": "new-lease", "generation": 5},
                lifecycle=lifecycle,
                config_digest=self.config_digest,
                runtime_digest=self.runtime_digest,
                contract_digest=self.contract_digest,
                settings=self.settings,
            )
        self.assertEqual(recovered["lifecycle_state"], "paused")
        self.assertEqual(
            recovered["planning"]["active_global_breaker"]["generation"],
            "4:1:budget",
        )
        self.assertEqual(
            recovered["history"][-1]["evidence"]["breaker_generation"],
            "4:1:budget",
        )

    def test_takeover_restores_pressure_and_all_routes_generations(self) -> None:
        _contract, lifecycle, _digest = sprint_supervisor.contract()
        cases = (
            (
                "degraded",
                "unfinished_pr_pressure",
                "4:1:pressure",
                {
                    "pressure_class": "unfinished_pr_pressure",
                    "capacity_snapshot": "capacity",
                },
            ),
            (
                "paused",
                "all_routes_unavailable",
                "4:2:routes",
                {"route_incidents": "routes", "next_probe_at": 200},
            ),
        )
        for lifecycle_state, source_id, generation, transition_evidence in cases:
            with self.subTest(source_id=source_id):
                previous = self.previous(lifecycle_state)
                previous["planning"]["pause_cause"] = (
                    "all_routes_unavailable" if lifecycle_state == "paused" else ""
                )
                evidence = (
                    {
                        "pressure_class": "unfinished_prs",
                        "capacity_snapshot": {"count": 3},
                    }
                    if lifecycle_state == "degraded"
                    else {"route_incidents": ["route-a"], "next_probe_at": 200}
                )
                breaker = sprint_supervisor.BREAKER_RUNTIME.sprint_record(
                    source_id, sprint="99", evidence=evidence
                )
                breaker.update(
                    {
                        "generation": generation,
                        "transition_evidence": transition_evidence,
                        "activated_at": "before",
                    }
                )
                previous["planning"]["active_global_breaker"] = breaker
                with patch.object(
                    sprint_supervisor, "process_status", return_value="absent"
                ):
                    recovered = sprint_supervisor.takeover_state(
                        previous,
                        identity={
                            "pid": 123,
                            "start_fingerprint": "new-process",
                            "session_id": 123,
                        },
                        lease={"id": "new-lease", "generation": 5},
                        lifecycle=lifecycle,
                        config_digest=self.config_digest,
                        runtime_digest=self.runtime_digest,
                        contract_digest=self.contract_digest,
                        settings=self.settings,
                    )
                self.assertEqual(recovered["lifecycle_state"], lifecycle_state)
                self.assertEqual(
                    recovered["planning"]["active_global_breaker"]["generation"],
                    generation,
                )
                self.assertEqual(
                    recovered["history"][-1]["evidence"]["breaker_generation"],
                    generation,
                )

    def test_takeover_rejects_live_unknown_or_changed_predecessor_evidence(
        self,
    ) -> None:
        _contract, lifecycle, _digest = sprint_supervisor.contract()
        arguments = {
            "identity": {
                "pid": 123,
                "start_fingerprint": "new-process",
                "session_id": 123,
            },
            "lease": {"id": "new-lease", "generation": 5},
            "lifecycle": lifecycle,
            "config_digest": self.config_digest,
            "runtime_digest": self.runtime_digest,
            "contract_digest": self.contract_digest,
            "settings": self.settings,
        }
        for status in ("live", "unknown"):
            with (
                self.subTest(status=status),
                patch.object(sprint_supervisor, "process_status", return_value=status),
            ):
                with self.assertRaises(sprint_supervisor.SupervisorError):
                    sprint_supervisor.takeover_state(
                        self.previous("active"), **arguments
                    )
        with patch.object(sprint_supervisor, "process_status", return_value="absent"):
            changed = dict(arguments, runtime_digest="changed")
            with self.assertRaises(sprint_supervisor.SupervisorError):
                sprint_supervisor.takeover_state(self.previous("active"), **changed)
            changed = dict(
                arguments,
                lease={"id": "new-lease", "generation": 5, "lock_inode": 2},
            )
            with self.assertRaises(sprint_supervisor.SupervisorError):
                sprint_supervisor.takeover_state(self.previous("active"), **changed)


class SupervisorProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="orka-supervisor-test-"))
        self.repositories: list[Path] = []

    def tearDown(self) -> None:
        for repository in self.repositories:
            state = self.read_state(repository, required=False)
            if state and self.process_live(state):
                self.run_cli(
                    "stop",
                    repository,
                    "--request-id",
                    "test-cleanup",
                    "--reason",
                    "test cleanup",
                    check=False,
                )
                self.wait_for(lambda: not self.process_live(state), timeout=5)
        shutil.rmtree(self.temp, ignore_errors=True)

    def repository(self, *, with_config: bool = True) -> Path:
        repository = self.temp / f"repo-{len(self.repositories)}"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        if with_config:
            config = repository / ".orchestration/config.yaml"
            config.parent.mkdir()
            config.write_text(
                "schema_version: 1\nconcurrency_max: 2\n", encoding="utf-8"
            )
        self.repositories.append(repository)
        return repository

    def run_cli(
        self,
        command: str,
        repository: Path,
        *extra: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "python3",
                str(SUPERVISOR),
                command,
                "--repo",
                str(repository),
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            self.fail(
                f"{command} failed ({result.returncode}): {result.stderr or result.stdout}"
            )
        return result

    def output(self, result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    def read_state(self, repository: Path, *, required: bool = True) -> dict | None:
        path = repository / ".orchestration/.supervisor/state.json"
        if not path.exists():
            if required:
                self.fail(f"missing supervisor state: {path}")
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def process_live(self, state: dict) -> bool:
        identity = state.get("process") or {}
        pid = identity.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_for(self, predicate, *, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("condition did not become true before timeout")

    def test_start_detaches_and_duplicate_start_is_rejected(self) -> None:
        repository = self.repository()
        started = self.output(self.run_cli("start", repository))
        self.assertEqual(started["lifecycle_state"], "active")
        state = self.read_state(repository)
        self.assertNotEqual(state["process"]["session_id"], os.getsid(0))
        self.assertTrue(self.process_live(state))

        duplicate = self.run_cli("start", repository, check=False)
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("lease is held", duplicate.stderr)
        self.assertTrue(self.process_live(state))

    def test_pause_resume_drain_stop_and_clean_restart(self) -> None:
        repository = self.repository()
        first = self.output(self.run_cli("start", repository))
        first_pid = first["process"]["pid"]

        paused = self.output(
            self.run_cli("pause", repository, "--request-id", "pause-1")
        )
        self.assertEqual(paused["lifecycle_state"], "paused")
        replayed = self.output(
            self.run_cli("pause", repository, "--request-id", "pause-1")
        )
        self.assertEqual(replayed, paused)

        conflict = self.run_cli(
            "resume", repository, "--request-id", "pause-1", check=False
        )
        self.assertEqual(conflict.returncode, 2)
        self.assertIn("another command", conflict.stderr)

        resumed = self.output(
            self.run_cli("resume", repository, "--request-id", "resume-1")
        )
        self.assertEqual(resumed["lifecycle_state"], "active")
        drained = self.output(
            self.run_cli("drain", repository, "--request-id", "drain-1")
        )
        self.assertEqual(drained["lifecycle_state"], "paused")

        stopped = self.output(
            self.run_cli(
                "stop",
                repository,
                "--request-id",
                "stop-1",
                "--reason",
                "operator requested maintenance",
            )
        )
        self.assertEqual(stopped["lifecycle_state"], "stopped")
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        self.wait_for(lambda: not self.process_live({"process": {"pid": first_pid}}))
        state = self.read_state(repository)
        self.assertEqual(state["lease"]["release_count"], 1)
        self.assertTrue(state["lease"]["released_at"])
        events = [item["event"] for item in state["history"]]
        self.assertIn("operator_paused", events)
        self.assertIn("operator_resumed", events)
        self.assertIn("drain_requested", events)
        self.assertIn("drain_completed", events)
        self.assertIn("operator_stopped", events)
        self.assertEqual(events.count("lease_released"), 1)

        second = self.output(self.run_cli("start", repository))
        self.assertEqual(second["lease_generation"], 2)
        self.assertNotEqual(second["lease_id"], first["lease_id"])
        restarted = self.read_state(repository)
        self.assertEqual(
            [item["event"] for item in restarted["history"]].count("lease_released"),
            1,
        )

    def test_status_identifies_process_and_lease_without_secrets(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        status = self.output(self.run_cli("status", repository))
        self.assertEqual(status["lifecycle_state"], "active")
        self.assertEqual(status["process_status"], "live")
        self.assertEqual(status["lease_generation"], 1)
        self.assertRegex(status["lease_id"], r"^[0-9a-f-]{36}$")
        encoded = json.dumps(status).casefold()
        self.assertNotIn("token", encoded)
        self.assertNotIn("password", encoded)
        self.assertNotIn("credential", encoded)

    def test_replacing_the_lease_inode_stops_fail_closed(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        state = self.read_state(repository)
        lease_path = Path(state["lease"]["lock_path"])
        lease_path.unlink()
        lease_path.write_text("replacement\n", encoding="utf-8")

        self.wait_for(
            lambda: self.read_state(repository)["lifecycle_state"] == "stopped"
        )
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        final = self.read_state(repository)
        self.assertEqual(final["last_event"], "lease_lost")
        self.assertEqual(
            final["planning"]["active_global_breaker"]["source_id"], "lease_lost"
        )
        self.wait_for(lambda: not self.process_live(final))
        self.assertFalse(self.process_live(final))

    def test_external_state_mutation_stops_fail_closed(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        state_path = repository / ".orchestration/.supervisor/state.json"
        state = self.read_state(repository)
        state["updated_at"] = "forged"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        self.wait_for(
            lambda: self.read_state(repository)["lifecycle_state"] == "stopped"
        )
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        final = self.read_state(repository)
        self.assertEqual(final["last_event"], "durable_state_invalid")
        self.assertEqual(
            final["planning"]["active_global_breaker"]["source_id"],
            "durable_state_invalid",
        )
        self.wait_for(lambda: not self.process_live(final))
        self.assertFalse(self.process_live(final))

    def test_missing_config_records_failed_preflight_and_releases_once(self) -> None:
        repository = self.repository(with_config=False)
        result = self.run_cli("start", repository, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("config is missing", result.stderr)
        self.wait_for(
            lambda: self.read_state(repository)["lease"]["release_count"] == 1
        )
        state = self.read_state(repository)
        self.assertEqual(state["lifecycle_state"], "stopped")
        self.assertIn("preflight_failed", [item["event"] for item in state["history"]])
        self.assertEqual(
            state["planning"]["active_global_breaker"]["source_id"],
            "preflight_failed",
        )
        self.assertEqual(state["lease"]["release_count"], 1)
        self.assertFalse(self.process_live(state))

    def test_unclean_process_death_resumes_with_exact_absence_receipt(self) -> None:
        repository = self.repository()
        first = self.output(self.run_cli("start", repository))
        state = self.read_state(repository)
        os.kill(state["process"]["pid"], signal.SIGKILL)
        self.wait_for(lambda: not self.process_live(state))

        replacement = self.output(self.run_cli("start", repository))
        self.assertEqual(replacement["lifecycle_state"], "active")
        self.assertEqual(replacement["lease_generation"], 2)
        self.assertNotEqual(replacement["lease_id"], first["lease_id"])
        recovered = self.read_state(repository)
        self.assertEqual(recovered["takeover"]["absence_receipt"]["status"], "absent")
        self.assertEqual(
            recovered["takeover"]["predecessor_lease"]["id"], first["lease_id"]
        )
        events = [item["event"] for item in recovered["history"]]
        self.assertIn("takeover_requested", events)
        self.assertIn("predecessor_absent", events)

    def test_authenticated_schema_one_state_migrates_once_on_unclean_restart(
        self,
    ) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        previous = self.read_state(repository)
        os.kill(previous["process"]["pid"], signal.SIGKILL)
        self.wait_for(lambda: not self.process_live(previous))

        state_path = repository / ".orchestration/.supervisor/state.json"
        receipt_path = repository / ".orchestration/.supervisor/state.sha256.json"
        legacy = self.read_state(repository)
        legacy["schema_version"] = 1
        legacy["runtime_fingerprint"] = "legacy-runtime-fingerprint"
        legacy["contract_digest"] = "legacy-contract-digest"
        legacy.pop("breaker_migration", None)
        state_path.write_text(
            json.dumps(legacy, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        receipt_path.write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
                    "recorded_at": "legacy",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        restarted = self.output(self.run_cli("start", repository))
        self.assertEqual(restarted["lifecycle_state"], "active")
        migrated = self.read_state(repository)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(
            migrated["breaker_migration"]["prior_runtime_fingerprint"],
            "legacy-runtime-fingerprint",
        )
        self.assertEqual(
            migrated["takeover"]["predecessor_process"], previous["process"]
        )

    def test_pause_and_idempotent_request_survive_unclean_restart(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        self.run_cli("pause", repository, "--request-id", "pause-1")
        before = self.read_state(repository)
        os.kill(before["process"]["pid"], signal.SIGKILL)
        self.wait_for(lambda: not self.process_live(before))

        restarted = self.output(self.run_cli("start", repository))
        self.assertEqual(restarted["lifecycle_state"], "paused")
        state = self.read_state(repository)
        pause_events = [
            item for item in state["history"] if item["event"] == "operator_paused"
        ]
        replay = self.output(
            self.run_cli("pause", repository, "--request-id", "pause-1")
        )
        self.assertEqual(replay["lifecycle_state"], "paused")
        after = self.read_state(repository)
        self.assertEqual(
            len(
                [
                    item
                    for item in after["history"]
                    if item["event"] == "operator_paused"
                ]
            ),
            len(pause_events),
        )

    def test_unclean_restart_rejects_tampered_durable_state(self) -> None:
        repository = self.repository()
        self.run_cli("start", repository)
        state = self.read_state(repository)
        os.kill(state["process"]["pid"], signal.SIGKILL)
        self.wait_for(lambda: not self.process_live(state))

        state_path = repository / ".orchestration/.supervisor/state.json"
        altered = self.read_state(repository)
        altered["updated_at"] = "tampered-after-crash"
        state_path.write_text(json.dumps(altered), encoding="utf-8")
        replacement = self.run_cli("start", repository, check=False)
        self.assertEqual(replacement.returncode, 2)
        self.assertIn("state digest does not match", replacement.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
