#!/usr/bin/env python3
"""Runtime bindings for Orka's machine-readable breaker classification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from breaker_contract import BreakerContractError, load_json, validate


class BreakerRuntimeError(RuntimeError):
    pass


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = PLUGIN_ROOT / "contracts/breaker-classification-v1.json"
CONTROLLER = Path(__file__).with_name("sprint-controller.py")

OUTCOME_SOURCES = {
    "needs_decomposition": "decomposition_required",
    "external_blocked": "external_dependency_wait",
    "operator_decision": "operator_decision_required",
    "blocked": "worker_irrecoverable",
    "malformed_result": "worker_result_invalid",
    "timeout_with_progress": "timeout_with_progress",
    "timeout_without_progress": "timeout_without_progress",
    "cancelled_attempt": "worker_cancelled",
}

PROVIDER_STATE_SOURCES = {
    "unverified": "provider_unverified",
    "rate_limited": "provider_rate_limited",
    "transport": "provider_transport",
    "authentication": "provider_authentication",
    "incompatible": "provider_incompatible",
    "unconfigured": "provider_incompatible",
}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BreakerRuntime:
    def __init__(
        self,
        contract_path: Path = DEFAULT_CONTRACT,
        controller: Path = CONTROLLER,
    ) -> None:
        try:
            self.contract = load_json(contract_path)
            validate(self.contract, controller)
        except BreakerContractError as exc:
            raise BreakerRuntimeError(str(exc)) from exc
        self.classes = self.contract["classes"]
        self.sources = {
            source["id"]: source for source in self.contract["stop_sources"]
        }

    def record(
        self,
        source_id: str,
        *,
        subject: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        source = self.sources.get(source_id)
        if not source:
            raise BreakerRuntimeError(f"unknown breaker source {source_id}")
        if not isinstance(subject, str) or not subject.strip():
            raise BreakerRuntimeError("breaker subject must be a nonempty string")
        if not isinstance(evidence, dict) or not evidence:
            raise BreakerRuntimeError("breaker evidence must be a nonempty object")
        class_id = source["class"]
        definition = self.classes[class_id]
        return {
            "source_id": source_id,
            "class_id": class_id,
            "scope": definition["scope"],
            "strength": definition["strength"],
            "durable_state": definition["durable_state"],
            "wake_condition": definition["wake_condition"],
            "authority": definition["authority"],
            "subject": subject.strip(),
            "evidence_digest": canonical_digest(evidence),
        }

    def terminal_record(
        self,
        outcome: str,
        *,
        target_state: str,
        ticket: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        source_id = OUTCOME_SOURCES.get(outcome)
        if source_id is None:
            return None
        record = self.record(source_id, subject=ticket, evidence=evidence)
        if record["scope"] != "ticket":
            raise BreakerRuntimeError(
                f"terminal outcome {outcome} resolved outside ticket scope"
            )
        if record["durable_state"] != target_state:
            raise BreakerRuntimeError(
                f"terminal outcome {outcome} targets {target_state}, but breaker "
                f"contract requires {record['durable_state']}"
            )
        return record

    def route_record(self, hold: dict[str, Any]) -> dict[str, Any]:
        state = str(hold.get("state") or "")
        source_id = PROVIDER_STATE_SOURCES.get(state)
        if not source_id:
            raise BreakerRuntimeError(f"unsupported provider hold state {state!r}")
        route = str(hold.get("route_identity") or hold.get("route") or "")
        role = str(hold.get("role") or "")
        if not route or not role:
            raise BreakerRuntimeError(
                "provider hold must bind an exact route identity and role"
            )
        record = self.record(
            source_id,
            subject=route,
            evidence={
                "provider": str(hold.get("provider") or ""),
                "role": role,
                "route_identity": route,
                "state": state,
                "incident": str(hold.get("incident") or ""),
                "retry_at": hold.get("retry_at"),
                "probe_until": hold.get("probe_until"),
            },
        )
        if record["scope"] != "route" or record["durable_state"] != "route_held":
            raise BreakerRuntimeError(
                f"provider hold {state} does not resolve to a route-held breaker"
            )
        record["role"] = role
        record["provider"] = str(hold.get("provider") or "")
        return record
