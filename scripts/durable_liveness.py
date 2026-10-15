#!/usr/bin/env python3
"""Validate and explore Orka's bounded durable-state liveness contract."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class LivenessError(RuntimeError):
    """The contract is malformed or contains a reachable sink."""

    def __init__(self, message: str, *, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report or {"error": message}


EXIT_CLASSES = {
    "automatic",
    "timer_driven",
    "authenticated_external_evidence",
    "operator_authorized",
    "terminal_failure",
}
SOURCE_NAMES = {
    "supervisor",
    "breaker",
    "recovery",
    "phase_worker",
    "review",
    "controller",
    "supervisor_cli",
    "api_agent",
    "operator_authority",
}


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LivenessError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise LivenessError(f"{label} must contain a JSON object")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise LivenessError(f"{label} must be a nonempty unique string array")
    return value


def _python_symbols(path: Path) -> tuple[set[str], dict[str, str]]:
    """Return literal argparse commands and string constants from one module."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise LivenessError(f"cannot inspect Python source {path}: {exc}") from exc
    commands: set[str] = set()
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            commands.add(node.args[0].value)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            constants[node.targets[0].id] = node.value.value
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and isinstance(node.iter, (ast.Tuple, ast.List))
            and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in node.iter.elts
            )
        ):
            target_name = node.target.id
            has_dynamic_parser = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "add_parser"
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id == target_name
                for statement in node.body
                for child in ast.walk(statement)
            )
            if has_dynamic_parser:
                commands.update(str(item.value) for item in node.iter.elts)
    return commands, constants


def source_registry(root: Path, sources: dict[str, Any]) -> set[str]:
    if set(sources) != SOURCE_NAMES:
        raise LivenessError(
            "source_contracts must define exactly " + ", ".join(sorted(SOURCE_NAMES))
        )
    resolved: dict[str, Path] = {}
    for name, raw in sources.items():
        if not isinstance(raw, str) or not raw:
            raise LivenessError(f"source_contracts.{name} must be a path")
        path = (root / raw).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise LivenessError(f"source_contracts.{name} escapes the repository") from exc
        resolved[name] = path

    supervisor = load_json(resolved["supervisor"], "supervisor lifecycle contract")
    breaker = load_json(resolved["breaker"], "breaker contract")
    recovery = load_json(resolved["recovery"], "recovery contract")
    phase = load_json(resolved["phase_worker"], "phase-worker contract")
    if supervisor.get("contract_id") != "orka.supervisor-lifecycle":
        raise LivenessError("supervisor source has the wrong contract identity")
    if breaker.get("contract_id") != "orka.breaker-classification":
        raise LivenessError("breaker source has the wrong contract identity")
    if recovery.get("schema") != "orka.recovery-eligibility/v1":
        raise LivenessError("recovery source has the wrong contract identity")
    if phase.get("contract_id") != "orka.phase-worker-protocol":
        raise LivenessError("phase-worker source has the wrong contract identity")

    registry = {
        *(f"supervisor:event:{name}" for name in (supervisor.get("events") or {})),
        *(f"breaker:class:{name}" for name in (breaker.get("classes") or {})),
        *(
            f"breaker:source:{item.get('id')}"
            for item in breaker.get("stop_sources") or []
            if isinstance(item, dict) and item.get("id")
        ),
        *(f"recovery:verdict:{name}" for name in (recovery.get("verdicts") or {})),
        *(f"recovery:reason:{name}" for name in (recovery.get("reason_codes") or {})),
        *(f"phase_worker:terminal:{name}" for name in (phase.get("terminal_outcomes") or {})),
    }
    review_commands, review_constants = _python_symbols(resolved["review"])
    controller_commands, _ = _python_symbols(resolved["controller"])
    supervisor_commands, _ = _python_symbols(resolved["supervisor_cli"])
    api_agent_commands, _ = _python_symbols(resolved["api_agent"])
    operator_commands, _ = _python_symbols(resolved["operator_authority"])
    registry.update(f"review:command:{name}" for name in review_commands)
    registry.update(
        f"review:action:{value}"
        for name, value in review_constants.items()
        if name.startswith("ACTION_")
    )
    registry.update(f"controller:command:{name}" for name in controller_commands)
    registry.update(f"supervisor_cli:command:{name}" for name in supervisor_commands)
    registry.update(f"api_agent:command:{name}" for name in api_agent_commands)
    registry.update(f"operator_authority:command:{name}" for name in operator_commands)
    return registry


def state_key(state: dict[str, str], dimensions: list[str]) -> tuple[str, ...]:
    return tuple(state[name] for name in dimensions)


def validate_state_invariants(state: dict[str, str], *, label: str) -> None:
    """Reject impossible or unactionable bounded-model states before BFS."""

    decision = state["decision_state"]
    authority = state["decision_authority"]
    command = state["command_availability"]
    if decision in {"undefined", "contradictory"}:
        raise LivenessError(f"{label} has an {decision} decision state")
    if state["ticket_state"] == "parked_decision":
        if decision != "actionable":
            raise LivenessError(f"{label} parks a ticket without an actionable decision")
        if authority != "ticket_scoped_operator":
            raise LivenessError(f"{label} parks a decision without authority")
        if command != "available":
            raise LivenessError(f"{label} parks a decision without an available command")
    elif decision != "none" or authority != "none":
        raise LivenessError(f"{label} assigns decision authority outside parked_decision")

    peer = state["peer_ticket"]
    relation = state["peer_relationship"]
    admission = state["scheduler_admission"]
    if peer in {"none", "completed"}:
        if relation != "none" or admission != "not_applicable":
            raise LivenessError(f"{label} has peer scheduler state without a live peer")
    elif peer == "queued":
        if relation not in {"independent", "depends_on_current"}:
            raise LivenessError(f"{label} has a queued peer without dependency semantics")
        if admission not in {"admitted", "blocked"}:
            raise LivenessError(f"{label} has a queued peer without scheduler admission")
        if relation == "depends_on_current" and admission == "admitted":
            raise LivenessError(f"{label} admits a peer that depends on current work")
    elif peer == "running" and (relation != "independent" or admission != "admitted"):
        raise LivenessError(f"{label} has an invalid running peer scheduler state")


def normalize_state(
    raw: Any,
    domains: dict[str, set[str]],
    *,
    label: str,
    defaults: dict[str, str],
) -> dict[str, str]:
    if not isinstance(raw, dict) or not set(raw) <= set(domains):
        raise LivenessError(
            f"{label} may define only these dimensions: {', '.join(domains)}"
        )
    state = dict(defaults)
    state.update(raw)
    if set(state) != set(domains):
        raise LivenessError(f"{label} has no complete state projection")
    for dimension, value in state.items():
        if not isinstance(value, str) or value not in domains[dimension]:
            raise LivenessError(f"{label}.{dimension} has an undefined value {value!r}")
    validate_state_invariants(state, label=label)
    return state


def _accepted_values(raw: Any, domain: set[str], label: str) -> set[str]:
    values = raw if isinstance(raw, list) else [raw]
    if not values or any(not isinstance(value, str) for value in values):
        raise LivenessError(f"{label} must name one or more state values")
    unknown = set(values) - domain
    if unknown:
        raise LivenessError(f"{label} has undefined values: {', '.join(sorted(unknown))}")
    return set(values)


def enabled(state: dict[str, str], transition: dict[str, Any]) -> bool:
    return all(
        state[dimension] in (set(expected) if isinstance(expected, list) else {expected})
        for dimension, expected in transition["when"].items()
    )


def apply_transition(
    state: dict[str, str], transition: dict[str, Any]
) -> dict[str, str]:
    result = dict(state)
    result.update(transition["set"])
    return result


def is_terminal(state: dict[str, str], terminal: dict[str, set[str]]) -> bool:
    if state["sprint_breaker"] in terminal["sprint_breaker"]:
        return True
    return (
        state["ticket_state"] in terminal["ticket_state"]
        and state["peer_ticket"] in terminal["peer_terminal"]
    )


def validate_source_owned_model(
    contract: dict[str, Any], *, root: Path, transitions: list[dict[str, Any]]
) -> None:
    """Prove the model is owned by runtime-enforced supervisor semantics."""

    raw_sources = contract.get("source_contracts")
    if not isinstance(raw_sources, dict) or not isinstance(raw_sources.get("supervisor"), str):
        raise LivenessError("source_contracts.supervisor must name the owning contract")
    supervisor_path = (root / raw_sources["supervisor"]).resolve()
    supervisor = load_json(supervisor_path, "supervisor lifecycle contract")
    ownership = supervisor.get("liveness_model")
    required = {
        "schema",
        "contract",
        "runtime_required",
        "job_state_dimension",
        "semantic_source",
        "job_event_bindings",
        "extension_transitions",
    }
    if not isinstance(ownership, dict) or set(ownership) != required:
        raise LivenessError("supervisor liveness_model has an invalid field set")
    if ownership.get("schema") != "orka.supervisor-owned-liveness/v1":
        raise LivenessError("supervisor liveness_model has an unsupported schema")
    if ownership.get("contract") != "contracts/durable-liveness-v1.json":
        raise LivenessError("supervisor liveness_model points to the wrong contract")
    if ownership.get("runtime_required") is not True:
        raise LivenessError("supervisor must require liveness validation at runtime")
    if ownership.get("job_state_dimension") != "ticket_state":
        raise LivenessError("supervisor liveness_model has the wrong state dimension")
    if ownership.get("semantic_source") != "transitions":
        raise LivenessError("supervisor liveness_model has the wrong semantic source")

    bindings = ownership.get("job_event_bindings")
    extensions = ownership.get("extension_transitions")
    if not isinstance(bindings, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in bindings.items()
    ):
        raise LivenessError("supervisor job_event_bindings must map transition ids to events")
    extension_ids = set(_string_list(extensions, "liveness_model.extension_transitions"))
    if set(bindings) & extension_ids:
        raise LivenessError("supervisor liveness ownership classes overlap")
    model = {item["id"]: item for item in transitions}
    if set(bindings) | extension_ids != set(model):
        raise LivenessError("supervisor liveness ownership does not cover the bounded model")

    source_transitions: dict[str, set[tuple[str, str]]] = {}
    for item in supervisor.get("transitions") or []:
        if not isinstance(item, dict) or item.get("entity") != "job":
            continue
        event = item.get("event")
        source = item.get("from")
        target = item.get("to")
        if not all(isinstance(value, str) and value for value in (event, source, target)):
            raise LivenessError("supervisor job transition is malformed")
        source_transitions.setdefault(event, set()).add((source, target))

    for identifier, event in bindings.items():
        item = model.get(identifier)
        if item is None:
            raise LivenessError(f"supervisor binds unknown liveness transition {identifier}")
        if item.get("derived_event") != event:
            raise LivenessError(f"liveness transition {identifier} has the wrong derived event")
        if f"supervisor:event:{event}" not in item["source_refs"]:
            raise LivenessError(f"liveness transition {identifier} does not cite {event}")
        if event not in source_transitions:
            raise LivenessError(f"liveness transition {identifier} binds an unknown job event")
        modeled_sources = item["when"].get("ticket_state")
        accepted = set(modeled_sources) if isinstance(modeled_sources, list) else {modeled_sources}
        modeled_edges = {
            (source, item["set"].get("ticket_state")) for source in accepted
        }
        if not modeled_edges or not modeled_edges <= source_transitions[event]:
            raise LivenessError(
                f"liveness transition {identifier} disagrees with supervisor event {event}"
            )

    for identifier in extension_ids:
        if "derived_event" in model[identifier]:
            raise LivenessError(
                f"extension transition {identifier} cannot claim a derived job event"
            )


@dataclass(frozen=True)
class Seed:
    name: str
    state: dict[str, str]


def validate_contract(
    contract: dict[str, Any], *, root: Path
) -> tuple[dict[str, set[str]], list[dict[str, Any]], dict[str, set[str]], list[Seed]]:
    if contract.get("schema_version") != 1:
        raise LivenessError("schema_version must be 1")
    if contract.get("contract_id") != "orka.durable-liveness":
        raise LivenessError("contract_id must be orka.durable-liveness")
    if set(_string_list(contract.get("exit_classes"), "exit_classes")) != EXIT_CLASSES:
        raise LivenessError("exit_classes must define every supported liveness exit")

    raw_dimensions = contract.get("dimensions")
    if not isinstance(raw_dimensions, dict) or not raw_dimensions:
        raise LivenessError("dimensions must be a nonempty object")
    domains = {
        name: set(_string_list(values, f"dimensions.{name}"))
        for name, values in raw_dimensions.items()
        if isinstance(name, str) and name
    }
    if len(domains) != len(raw_dimensions):
        raise LivenessError("dimension names must be nonempty strings")
    raw_defaults = contract.get("state_defaults")
    if not isinstance(raw_defaults, dict) or set(raw_defaults) != set(domains):
        raise LivenessError("state_defaults must define every state dimension")
    defaults: dict[str, str] = {}
    for dimension, value in raw_defaults.items():
        if not isinstance(value, str) or value not in domains[dimension]:
            raise LivenessError(f"state_defaults.{dimension} has an undefined value")
        defaults[dimension] = value

    raw_terminal = contract.get("terminal")
    if not isinstance(raw_terminal, dict) or set(raw_terminal) != {
        "ticket_state", "sprint_breaker", "peer_terminal"
    }:
        raise LivenessError("terminal must classify ticket, sprint, and peer state")
    terminal = {
        "ticket_state": _accepted_values(
            raw_terminal["ticket_state"], domains["ticket_state"], "terminal.ticket_state"
        ),
        "sprint_breaker": _accepted_values(
            raw_terminal["sprint_breaker"],
            domains["sprint_breaker"],
            "terminal.sprint_breaker",
        ),
        "peer_terminal": _accepted_values(
            raw_terminal["peer_terminal"], domains["peer_ticket"], "terminal.peer_terminal"
        ),
    }

    registry = source_registry(root, contract.get("source_contracts") or {})
    authorities = contract.get("operator_authorities")
    if not isinstance(authorities, dict) or not authorities:
        raise LivenessError("operator_authorities must be a nonempty object")
    authority_commands: dict[str, set[str]] = {}
    for authority, definition in authorities.items():
        if not isinstance(definition, dict) or set(definition) != {"commands"}:
            raise LivenessError(f"operator authority {authority} must define commands")
        authority_commands[authority] = set(
            _string_list(definition["commands"], f"operator_authorities.{authority}.commands")
        )
        missing_commands = {
            command
            for command in authority_commands[authority]
            if (
                f"{command.split(':', 1)[0]}:command:{command.split(':', 1)[1]}"
                not in registry
            )
        }
        if missing_commands:
            raise LivenessError(
                f"operator authority {authority} names undefined commands: "
                + ", ".join(sorted(missing_commands))
            )

    raw_transitions = contract.get("transitions")
    if not isinstance(raw_transitions, list) or not raw_transitions:
        raise LivenessError("transitions must be a nonempty array")
    transitions: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, raw in enumerate(raw_transitions):
        if not isinstance(raw, dict):
            raise LivenessError(f"transition {index} must be an object")
        required = {"id", "exit_class", "operation", "source_refs", "when", "set"}
        allowed = required | {"authority", "explicit_recovery", "derived_event"}
        if not required <= set(raw) or set(raw) - allowed:
            raise LivenessError(f"transition {index} has an invalid field set")
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise LivenessError(f"transition {index} has a missing or duplicate id")
        identifiers.add(identifier)
        exit_class = raw.get("exit_class")
        if exit_class not in EXIT_CLASSES:
            raise LivenessError(f"transition {identifier} has an invalid exit class")
        operation = raw.get("operation")
        if not isinstance(operation, str) or not operation:
            raise LivenessError(f"transition {identifier} requires an operation")
        refs = _string_list(raw.get("source_refs"), f"transitions.{identifier}.source_refs")
        missing_refs = set(refs) - registry
        if missing_refs:
            raise LivenessError(
                f"transition {identifier} has undefined source refs: "
                + ", ".join(sorted(missing_refs))
            )
        operation_namespace, separator, operation_name = operation.partition(":")
        if separator and operation_namespace in {
            "review", "controller", "supervisor_cli", "api_agent",
            "operator_authority",
        }:
            operation_ref = f"{operation_namespace}:command:{operation_name}"
            if operation_ref not in refs:
                raise LivenessError(
                    f"transition {identifier} does not cite its operation command {operation_ref}"
                )
        conditions = raw.get("when")
        updates = raw.get("set")
        if not isinstance(conditions, dict) or not conditions:
            raise LivenessError(f"transition {identifier} requires conditions")
        if not isinstance(updates, dict) or not updates:
            raise LivenessError(f"transition {identifier} requires updates")
        for dimension, expected in conditions.items():
            if dimension not in domains:
                raise LivenessError(f"transition {identifier} uses unknown dimension {dimension}")
            _accepted_values(
                expected, domains[dimension], f"transitions.{identifier}.when.{dimension}"
            )
        for dimension, value in updates.items():
            if (
                dimension not in domains
                or not isinstance(value, str)
                or value not in domains[dimension]
            ):
                raise LivenessError(f"transition {identifier} has an invalid {dimension} update")
        authority = raw.get("authority")
        if exit_class == "operator_authorized":
            if authority not in authority_commands:
                raise LivenessError(
                    f"operator transition {identifier} lacks a named authority"
                )
            if operation not in authority_commands[authority]:
                raise LivenessError(
                    f"operator transition {identifier} operation is not granted by {authority}"
                )
        elif authority is not None:
            raise LivenessError(f"non-operator transition {identifier} cannot name authority")
        transitions.append(copy.deepcopy(raw))

    raw_initial = contract.get("initial_states")
    if not isinstance(raw_initial, list) or not raw_initial:
        raise LivenessError("initial_states must be a nonempty array")
    seeds: list[Seed] = []
    names: set[str] = set()
    for index, item in enumerate(raw_initial):
        if not isinstance(item, dict) or set(item) != {"name", "state"}:
            raise LivenessError(f"initial state {index} must define name and state")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise LivenessError(f"initial state {index} has a missing or duplicate name")
        names.add(name)
        seeds.append(
            Seed(name, normalize_state(item["state"], domains, label=name, defaults=defaults))
        )
    validate_source_owned_model(contract, root=root, transitions=transitions)
    return domains, transitions, terminal, seeds


def load_fixtures(
    path: Path, domains: dict[str, set[str]], defaults: dict[str, str]
) -> list[tuple[Seed, str, str]]:
    value = load_json(path, "liveness fixtures")
    if value.get("schema") != "orka.durable-liveness-fixtures/v1":
        raise LivenessError("fixtures use an unsupported schema")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise LivenessError("fixtures must contain cases")
    result: list[tuple[Seed, str, str]] = []
    names: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or set(case) != {
            "name", "state", "expected_transition", "expected_exit_class"
        }:
            raise LivenessError(f"fixture {index} has an invalid field set")
        name = case.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise LivenessError(f"fixture {index} has a missing or duplicate name")
        names.add(name)
        result.append(
            (
                Seed(
                    name,
                    normalize_state(
                        case["state"], domains, label=name, defaults=defaults
                    ),
                ),
                str(case["expected_transition"]),
                str(case["expected_exit_class"]),
            )
        )
    return result


def _trace(
    key: tuple[str, ...],
    predecessor: dict[tuple[str, ...], tuple[tuple[str, ...], str] | None],
    seeds: dict[tuple[str, ...], str],
) -> tuple[str, list[str]]:
    transitions: list[str] = []
    cursor = key
    while predecessor[cursor] is not None:
        prior, transition = predecessor[cursor]  # type: ignore[misc]
        transitions.append(transition)
        cursor = prior
    transitions.reverse()
    return seeds[cursor], transitions


def verify(
    contract: dict[str, Any], *, root: Path, fixtures_path: Path | None = None
) -> dict[str, Any]:
    domains, transitions, terminal, initial = validate_contract(contract, root=root)
    defaults = contract["state_defaults"]
    fixture_cases: list[tuple[Seed, str, str]] = []
    if fixtures_path is not None:
        fixture_cases = load_fixtures(fixtures_path, domains, defaults)
    by_id = {transition["id"]: transition for transition in transitions}
    for seed, expected_transition, expected_class in fixture_cases:
        matching = [transition for transition in transitions if enabled(seed.state, transition)]
        if expected_transition not in {transition["id"] for transition in matching}:
            raise LivenessError(
                f"fixture {seed.name} does not enable {expected_transition}"
            )
        if (
            expected_transition not in by_id
            or by_id[expected_transition]["exit_class"] != expected_class
        ):
            raise LivenessError(
                f"fixture {seed.name} expected the wrong exit classification"
            )

    all_seeds = initial + [case[0] for case in fixture_cases]
    dimensions = list(domains)
    queue: deque[tuple[str, ...]] = deque()
    states: dict[tuple[str, ...], dict[str, str]] = {}
    predecessor: dict[tuple[str, ...], tuple[tuple[str, ...], str] | None] = {}
    seed_names: dict[tuple[str, ...], str] = {}
    for seed in all_seeds:
        key = state_key(seed.state, dimensions)
        if key not in states:
            states[key] = seed.state
            predecessor[key] = None
            seed_names[key] = seed.name
            queue.append(key)

    transition_hits = {transition["id"]: 0 for transition in transitions}
    exit_hits = {exit_class: 0 for exit_class in EXIT_CLASSES}
    terminal_count = 0
    while queue:
        key = queue.popleft()
        state = states[key]
        applicable = [transition for transition in transitions if enabled(state, transition)]
        terminal_state = is_terminal(state, terminal)
        if terminal_state:
            terminal_count += 1
            illegal = [
                transition["id"]
                for transition in applicable
                if not is_terminal(
                    apply_transition(state, transition), terminal
                ) and transition.get("explicit_recovery") is not True
            ]
            if illegal:
                seed, trace = _trace(key, predecessor, seed_names)
                raise LivenessError(
                    "terminal state has a non-recovery mutation",
                    report={
                        "error": "terminal state has a non-recovery mutation",
                        "seed": seed,
                        "trace": trace,
                        "illegal_transitions": illegal,
                        "state": state,
                    },
                )
        elif not applicable:
            seed, trace = _trace(key, predecessor, seed_names)
            raise LivenessError(
                "reachable nonterminal state has no supported exit",
                report={
                    "error": "reachable nonterminal state has no supported exit",
                    "seed": seed,
                    "trace": trace,
                    "state": state,
                },
            )

        if (
            state["peer_ticket"] == "queued"
            and state["peer_relationship"] == "independent"
            and state["scheduler_admission"] == "admitted"
            and state["ticket_state"] in {
                "parked_decision", "parked_external", "blocked"
            }
            and state["sprint_breaker"] == "active"
            and state["route_breaker"] == "healthy"
        ):
            independent = [
                transition
                for transition in applicable
                if transition["exit_class"] != "operator_authorized"
                and transition["set"].get("peer_ticket") in {"running", "completed"}
            ]
            if not independent:
                seed, trace = _trace(key, predecessor, seed_names)
                raise LivenessError(
                    "ticket-local hold blocks an independent queued ticket",
                    report={
                        "error": "ticket-local hold blocks an independent queued ticket",
                        "seed": seed,
                        "trace": trace,
                        "state": state,
                    },
                )

        for transition in applicable:
            transition_hits[transition["id"]] += 1
            exit_hits[transition["exit_class"]] += 1
            target = apply_transition(state, transition)
            validate_state_invariants(target, label=f"transition {transition['id']} target")
            if target == state:
                raise LivenessError(f"transition {transition['id']} does not change state")
            target_key = state_key(target, dimensions)
            if target_key not in states:
                if len(states) >= 10000:
                    raise LivenessError("bounded liveness exploration exceeded 10000 states")
                states[target_key] = target
                predecessor[target_key] = (key, transition["id"])
                queue.append(target_key)

    missing_exit_classes = sorted(
        exit_class for exit_class, count in exit_hits.items() if count == 0
    )
    if missing_exit_classes:
        raise LivenessError(
            "bounded model does not exercise exit classes: "
            + ", ".join(missing_exit_classes)
        )
    unreachable = sorted(identifier for identifier, count in transition_hits.items() if count == 0)
    if unreachable:
        raise LivenessError(
            "bounded model contains unreachable transitions: " + ", ".join(unreachable)
        )
    return {
        "contract_id": contract["contract_id"],
        "schema_version": contract["schema_version"],
        "dimensions": len(domains),
        "transitions": len(transitions),
        "reachable_states": len(states),
        "terminal_states": terminal_count,
        "fixtures": len(fixture_cases),
        "exit_classes": dict(sorted(exit_hits.items())),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = verify(
            load_json(args.contract, "liveness contract"),
            root=args.root.resolve(),
            fixtures_path=args.fixtures,
        )
    except LivenessError as exc:
        print(json.dumps(exc.report, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
