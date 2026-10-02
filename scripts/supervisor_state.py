#!/usr/bin/env python3
"""Deterministic migration for durable Orka supervisor state."""

from __future__ import annotations

import copy
from typing import Any

from breaker_runtime import BreakerRuntime, BreakerRuntimeError, canonical_digest


class SupervisorStateError(RuntimeError):
    pass


CURRENT_SCHEMA_VERSION = 3
BREAKER_RUNTIME = BreakerRuntime()
BREAKER_RECORD_FIELDS = (
    "source_id",
    "class_id",
    "scope",
    "strength",
    "durable_state",
    "wake_condition",
    "authority",
    "global_transition",
    "subject",
    "evidence_digest",
)


def _legacy_execution_digest(job: dict[str, Any]) -> str:
    return canonical_digest(
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


def _legacy_time(state: dict[str, Any]) -> str:
    return str(state.get("updated_at") or state.get("started_at") or "legacy")


def _upgrade_record(record: dict[str, Any], expected_scope: str) -> dict[str, Any]:
    source_id = str(record.get("source_id") or "")
    subject = str(record.get("subject") or "")
    source = BREAKER_RUNTIME.sources.get(source_id)
    if not source:
        raise SupervisorStateError(
            f"legacy breaker names unknown source {source_id!r}; classify it explicitly"
        )
    definition = BREAKER_RUNTIME.classes[source["class"]]
    expected = {
        "class_id": source["class"],
        "scope": definition["scope"],
        "strength": definition["strength"],
        "durable_state": definition["durable_state"],
        "authority": definition["authority"],
    }
    if not subject or expected["scope"] != expected_scope:
        raise SupervisorStateError(
            f"legacy breaker {source_id!r} cannot be migrated as {expected_scope} scope"
        )
    for field, canonical in expected.items():
        observed = record.get(field)
        if observed not in {None, "", canonical}:
            raise SupervisorStateError(
                f"legacy breaker {source_id!r} changes protected {field}; "
                "classify it explicitly"
            )
    existing_digest = str(record.get("record_digest") or "")
    upgraded = copy.deepcopy(record)
    upgraded.update(expected)
    upgraded["wake_condition"] = definition["wake_condition"]
    upgraded["global_transition"] = bool(definition["global_transition"])
    evidence_digest = str(upgraded.get("evidence_digest") or "")
    if not evidence_digest:
        evidence_digest = canonical_digest(
            {
                "legacy_source": source_id,
                "subject": subject,
                "record": record,
            }
        )
    upgraded["evidence_digest"] = evidence_digest
    expected_digest = canonical_digest(
        {field: upgraded[field] for field in BREAKER_RECORD_FIELDS}
    )
    if existing_digest and existing_digest != expected_digest:
        raise SupervisorStateError(
            f"legacy breaker {source_id!r} has a mismatched record digest"
        )
    upgraded["record_digest"] = existing_digest or expected_digest
    return upgraded


def _validate_current_state(value: dict[str, Any]) -> None:
    planning = value.get("planning") or {}
    if not isinstance(planning, dict):
        raise SupervisorStateError("supervisor planning state must be an object")
    for field, scope in (
        ("ticket_breakers", "ticket"),
        ("route_breakers", "route"),
        ("pressure_breakers", "sprint"),
    ):
        records = planning.get(field) or []
        if not isinstance(records, list):
            raise SupervisorStateError(f"supervisor {field} must be an array")
        for record in records:
            if _upgrade_record(record, scope) != record:
                raise SupervisorStateError(
                    f"schema-v2 {field} contains a noncanonical breaker record"
                )
    active = planning.get("active_global_breaker") or {}
    if active:
        upgraded = _upgrade_record(active, "sprint")
        if upgraded != active:
            raise SupervisorStateError(
                "schema-v2 active global breaker is not canonical"
            )
        expected_class = {
            "degraded": {"sprint_pressure"},
            "paused": {"sprint_wait", "sprint_hard_budget"},
            "stopped": {"sprint_integrity_stop"},
        }.get(str(value.get("lifecycle_state") or ""), set())
        if (
            active["class_id"] not in expected_class
            or not active.get("generation")
            or not isinstance(active.get("transition_evidence"), dict)
            or not active["transition_evidence"]
        ):
            raise SupervisorStateError(
                "schema-v2 global breaker is not bound to its lifecycle and exact generation"
            )


def _legacy_ticket_breakers(planning: dict[str, Any]) -> list[dict[str, Any]]:
    existing = planning.get("ticket_breakers") or []
    if not isinstance(existing, list):
        raise SupervisorStateError("legacy ticket_breakers must be an array")
    if existing:
        return [_upgrade_record(record, "ticket") for record in existing]

    records: list[dict[str, Any]] = []
    decisions = planning.get("decision_queue") or []
    if not isinstance(decisions, list):
        raise SupervisorStateError("legacy decision_queue must be an array")
    mapping = {
        "external_blocked": "external_dependency_wait",
        "blocked": "worker_irrecoverable",
        "operator_decision": "operator_decision_required",
        "user_action": "operator_decision_required",
    }
    for decision in decisions:
        if not isinstance(decision, dict):
            raise SupervisorStateError("legacy decision entry must be an object")
        key = str(decision.get("key") or "")
        state = str(decision.get("state") or "")
        source_id = str(decision.get("source_id") or mapping.get(state) or "")
        if not key or not source_id:
            raise SupervisorStateError(
                "legacy ticket stop is ambiguous; record its ticket and breaker source"
            )
        try:
            record = BREAKER_RUNTIME.record(
                source_id,
                subject=key,
                evidence={"legacy_decision": decision},
            )
        except BreakerRuntimeError as exc:
            raise SupervisorStateError(
                f"legacy ticket {key} cannot be classified: {exc}"
            ) from exc
        if record["scope"] != "ticket":
            raise SupervisorStateError(
                f"legacy ticket {key} names non-ticket breaker {source_id}"
            )
        records.append(record)
    return sorted(records, key=lambda item: (item["subject"], item["source_id"]))


def _legacy_route_breakers(planning: dict[str, Any]) -> list[dict[str, Any]]:
    existing = planning.get("route_breakers") or []
    if not isinstance(existing, list):
        raise SupervisorStateError("legacy route_breakers must be an array")
    if existing:
        return [_upgrade_record(record, "route") for record in existing]
    records = []
    for hold in planning.get("provider_holds") or []:
        try:
            records.append(BREAKER_RUNTIME.route_record(hold))
        except BreakerRuntimeError as exc:
            raise SupervisorStateError(
                f"legacy provider hold is ambiguous: {exc}; refresh route health evidence"
            ) from exc
    return sorted(records, key=lambda item: (item["subject"], item["source_id"]))


def _bind_legacy_global(
    state: dict[str, Any], planning: dict[str, Any], source_digest: str
) -> None:
    lifecycle = str(state.get("lifecycle_state") or "")
    cause = str(planning.get("pause_cause") or "")
    sprint = str((planning.get("sprint") or {}).get("id") or state.get("repository"))
    existing = planning.get("active_global_breaker") or {}
    if existing:
        upgraded = _upgrade_record(existing, "sprint")
        expected_class = {
            "degraded": "sprint_pressure",
            "paused": {"sprint_wait", "sprint_hard_budget"},
            "stopped": "sprint_integrity_stop",
        }.get(lifecycle)
        if (
            not upgraded.get("generation")
            or not isinstance(upgraded.get("transition_evidence"), dict)
            or not upgraded["transition_evidence"]
            or (
                isinstance(expected_class, set)
                and upgraded["class_id"] not in expected_class
            )
            or (
                isinstance(expected_class, str)
                and upgraded["class_id"] != expected_class
            )
            or expected_class is None
        ):
            raise SupervisorStateError(
                "legacy global breaker is not bound to its lifecycle and exact generation"
            )
        planning["active_global_breaker"] = upgraded
        return
    if lifecycle == "degraded":
        # Pre-taxonomy degraded meant "some route is held". Route holds are
        # local now and cannot change global state, so preserve them and resume
        # global admission. A future planning cycle may apply real pressure.
        state["lifecycle_state"] = "active"
        state.setdefault("history", []).append(
            {
                "at": _legacy_time(state),
                "event": "legacy-route-degradation-reclassified",
                "from": "degraded",
                "to": "active",
                "evidence": {"migration_source_digest": source_digest},
            }
        )
        return
    if lifecycle != "paused":
        return
    if cause in {"operator_paused", "operator_drain"}:
        planning["operator_pause_generation"] = canonical_digest(
            {"source_digest": source_digest, "cause": cause}
        )
        return
    if cause == "all_routes_unavailable":
        incidents = planning.get("route_breakers") or []
        if not incidents:
            raise SupervisorStateError(
                "legacy all-routes pause has no route incidents; refresh route health evidence"
            )
        evidence = {
            "route_incidents": incidents,
            "next_probe_at": planning.get("next_wake_epoch"),
        }
        source_id = "all_routes_unavailable"
        transition_evidence = {
            "route_incidents": canonical_digest(incidents),
            "next_probe_at": planning.get("next_wake_epoch"),
        }
    elif cause == "hard_sprint_budget_exhausted":
        budget = planning.get("budget") or {}
        ceiling = budget.get("absolute_ceiling_usd")
        receipt = budget.get("digest")
        if ceiling in {None, ""} or not receipt:
            raise SupervisorStateError(
                "legacy hard sprint budget pause lacks its receipt and absolute ceiling"
            )
        evidence = {"budget_receipt": budget}
        source_id = "max_usd_per_sprint"
        transition_evidence = {
            "budget_receipt": receipt,
            "absolute_ceiling": ceiling,
        }
    else:
        raise SupervisorStateError(
            f"legacy paused state has unknown cause {cause!r}; classify it explicitly"
        )
    try:
        breaker = BREAKER_RUNTIME.sprint_record(
            source_id, sprint=sprint, evidence=evidence
        )
    except BreakerRuntimeError as exc:
        raise SupervisorStateError(str(exc)) from exc
    breaker["generation"] = (
        f"{int((state.get('lease') or {}).get('generation') or 0)}:"
        f"migration:{source_digest[:16]}"
    )
    breaker["transition_evidence"] = transition_evidence
    breaker["activated_at"] = _legacy_time(state)
    planning["active_global_breaker"] = breaker
    planning["global_breaker_sequence"] = max(
        1, int(planning.get("global_breaker_sequence") or 0)
    )


def migrate_supervisor_state(
    value: dict[str, Any],
    *,
    target_runtime_fingerprint: str,
    target_contract_digest: str,
) -> tuple[dict[str, Any], bool]:
    """Upgrade durable state exactly once while retaining its complete payload."""

    version = value.get("schema_version")
    if version == CURRENT_SCHEMA_VERSION:
        _validate_current_state(value)
        return value, False
    if version not in {1, 2}:
        raise SupervisorStateError(
            f"unsupported supervisor state schema {version!r}; restore a supported checkpoint"
        )
    source = copy.deepcopy(value)
    source_digest = canonical_digest(source)
    migrated = copy.deepcopy(source)
    if version == 1:
        planning = migrated.setdefault("planning", {})
        if not isinstance(planning, dict):
            raise SupervisorStateError(
                "legacy supervisor planning state must be an object"
            )
        planning["ticket_breakers"] = _legacy_ticket_breakers(planning)
        planning["route_breakers"] = _legacy_route_breakers(planning)
        pressure_breakers = planning.get("pressure_breakers") or []
        if not isinstance(pressure_breakers, list):
            raise SupervisorStateError("legacy pressure_breakers must be an array")
        planning["pressure_breakers"] = [
            _upgrade_record(record, "sprint") for record in pressure_breakers
        ]
        if any(
            record["class_id"] != "sprint_pressure"
            for record in planning["pressure_breakers"]
        ):
            raise SupervisorStateError(
                "legacy pressure list contains a non-pressure sprint breaker"
            )
        for field, default in (
            ("global_breaker_history", []),
            ("active_global_breaker", {}),
        ):
            planning.setdefault(field, default)
        planning.setdefault("global_breaker_sequence", 0)
        _bind_legacy_global(migrated, planning, source_digest)
        migrated["schema_version"] = 2
        migrated["breaker_migration"] = {
            "schema_version": 1,
            "migration_id": f"breaker-taxonomy:{source_digest}",
            "source_schema_version": 1,
            "source_digest": source_digest,
            "prior_runtime_fingerprint": str(source.get("runtime_fingerprint") or ""),
            "target_runtime_fingerprint": target_runtime_fingerprint,
            "prior_contract_digest": str(source.get("contract_digest") or ""),
            "target_contract_digest": target_contract_digest,
            "preserved_payload_digest": canonical_digest(
                {
                    key: source.get(key)
                    for key in ("dispatch", "requests", "history", "takeover")
                }
            ),
        }

    schema_two = copy.deepcopy(migrated)
    schema_two_digest = canonical_digest(schema_two)
    jobs = ((migrated.get("dispatch") or {}).get("jobs") or {})
    if not isinstance(jobs, dict):
        raise SupervisorStateError("supervisor dispatch jobs must be an object")
    imported: list[str] = []
    for run_ref, job in sorted(jobs.items()):
        if not isinstance(job, dict):
            raise SupervisorStateError(f"job {run_ref} must be an object")
        if (
            job.get("state") in {"running", "reserved", "launch_uncertain"}
            and "execution_backend" not in job
            and isinstance(job.get("phase_execution"), dict)
            and isinstance(job.get("execution_identity"), dict)
        ):
            job_digest = _legacy_execution_digest(job)
            job["legacy_execution_backend_provenance"] = {
                "schema_version": 1,
                "migration_id": f"execution-backend-v1:{job_digest}",
                "source_schema_version": 2,
                "source_job_digest": job_digest,
            }
            imported.append(str(run_ref))
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    migrated["execution_backend_migration"] = {
        "schema_version": 1,
        "migration_id": f"execution-backend-v1:{schema_two_digest}",
        "source_schema_version": 2,
        "source_digest": schema_two_digest,
        "prior_runtime_fingerprint": str(schema_two.get("runtime_fingerprint") or ""),
        "target_runtime_fingerprint": target_runtime_fingerprint,
        "prior_contract_digest": str(schema_two.get("contract_digest") or ""),
        "target_contract_digest": target_contract_digest,
        "imported_runs": imported,
    }
    _validate_current_state(migrated)
    return migrated, True


def migration_authorizes_runtime(
    state: dict[str, Any], *, runtime_fingerprint: str, contract_digest: str
) -> bool:
    receipt = state.get("execution_backend_migration") or {}
    if (
        receipt.get("source_schema_version") == 2
        and receipt.get("prior_runtime_fingerprint") == state.get("runtime_fingerprint")
        and receipt.get("target_runtime_fingerprint") == runtime_fingerprint
        and receipt.get("prior_contract_digest") == state.get("contract_digest")
        and receipt.get("target_contract_digest") == contract_digest
        and receipt.get("migration_id")
        == f"execution-backend-v1:{receipt.get('source_digest')}"
    ):
        return True
    receipt = state.get("breaker_migration") or {}
    return bool(
        receipt.get("source_schema_version") == 1
        and receipt.get("prior_runtime_fingerprint") == state.get("runtime_fingerprint")
        and receipt.get("target_runtime_fingerprint") == runtime_fingerprint
        and receipt.get("prior_contract_digest") == state.get("contract_digest")
        and receipt.get("target_contract_digest") == contract_digest
        and receipt.get("migration_id")
        == f"breaker-taxonomy:{receipt.get('source_digest')}"
    )
