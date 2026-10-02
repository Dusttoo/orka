#!/usr/bin/env python3
"""Validate Orka's disposable phase-worker protocol and shared fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    pass


TYPE_CHECKS = {
    "array": lambda value: isinstance(value, list),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "object": lambda value: isinstance(value, dict),
    "string": lambda value: isinstance(value, str) and bool(value),
}

EXPECTED_ENVELOPE_IDENTITIES = {
    "capability_offer": set(),
    "job": {
        "job_id", "ticket_id", "phase", "attempt_token", "supervisor_fence",
        "repository_id", "worktree_id", "dispatch_id", "execution_unit_id",
    },
    "progress": {
        "job_id", "ticket_id", "phase", "attempt_token", "supervisor_fence",
        "dispatch_id", "execution_unit_id",
    },
    "heartbeat": {
        "job_id", "ticket_id", "phase", "attempt_token", "supervisor_fence",
        "dispatch_id", "execution_unit_id",
    },
    "cancellation": {
        "job_id", "ticket_id", "phase", "attempt_token", "supervisor_fence",
        "dispatch_id", "execution_unit_id",
    },
    "cancellation_ack": {
        "job_id", "ticket_id", "phase", "attempt_token", "supervisor_fence",
        "dispatch_id", "execution_unit_id",
    },
    "terminal": {
        "job_id", "ticket_id", "phase", "attempt_token", "supervisor_fence",
        "repository_id", "worktree_id", "dispatch_id", "execution_unit_id",
    },
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def string_list(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError(f"{label} must be a{' nonempty' if nonempty else ''} array")
    if any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{label} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ContractError(f"{label} must not contain duplicates")
    return value


def reject_forbidden_fields(
    value: Any,
    forbidden_fields: list[str],
    *,
    label: str,
    path: str = "$",
) -> None:
    """Reject exact forbidden keys recursively without inspecting string values."""

    forbidden = set(forbidden_fields)
    if isinstance(value, dict):
        for key, nested in value.items():
            child_path = f"{path}.{key}"
            if key in forbidden:
                raise ContractError(
                    f"{label} contains forbidden field {key} at {child_path}"
                )
            reject_forbidden_fields(
                nested,
                forbidden_fields,
                label=label,
                path=child_path,
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            reject_forbidden_fields(
                nested,
                forbidden_fields,
                label=label,
                path=f"{path}[{index}]",
            )


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if contract.get("schema_version") != 1:
        raise ContractError("schema_version must be 1")
    if contract.get("contract_id") != "orka.phase-worker-protocol":
        raise ContractError("contract_id must be orka.phase-worker-protocol")
    if contract.get("protocol_version") != 1:
        raise ContractError("protocol_version must be 1")

    context = contract.get("context_lifecycle")
    required_context = {
        "scope": "one-phase-dispatch",
        "fresh_context_required": True,
        "conversation_reuse_across_dispatches": False,
        "host_process_reuse_allowed": True,
        "host_retained_model_state_allowed": False,
    }
    if context != required_context:
        raise ContractError("context_lifecycle does not enforce fresh phase contexts")

    identities = string_list(contract.get("identity_fields"), "identity_fields")
    required_identities = {
        "job_id",
        "ticket_id",
        "phase",
        "attempt_token",
        "supervisor_fence",
        "repository_id",
        "worktree_id",
        "dispatch_id",
        "execution_unit_id",
    }
    if set(identities) != required_identities:
        raise ContractError("identity_fields must define the complete immutable binding")

    capabilities = contract.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ContractError("capabilities must be an object")
    mandatory = set(string_list(capabilities.get("mandatory"), "capabilities.mandatory"))
    optional = set(string_list(capabilities.get("optional"), "capabilities.optional"))
    required_capabilities = {
        "attempt-fencing",
        "cancellation-ack-or-fence",
        "fresh-context-per-dispatch",
        "immutable-job-identity",
        "structured-progress",
        "structured-terminal-result",
    }
    if mandatory != required_capabilities:
        raise ContractError("mandatory capabilities do not preserve the protocol invariants")
    if mandatory & optional:
        raise ContractError("mandatory and optional capabilities must be disjoint")

    profiles = contract.get("adapter_profiles")
    if not isinstance(profiles, dict) or set(profiles) != {
        "codex-desktop",
        "claude-desktop",
        "api",
    }:
        raise ContractError("adapter_profiles must define Codex, Claude, and API")
    known_capabilities = mandatory | optional
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ContractError(f"adapter profile {name} must be an object")
        required = set(
            string_list(
                profile.get("required_capabilities"),
                f"adapter_profiles.{name}.required_capabilities",
            )
        )
        if not required <= known_capabilities:
            raise ContractError(f"adapter profile {name} names an unknown capability")

    envelopes = contract.get("envelopes")
    required_envelopes = {
        "capability_offer",
        "job",
        "progress",
        "heartbeat",
        "cancellation",
        "cancellation_ack",
        "terminal",
    }
    if not isinstance(envelopes, dict) or set(envelopes) != required_envelopes:
        raise ContractError("envelopes must define every protocol message exactly once")
    for name, schema in envelopes.items():
        if not isinstance(schema, dict):
            raise ContractError(f"envelope {name} must be an object")
        required = set(string_list(schema.get("required_fields"), f"envelopes.{name}.required_fields"))
        types = schema.get("types")
        constants = schema.get("constants")
        identity_fields = set(
            string_list(
                schema.get("identity_fields"),
                f"envelopes.{name}.identity_fields",
                nonempty=False,
            )
        )
        forbidden = set(
            string_list(
                schema.get("forbidden_fields"),
                f"envelopes.{name}.forbidden_fields",
                nonempty=False,
            )
        )
        if not isinstance(types, dict) or set(types) != required:
            raise ContractError(f"envelope {name} must type every required field")
        if any(value not in TYPE_CHECKS for value in types.values()):
            raise ContractError(f"envelope {name} contains an unsupported field type")
        if not isinstance(constants, dict) or not set(constants) <= required:
            raise ContractError(f"envelope {name} constants must be required fields")
        if not identity_fields <= required_identities or not identity_fields <= required:
            raise ContractError(f"envelope {name} has an invalid identity binding")
        if identity_fields != EXPECTED_ENVELOPE_IDENTITIES[name]:
            raise ContractError(
                f"envelope {name} does not carry its complete immutable identity binding"
            )
        if required & forbidden:
            raise ContractError(f"envelope {name} requires a forbidden field")

    outcomes = set(string_list(contract.get("terminal_outcomes"), "terminal_outcomes"))
    expected_outcomes = {
        "blocked",
        "cancelled_attempt",
        "completed",
        "external_blocked",
        "malformed_result",
        "needs_decomposition",
        "needs_repair",
        "operator_decision",
        "recoverable",
        "timeout_with_progress",
        "timeout_without_progress",
    }
    if outcomes != expected_outcomes:
        raise ContractError("terminal_outcomes must match the supervisor lifecycle contract")

    artifact_bindings = contract.get("artifact_bindings")
    expected_artifact_bindings = {
        "terminal_fields": ["artifact_id", "branch", "head", "pr", "tree"],
        "value_type": "string",
        "external_verification_required": True,
    }
    if artifact_bindings != expected_artifact_bindings:
        raise ContractError("artifact_bindings must define the trusted terminal references")

    failure_classes = contract.get("failure_classes")
    required_failures = {
        "unsupported_protocol",
        "missing_capability",
        "identity_mismatch",
        "malformed_envelope",
        "cancellation_unacknowledged",
    }
    if not isinstance(failure_classes, dict) or set(failure_classes) != required_failures:
        raise ContractError("failure_classes must classify every protocol boundary")
    if any(not isinstance(value, str) or not value for value in failure_classes.values()):
        raise ContractError("failure_classes must name bounded lifecycle results")

    compatibility = contract.get("current_evidence_compatibility")
    required_compatibility = {
        "attempt_token",
        "controller_invocation_id",
        "execution_unit_identity",
        "repository_identity",
        "worker_cwd",
    }
    if not isinstance(compatibility, dict) or set(compatibility) != required_compatibility:
        raise ContractError("current_evidence_compatibility is incomplete")
    if not set(compatibility.values()) <= required_identities:
        raise ContractError("current evidence maps outside protocol identity fields")

    return {
        "contract_id": contract["contract_id"],
        "protocol_version": contract["protocol_version"],
        "envelopes": len(envelopes),
        "mandatory_capabilities": len(mandatory),
        "adapter_profiles": len(profiles),
        "terminal_outcomes": len(outcomes),
    }


def validate_envelope(
    contract: dict[str, Any],
    envelope: dict[str, Any],
    *,
    expected_identity: dict[str, str] | None = None,
) -> None:
    if not isinstance(envelope, dict):
        raise ContractError("envelope must be an object")
    kind = envelope.get("kind")
    schemas = contract["envelopes"]
    if kind not in schemas:
        raise ContractError(f"unsupported envelope kind {kind!r}")
    schema = schemas[kind]
    for field in schema["required_fields"]:
        if field not in envelope:
            raise ContractError(f"{kind} is missing required field {field}")
        expected_type = schema["types"][field]
        if not TYPE_CHECKS[expected_type](envelope[field]):
            raise ContractError(f"{kind}.{field} must be a nonempty {expected_type}")
    for field, expected in schema["constants"].items():
        if envelope[field] != expected:
            raise ContractError(f"{kind}.{field} must equal {expected!r}")
    if kind == "job":
        reject_forbidden_fields(
            envelope,
            schema["forbidden_fields"],
            label=kind,
        )
    else:
        for field in schema["forbidden_fields"]:
            if field in envelope:
                raise ContractError(f"{kind} contains forbidden field {field}")
    if expected_identity is not None:
        for field in schema["identity_fields"]:
            if envelope[field] != expected_identity[field]:
                raise ContractError(f"identity field {field} does not match the dispatch")

    mandatory = set(contract["capabilities"]["mandatory"])
    if kind == "capability_offer":
        supported = set(
            string_list(envelope["supported_capabilities"], "supported_capabilities")
        )
        missing = mandatory - supported
        if missing:
            raise ContractError(
                "missing mandatory capabilities: " + ", ".join(sorted(missing))
            )
        adapter = envelope["adapter"]
        if adapter not in contract["adapter_profiles"]:
            raise ContractError(f"unsupported adapter profile {adapter}")
        profile_required = set(
            contract["adapter_profiles"][adapter]["required_capabilities"]
        )
        if not profile_required <= supported:
            raise ContractError(
                f"adapter {adapter} is missing profile capabilities: "
                + ", ".join(sorted(profile_required - supported))
            )
    elif kind == "job":
        required = set(string_list(envelope["required_capabilities"], "required_capabilities"))
        if not mandatory <= required:
            raise ContractError("job does not require every mandatory capability")
    elif kind == "progress" and envelope["sequence"] < 1:
        raise ContractError("progress.sequence must be at least 1")
    elif kind == "terminal":
        if envelope["outcome"] not in contract["terminal_outcomes"]:
            raise ContractError(f"unsupported terminal outcome {envelope['outcome']}")
        artifacts = envelope["artifacts"]
        allowed_artifacts = set(contract["artifact_bindings"]["terminal_fields"])
        unknown_artifacts = set(artifacts) - allowed_artifacts
        if unknown_artifacts:
            raise ContractError(
                "terminal contains unsupported artifact bindings: "
                + ", ".join(sorted(unknown_artifacts))
            )
        if any(not isinstance(value, str) or not value for value in artifacts.values()):
            raise ContractError("terminal artifact bindings must be nonempty strings")


def validate_fixtures(contract: dict[str, Any], fixtures: dict[str, Any]) -> dict[str, Any]:
    if fixtures.get("schema_version") != 1:
        raise ContractError("fixture schema_version must be 1")
    if fixtures.get("contract_id") != contract["contract_id"]:
        raise ContractError("fixtures target another contract")
    identity = fixtures.get("identity")
    if not isinstance(identity, dict) or set(identity) != set(contract["identity_fields"]):
        raise ContractError("fixture identity must bind every protocol identity field")
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ContractError("fixture identity values must be nonempty strings")
    cases = fixtures.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ContractError("fixtures must contain cases")
    names: set[str] = set()
    valid_count = 0
    invalid_count = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ContractError(f"fixture case {index} must be an object")
        name = case.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ContractError("fixture names must be unique nonempty strings")
        names.add(name)
        envelope = case.get("envelope")
        if not isinstance(envelope, dict):
            raise ContractError(f"fixture {name} envelope must be an object")
        envelope = {**(identity if case.get("use_identity") else {}), **envelope}
        expected_identity = identity if case.get("use_identity") else None
        expected_valid = case.get("valid")
        if not isinstance(expected_valid, bool):
            raise ContractError(f"fixture {name} valid must be boolean")
        try:
            validate_envelope(contract, envelope, expected_identity=expected_identity)
        except ContractError as exc:
            if expected_valid:
                raise ContractError(f"valid fixture {name} failed: {exc}") from exc
            expected_error = case.get("error")
            if not isinstance(expected_error, str) or expected_error not in str(exc):
                raise ContractError(
                    f"invalid fixture {name} failed for the wrong reason: {exc}"
                ) from exc
            invalid_count += 1
        else:
            if not expected_valid:
                raise ContractError(f"invalid fixture {name} was accepted")
            valid_count += 1
    return {"fixtures": len(cases), "valid": valid_count, "invalid": invalid_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--fixtures", type=Path)
    args = parser.parse_args()
    try:
        contract = load_json(args.contract)
        result = validate_contract(contract)
        if args.fixtures:
            result.update(validate_fixtures(contract, load_json(args.fixtures)))
    except ContractError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
