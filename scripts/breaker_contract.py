#!/usr/bin/env python3
"""Validate Orka's canonical breaker classification contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from supervisor_contract import (
    ContractError,
    current_controller_states,
    require_string_list,
)


class BreakerContractError(RuntimeError):
    pass


SCOPES = {"ticket", "route", "sprint"}
STRENGTHS = {"soft", "hard"}
REQUIRED_CLASS_FIELDS = {
    "scope",
    "strength",
    "durable_state",
    "wake_condition",
    "authority",
    "required_evidence",
    "global_transition",
    "immutable_hard",
}
REQUIRED_INVARIANTS = {
    "ticket_and_route_breakers_never_stop_supervisor": True,
    "skipped_work_is_non_consuming": True,
    "history_is_never_erased": True,
    "hard_controls_cannot_be_softened": True,
    "unknown_sources_fail_closed": True,
}
REQUIRED_STOP_SOURCES = {
    "max_usd_per_run",
    "pause_usd_per_ticket",
    "max_usd_per_ticket",
    "max_usd_per_sprint",
    "max_usd_per_design_phase",
    "max_usd_per_implementation_phase",
    "max_usd_per_code_review_phase",
    "max_usd_per_security_review_phase",
    "max_usd_without_progress",
    "max_model_runs_per_ticket",
    "max_reviewer_runs_per_ticket",
    "max_lane_relaunches",
    "design_round_limit",
    "review_fix_cycle_limit",
    "review_gate_failed",
    "security_gate_failed",
    "merge_gate_failed",
    "destructive_action_required",
    "operator_decision_required",
    "recovery_evidence_ambiguous",
    "launch_rejected_transient",
    "launch_rejected_permanent",
    "timeout_without_progress",
    "timeout_with_progress",
    "worker_process_lost",
    "worker_result_invalid",
    "worker_cancelled",
    "worker_irrecoverable",
    "decomposition_required",
    "internal_dependency_wait",
    "external_dependency_wait",
    "provider_unverified",
    "provider_rate_limited",
    "provider_transport",
    "provider_authentication",
    "provider_incompatible",
    "unfinished_pr_pressure",
    "lane_capacity_pressure",
    "heavy_process_pressure",
    "all_routes_unavailable",
    "preflight_failed",
    "lease_lost",
    "durable_state_invalid",
}
PROTECTED_HARD_SOURCES = {
    "max_usd_per_run": "ticket",
    "pause_usd_per_ticket": "ticket",
    "max_usd_per_ticket": "ticket",
    "max_usd_per_sprint": "sprint",
    "max_usd_per_design_phase": "ticket",
    "max_usd_per_implementation_phase": "ticket",
    "max_usd_per_code_review_phase": "ticket",
    "max_usd_per_security_review_phase": "ticket",
    "max_usd_without_progress": "ticket",
    "max_model_runs_per_ticket": "ticket",
    "max_reviewer_runs_per_ticket": "ticket",
    "max_lane_relaunches": "ticket",
    "design_round_limit": "ticket",
    "review_fix_cycle_limit": "ticket",
    "review_gate_failed": "ticket",
    "security_gate_failed": "ticket",
    "merge_gate_failed": "ticket",
    "destructive_action_required": "ticket",
    "operator_decision_required": "ticket",
    "recovery_evidence_ambiguous": "ticket",
    "launch_rejected_permanent": "ticket",
    "worker_irrecoverable": "ticket",
    "provider_authentication": "route",
    "provider_incompatible": "route",
    "preflight_failed": "sprint",
    "lease_lost": "sprint",
    "durable_state_invalid": "sprint",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BreakerContractError(f"cannot read breaker contract: {exc}") from exc
    if not isinstance(value, dict):
        raise BreakerContractError("breaker contract must contain a JSON object")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BreakerContractError(f"{label} must be a nonempty string")
    return value


def validate(contract: dict[str, Any], controller: Path) -> dict[str, Any]:
    if contract.get("schema_version") != 1:
        raise BreakerContractError("schema_version must be 1")
    if contract.get("contract_id") != "orka.breaker-classification":
        raise BreakerContractError("contract_id must be orka.breaker-classification")

    classes = contract.get("classes")
    if not isinstance(classes, dict) or not classes:
        raise BreakerContractError("classes must be a nonempty object")
    for name, definition in classes.items():
        _nonempty_string(name, "class name")
        if not isinstance(definition, dict) or set(definition) != REQUIRED_CLASS_FIELDS:
            raise BreakerContractError(
                f"class {name} must define exactly {sorted(REQUIRED_CLASS_FIELDS)}"
            )
        scope = definition.get("scope")
        strength = definition.get("strength")
        if scope not in SCOPES:
            raise BreakerContractError(f"class {name} has invalid scope")
        if strength not in STRENGTHS:
            raise BreakerContractError(f"class {name} has invalid strength")
        for field in ("durable_state", "wake_condition", "authority"):
            _nonempty_string(definition.get(field), f"classes.{name}.{field}")
        try:
            require_string_list(
                definition.get("required_evidence"),
                f"classes.{name}.required_evidence",
            )
        except RuntimeError as exc:
            raise BreakerContractError(str(exc)) from exc
        if not isinstance(definition.get("global_transition"), bool):
            raise BreakerContractError(
                f"class {name} global_transition must be boolean"
            )
        if not isinstance(definition.get("immutable_hard"), bool):
            raise BreakerContractError(f"class {name} immutable_hard must be boolean")
        if scope in {"ticket", "route"} and definition["global_transition"]:
            raise BreakerContractError(
                f"{scope} breaker class {name} cannot declare a global transition"
            )
        if definition["immutable_hard"] and strength != "hard":
            raise BreakerContractError(
                f"immutable breaker class {name} must have hard strength"
            )

    raw_sources = contract.get("stop_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise BreakerContractError("stop_sources must be a nonempty array")
    sources: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(raw_sources):
        if not isinstance(source, dict) or set(source) != {"id", "class", "signal"}:
            raise BreakerContractError(
                f"stop source {index} must define exactly id, class, and signal"
            )
        source_id = _nonempty_string(source.get("id"), f"stop_sources[{index}].id")
        if source_id in sources:
            raise BreakerContractError(f"duplicate stop source {source_id}")
        class_name = _nonempty_string(
            source.get("class"), f"stop_sources[{index}].class"
        )
        if class_name not in classes:
            raise BreakerContractError(
                f"stop source {source_id} names undefined class {class_name}"
            )
        _nonempty_string(source.get("signal"), f"stop_sources[{index}].signal")
        sources[source_id] = source
    if set(sources) != REQUIRED_STOP_SOURCES:
        missing = sorted(REQUIRED_STOP_SOURCES - set(sources))
        extra = sorted(set(sources) - REQUIRED_STOP_SOURCES)
        raise BreakerContractError(
            f"stop source inventory mismatch; missing={missing or 'none'} extra={extra or 'none'}"
        )
    for source_id, required_scope in PROTECTED_HARD_SOURCES.items():
        definition = classes[sources[source_id]["class"]]
        if (
            definition["scope"] != required_scope
            or definition["strength"] != "hard"
            or not definition["immutable_hard"]
        ):
            raise BreakerContractError(
                f"protected source {source_id} must remain immutable hard {required_scope}"
            )

    compatibility = contract.get("current_ticket_state_compatibility")
    if not isinstance(compatibility, dict):
        raise BreakerContractError(
            "current_ticket_state_compatibility must be an object"
        )
    try:
        current_states = current_controller_states(controller)
    except ContractError as exc:
        raise BreakerContractError(str(exc)) from exc
    if set(compatibility) != current_states:
        missing = sorted(current_states - set(compatibility))
        extra = sorted(set(compatibility) - current_states)
        raise BreakerContractError(
            "current ticket state compatibility mismatch; "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    for state, class_name in compatibility.items():
        if class_name is not None and class_name not in classes:
            raise BreakerContractError(
                f"compatibility state {state} names undefined class {class_name}"
            )

    if contract.get("invariants") != REQUIRED_INVARIANTS:
        raise BreakerContractError(
            "invariants do not preserve the accepted breaker safety rules"
        )
    return {
        "contract_id": contract["contract_id"],
        "schema_version": contract["schema_version"],
        "classes": len(classes),
        "stop_sources": len(sources),
        "protected_hard_sources": len(PROTECTED_HARD_SOURCES),
        "current_states_mapped": len(compatibility),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--controller", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate(load_json(args.contract), args.controller)
    except BreakerContractError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
