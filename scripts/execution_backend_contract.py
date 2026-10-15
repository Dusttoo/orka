#!/usr/bin/env python3
"""Validate the versioned replaceable execution-backend contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    pass


def load_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read execution-backend contract: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("execution-backend contract must be an object")
    return value


def _unique_strings(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ContractError(f"{label} must be a nonempty unique string array")
    return value


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if contract.get("schema_version") != 1:
        raise ContractError("schema_version must be 1")
    if contract.get("contract_id") != "orka.execution-backend":
        raise ContractError("contract_id must be orka.execution-backend")
    if contract.get("protocol_version") != 1:
        raise ContractError("protocol_version must be 1")

    operations = set(_unique_strings(contract.get("operations"), "operations"))
    expected_operations = {
        "discover", "launch", "attach", "heartbeat", "progress", "cancel",
        "inspect", "terminal",
    }
    if operations != expected_operations:
        raise ContractError("operations must define the complete backend lifecycle")

    identities = set(
        _unique_strings(contract.get("identity_fields"), "identity_fields")
    )
    expected_identities = {
        "repository_id", "job_id", "phase", "attempt_token", "dispatch_id",
        "execution_unit_id", "worktree_id", "supervisor_fence",
    }
    if identities != expected_identities:
        raise ContractError("identity_fields must define the immutable execution binding")

    fresh = contract.get("fresh_context")
    if not isinstance(fresh, dict) or fresh.get("required") is not True:
        raise ContractError("fresh phase context must be required")
    if fresh.get("model_context_reuse_allowed") is not False:
        raise ContractError("model context reuse must be forbidden")
    if fresh.get("phase_worker_contract") != "orka.phase-worker-protocol/v1":
        raise ContractError("fresh context must bind the phase-worker v1 envelope")
    forbidden_context = set(
        _unique_strings(fresh.get("forbidden_fields"), "fresh_context.forbidden_fields")
    )
    if not {"conversation_id", "provider_session_id", "resume_session"} <= forbidden_context:
        raise ContractError("fresh context omits forbidden session state")

    domains = contract.get("capability_domains")
    if not isinstance(domains, dict) or set(domains) != {
        "lifecycle", "containment", "provider",
    }:
        raise ContractError("capability domains must separate lifecycle, containment, and provider")
    lifecycle = set(
        _unique_strings(domains["lifecycle"].get("required"), "lifecycle.required")
    )
    optional_lifecycle = set(
        _unique_strings(domains["lifecycle"].get("optional"), "lifecycle.optional")
    )
    if (
        lifecycle != expected_operations - {"discover", "cancel"}
        or optional_lifecycle != {"cancel"}
    ):
        raise ContractError("lifecycle capabilities are incomplete")
    containment = domains["containment"]
    containment_fields = set(
        _unique_strings(containment.get("required_fields"), "containment.required_fields")
    )
    required_containment = {
        "process", "filesystem", "network", "credentials", "process_control"
    }
    if containment_fields != required_containment:
        raise ContractError("containment capabilities are incomplete")
    values = containment.get("values")
    if not isinstance(values, dict) or set(values) != required_containment:
        raise ContractError("each containment field must define accepted values")
    for field, accepted in values.items():
        _unique_strings(accepted, f"containment.values.{field}")
    provider = domains["provider"]
    if provider != {
        "separate_from_containment": True,
        "backend_may_require": True,
        "backend_may_authorize": False,
    }:
        raise ContractError("provider capabilities must remain non-authoritative")

    inspection = contract.get("inspection")
    if not isinstance(inspection, dict):
        raise ContractError("inspection policy is required")
    if set(_unique_strings(inspection.get("statuses"), "inspection.statuses")) != {
        "live", "absent", "terminal", "unknown"
    }:
        raise ContractError("inspection statuses are incomplete")
    if inspection.get("timeout_status") != "unknown" or inspection.get("permission_denied_status") != "unknown":
        raise ContractError("inspection uncertainty must fail closed as unknown")
    if inspection.get("pid_alone_proves_absence") is not False:
        raise ContractError("PID-only inspection cannot prove absence")
    if inspection.get("process_identity_reuse") != "original_absent_with_receipt":
        raise ContractError("process identity reuse needs an explicit receipt")
    if inspection.get("terminal_requires_canonical_phase_envelope") is not True:
        raise ContractError("terminal inspection must require canonical terminal evidence")
    if set(inspection.get("replacement_safe_statuses") or []) != {"absent"}:
        raise ContractError("only authenticated mechanical absence permits replacement")

    cancellation = contract.get("cancellation")
    if not isinstance(cancellation, dict):
        raise ContractError("cancellation policy is required")
    if set(_unique_strings(cancellation.get("safe_results"), "cancellation.safe_results")) != {"acknowledged_with_verified_absence"}:
        raise ContractError("cancellation safety requires acknowledgement and absence")
    if (
        cancellation.get("acknowledgement_replacement_safe") is not False
        or cancellation.get("replacement_requires_mechanical_absence") is not True
    ):
        raise ContractError("cancellation acknowledgement alone cannot permit replacement")
    if cancellation.get("backend_may_assert_fence") is not False:
        raise ContractError("only the supervisor may assert an execution fence")
    if cancellation.get("unacknowledged_requires_supervisor_fence") is not True:
        raise ContractError("unacknowledged cancellation must require a fence")
    acknowledgement = set(
        _unique_strings(
            cancellation.get("acknowledgement_requires"),
            "cancellation.acknowledgement_requires",
        )
    )
    if acknowledgement != {
        "canonical_cancellation_ack",
        "exact_launch_identity",
        "exact_request_identity",
        "backend_mechanical_verification",
    }:
        raise ContractError("cancellation acknowledgement trust rules are incomplete")

    launch_receipt = contract.get("launch_receipt")
    if not isinstance(launch_receipt, dict):
        raise ContractError("launch receipt rules are required")
    if set(
        _unique_strings(
            launch_receipt.get("required_bindings"),
            "launch_receipt.required_bindings",
        )
    ) != {
        "binding", "backend_instance_id", "backend_handle", "process_identity",
        "envelope_digest",
    } or launch_receipt.get("mechanical_digest_required") is not True:
        raise ContractError("launch receipt must mechanically bind the execution")

    tombstones = contract.get("execution_tombstones")
    if tombstones != {
        "full_execution_key_reuse_allowed": False,
        "persist_before_production_integration": True,
        "retained_after_fence": True,
        "retained_after_uncertain_launch": True,
    }:
        raise ContractError("execution tombstones must permanently prevent key reuse")

    test_backends = contract.get("test_backends")
    if test_backends != {
        "explicit_conformance_mode_required": True,
        "production_selection_allowed": False,
    }:
        raise ContractError("test backends must be unavailable to production selection")

    authority = contract.get("authority_boundary")
    if not isinstance(authority, dict):
        raise ContractError("authority boundary is required")
    forbidden = set(_unique_strings(authority.get("forbidden"), "authority.forbidden"))
    if not {"schedule_work", "authorize_merge", "admit_replacement"} <= forbidden:
        raise ContractError("backend authority boundary is too broad")

    profiles = contract.get("compatibility_profiles")
    if not isinstance(profiles, dict) or set(profiles) != {
        "codex-desktop", "claude-desktop", "api"
    }:
        raise ContractError("compatibility map must cover current execution routes")

    return {
        "contract_id": contract["contract_id"],
        "protocol_version": contract["protocol_version"],
        "operations": len(operations),
        "identity_fields": len(identities),
        "compatibility_profiles": len(profiles),
    }
