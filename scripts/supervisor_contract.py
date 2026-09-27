#!/usr/bin/env python3
"""Validate Orka's machine-readable durable supervisor lifecycle contract."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    pass


REPLAY_POLICIES = {"no_op_if_applied", "reconcile_then_no_op", "reject_if_stale"}
SCOPES = {"ticket", "route", "global"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read lifecycle contract: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("lifecycle contract must contain a JSON object")
    return value


def current_controller_states(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ContractError(f"cannot inspect sprint controller states: {exc}") from exc
    values: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in {
            "TERMINAL",
            "AUTONOMOUS_INTERVENTIONS",
        }:
            continue
        try:
            parsed = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ContractError(f"cannot parse controller state set {target.id}") from exc
        if not isinstance(parsed, set) or not all(isinstance(item, str) for item in parsed):
            raise ContractError(f"controller state set {target.id} must be string literals")
        values[target.id] = parsed
    missing = {"TERMINAL", "AUTONOMOUS_INTERVENTIONS"} - values.keys()
    if missing:
        raise ContractError("controller state constants missing: " + ", ".join(sorted(missing)))
    return {"pending", "running"} | values["TERMINAL"] | values["AUTONOMOUS_INTERVENTIONS"]


def require_string_list(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError(f"{label} must be a{' nonempty' if nonempty else ''} array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{label} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ContractError(f"{label} must not contain duplicates")
    return value


def validate(contract: dict[str, Any], controller: Path) -> dict[str, Any]:
    if contract.get("schema_version") != 1:
        raise ContractError("schema_version must be 1")
    if contract.get("contract_id") != "orka.supervisor-lifecycle":
        raise ContractError("contract_id must be orka.supervisor-lifecycle")

    raw_entities = contract.get("entities")
    if not isinstance(raw_entities, dict) or set(raw_entities) != {"supervisor", "job"}:
        raise ContractError("entities must define exactly supervisor and job")
    entity_states: dict[str, set[str]] = {}
    for entity, definition in raw_entities.items():
        if not isinstance(definition, dict):
            raise ContractError(f"entities.{entity} must be an object")
        states = set(require_string_list(definition.get("states"), f"entities.{entity}.states"))
        terminal = set(
            require_string_list(
                definition.get("terminal_states"),
                f"entities.{entity}.terminal_states",
                nonempty=False,
            )
        )
        if not terminal <= states:
            raise ContractError(f"entities.{entity}.terminal_states contains an undefined state")
        entity_states[entity] = states

    events = contract.get("events")
    if not isinstance(events, dict) or not events:
        raise ContractError("events must be a nonempty object")
    for name, event in events.items():
        if not isinstance(name, str) or not name or not isinstance(event, dict):
            raise ContractError("events must use nonempty string names and object definitions")
        if event.get("entity") not in entity_states:
            raise ContractError(f"event {name} names an undefined entity")
        if event.get("scope") not in SCOPES:
            raise ContractError(f"event {name} has an invalid scope")
        require_string_list(event.get("required_evidence"), f"events.{name}.required_evidence")
        if event.get("replay") not in REPLAY_POLICIES:
            raise ContractError(f"event {name} has an invalid replay policy")

    transitions = contract.get("transitions")
    if not isinstance(transitions, list) or not transitions:
        raise ContractError("transitions must be a nonempty array")
    seen: set[tuple[str, str, str]] = set()
    for index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            raise ContractError(f"transition {index} must be an object")
        entity = transition.get("entity")
        source = transition.get("from")
        event_name = transition.get("event")
        target = transition.get("to")
        if entity not in entity_states:
            raise ContractError(f"transition {index} names an undefined entity")
        if source not in entity_states[entity]:
            raise ContractError(f"transition {index} has undefined source state {source!r}")
        if target not in entity_states[entity]:
            raise ContractError(f"transition {index} has undefined target state {target!r}")
        if event_name not in events:
            raise ContractError(f"transition {index} names undefined event {event_name!r}")
        if events[event_name]["entity"] != entity:
            raise ContractError(f"transition {index} uses an event for another entity")
        key = (entity, source, event_name)
        if key in seen:
            raise ContractError(
                f"ambiguous transition for entity={entity} from={source} event={event_name}"
            )
        seen.add(key)
        if entity == "supervisor" and target in {"paused", "stopped"}:
            if events[event_name]["scope"] != "global":
                raise ContractError(
                    f"non-global event {event_name} cannot pause or stop the supervisor"
                )

    results = contract.get("worker_terminal_results")
    if not isinstance(results, dict) or not results:
        raise ContractError("worker_terminal_results must be a nonempty object")
    for result, event_name in results.items():
        matches = [
            item
            for item in transitions
            if item.get("entity") == "job"
            and item.get("from") == "running"
            and item.get("event") == event_name
        ]
        if len(matches) != 1:
            raise ContractError(
                f"worker terminal result {result} must map to exactly one running transition"
            )

    compatibility = contract.get("current_ticket_state_compatibility")
    if not isinstance(compatibility, dict):
        raise ContractError("current_ticket_state_compatibility must be an object")
    current = current_controller_states(controller)
    if set(compatibility) != current:
        missing = sorted(current - set(compatibility))
        extra = sorted(set(compatibility) - current)
        raise ContractError(
            "current ticket state compatibility mismatch; "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    for old_state, mapping in compatibility.items():
        if not isinstance(mapping, dict) or mapping.get("job_state") not in entity_states["job"]:
            raise ContractError(f"compatibility state {old_state} has an undefined job_state")
        if not isinstance(mapping.get("interpretation"), str) or not mapping["interpretation"]:
            raise ContractError(f"compatibility state {old_state} lacks an interpretation")

    decisions = contract.get("operator_only_decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ContractError("operator_only_decisions must be a nonempty array")
    decision_classes: list[str] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ContractError("operator_only_decisions must contain objects")
        decision_class = decision.get("class")
        if not isinstance(decision_class, str) or not decision_class:
            raise ContractError("operator decision class must be a nonempty string")
        if not isinstance(decision.get("description"), str) or not decision["description"]:
            raise ContractError(f"operator decision {decision_class} lacks a description")
        decision_classes.append(decision_class)
    if len(decision_classes) != len(set(decision_classes)):
        raise ContractError("operator decision classes must be unique")

    global_stops = set(
        require_string_list(contract.get("global_admission_stops"), "global_admission_stops")
    )
    unknown_global_stops = global_stops - set(events)
    if unknown_global_stops:
        raise ContractError(
            "global_admission_stops names undefined events: "
            + ", ".join(sorted(unknown_global_stops))
        )
    for event_name in global_stops:
        if events[event_name]["entity"] != "supervisor" or events[event_name]["scope"] != "global":
            raise ContractError(f"global admission stop {event_name} is not a global supervisor event")

    queue_policy = contract.get("queue_policy")
    required_policy = {
        "eligibility_checks_are_non_consuming": True,
        "skipped_jobs_remain_queued": True,
        "ticket_local_events_never_stop_supervisor": True,
        "worker_context_reuse_across_tickets": False,
    }
    if queue_policy != required_policy:
        raise ContractError("queue_policy does not preserve the accepted durable-runtime invariants")

    return {
        "contract_id": contract["contract_id"],
        "schema_version": contract["schema_version"],
        "supervisor_states": len(entity_states["supervisor"]),
        "job_states": len(entity_states["job"]),
        "events": len(events),
        "transitions": len(transitions),
        "current_states_mapped": len(compatibility),
        "worker_results_mapped": len(results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--controller", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate(load_json(args.contract), args.controller)
    except ContractError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
