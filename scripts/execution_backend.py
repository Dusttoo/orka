#!/usr/bin/env python3
"""Supervisor-owned boundary for replaceable execution backends.

Backends execute and observe one already-admitted phase.  They never schedule,
reserve, authorize a replacement, or decide whether evidence permits a merge.
The coordinator validates every backend receipt against the immutable execution
binding before returning it to supervisor code.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from execution_backend_contract import ContractError, validate_contract


class BackendContractError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BackendCoordinator:
    """Validate one backend without leaking provider behavior into scheduling."""

    def __init__(self, contract: dict[str, Any], backend: Any) -> None:
        try:
            validate_contract(contract)
        except ContractError as exc:
            raise BackendContractError(str(exc)) from exc
        self.contract = copy.deepcopy(contract)
        self.backend = backend
        self.offer: dict[str, Any] = {}
        self._active: dict[tuple[str, ...], dict[str, Any]] = {}
        self._fenced: set[tuple[str, ...]] = set()

    @property
    def identity_fields(self) -> list[str]:
        return list(self.contract["identity_fields"])

    def _binding(self, value: dict[str, Any]) -> dict[str, str]:
        if not isinstance(value, dict):
            raise BackendContractError("execution binding must be an object")
        binding: dict[str, str] = {}
        for field in self.identity_fields:
            item = value.get(field)
            if not isinstance(item, str) or not item:
                raise BackendContractError(f"execution binding requires {field}")
            binding[field] = item
        return binding

    def _execution_key(self, binding: dict[str, str]) -> tuple[str, ...]:
        return tuple(binding[field] for field in self.identity_fields)

    def _logical_key(self, binding: dict[str, str]) -> tuple[str, ...]:
        replaced = {"dispatch_id", "execution_unit_id"}
        return tuple(
            binding[field] for field in self.identity_fields if field not in replaced
        )

    def negotiate(self, *, required: dict[str, Any] | None = None) -> dict[str, Any]:
        offer = self.backend.discover()
        if not isinstance(offer, dict):
            raise BackendContractError("backend discovery must return an object")
        if offer.get("protocol_version") != self.contract["protocol_version"]:
            raise BackendContractError("backend protocol version is unsupported")
        for field in ("backend_id", "backend_version"):
            if not isinstance(offer.get(field), str) or not offer[field]:
                raise BackendContractError(f"backend offer requires {field}")

        lifecycle = offer.get("lifecycle")
        expected_lifecycle = set(
            self.contract["capability_domains"]["lifecycle"]["required"]
        )
        if not isinstance(lifecycle, list) or not expected_lifecycle <= set(lifecycle):
            raise BackendContractError("backend lacks mandatory lifecycle capabilities")

        containment = offer.get("containment")
        definition = self.contract["capability_domains"]["containment"]
        if not isinstance(containment, dict):
            raise BackendContractError("backend containment offer must be an object")
        if set(containment) != set(definition["required_fields"]):
            raise BackendContractError("backend containment offer is incomplete")
        for field, value in containment.items():
            if value not in definition["values"][field]:
                raise BackendContractError(
                    f"backend containment {field} value is unsupported"
                )
        provider_capabilities = offer.get("provider_capabilities")
        if not isinstance(provider_capabilities, list) or any(
            not isinstance(value, str) or not value
            for value in provider_capabilities
        ):
            raise BackendContractError("provider capabilities must be a string array")

        required = required or {}
        for operation in required.get("lifecycle") or []:
            if operation not in lifecycle:
                raise BackendContractError(
                    f"backend lacks lifecycle capability {operation}"
                )
        for field, value in (required.get("containment") or {}).items():
            if field not in containment or containment[field] != value:
                raise BackendContractError(
                    f"backend containment {field} does not satisfy required value {value}"
                )
        for capability in required.get("provider_capabilities") or []:
            if capability not in provider_capabilities:
                raise BackendContractError(
                    f"backend route lacks provider capability {capability}"
                )
        self.offer = copy.deepcopy(offer)
        return copy.deepcopy(offer)

    def _require_negotiated(self) -> None:
        if not self.offer:
            raise BackendContractError("backend capabilities must be negotiated before launch")

    def _validate_envelope(
        self, binding: dict[str, str], envelope: dict[str, Any]
    ) -> None:
        if not isinstance(envelope, dict) or envelope.get("kind") != "job":
            raise BackendContractError("launch requires a phase-worker job envelope")
        if envelope.get("protocol_version") != 1 or envelope.get("fresh_context") is not True:
            raise BackendContractError("launch requires a fresh phase context")
        serialized = json.dumps(envelope, sort_keys=True)
        if any(
            f'"{field}"' in serialized
            for field in self.contract["fresh_context"]["forbidden_fields"]
        ):
            raise BackendContractError("launch requires a fresh phase context without session state")
        for field, expected in binding.items():
            if envelope.get(field) != expected:
                raise BackendContractError(
                    f"phase envelope does not match immutable binding {field}"
                )

    def _validate_receipt(
        self, receipt: dict[str, Any], binding: dict[str, str], label: str
    ) -> dict[str, Any]:
        if not isinstance(receipt, dict):
            raise BackendContractError(f"backend {label} receipt must be an object")
        observed = receipt.get("binding")
        if observed != binding:
            raise BackendContractError(f"backend {label} receipt has a stale binding")
        return receipt

    def launch(
        self, binding_value: dict[str, Any], envelope: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_negotiated()
        binding = self._binding(binding_value)
        self._validate_envelope(binding, envelope)
        logical = self._logical_key(binding)
        for record in self._active.values():
            if record["logical_key"] == logical and not record["fenced"]:
                raise BackendContractError("logical phase already has an active execution")
        receipt = self._validate_receipt(
            self.backend.launch(copy.deepcopy(binding), copy.deepcopy(envelope)),
            binding,
            "launch",
        )
        for field in ("backend_instance_id", "backend_handle", "process_identity"):
            if not isinstance(receipt.get(field), str) or not receipt[field]:
                raise BackendContractError(f"backend launch receipt requires {field}")
        key = self._execution_key(binding)
        self._active[key] = {
            "binding": binding,
            "logical_key": logical,
            "launch": copy.deepcopy(receipt),
            "fenced": False,
        }
        self._fenced.discard(key)
        return copy.deepcopy(receipt)

    def _record(self, binding_value: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        binding = self._binding(binding_value)
        key = self._execution_key(binding)
        record = self._active.get(key)
        if record is None or record.get("fenced") or key in self._fenced:
            raise BackendContractError("stale or fenced execution cannot report activity")
        return binding, record

    def attach(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        return copy.deepcopy(
            self._validate_receipt(
                self.backend.attach(binding, record["launch"]), binding, "attach"
            )
        )

    def heartbeat(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        return copy.deepcopy(
            self._validate_receipt(
                self.backend.heartbeat(binding, record["launch"]), binding, "heartbeat"
            )
        )

    def progress(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        return copy.deepcopy(
            self._validate_receipt(
                self.backend.progress(binding, record["launch"]), binding, "progress"
            )
        )

    def terminal(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        result = self._validate_receipt(
            self.backend.terminal(binding, record["launch"]), binding, "terminal"
        )
        if result.get("status") != "terminal":
            raise BackendContractError("terminal operation did not return terminal evidence")
        return copy.deepcopy(result)

    def inspect(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        result = self._validate_receipt(
            self.backend.inspect(binding, record["launch"]), binding, "inspection"
        )
        status = result.get("status")
        if status in {"timeout", "permission_denied"}:
            result = {
                **result,
                "status": "unknown",
                "reason": "inspection_timeout" if status == "timeout" else "inspection_permission_denied",
            }
            status = "unknown"
        if status not in self.contract["inspection"]["statuses"]:
            raise BackendContractError("backend inspection returned an unsupported status")
        result["replacement_safe"] = status in self.contract["inspection"][
            "replacement_safe_statuses"
        ]
        if status == "absent":
            absence = result.get("absence_receipt")
            required = self.contract["inspection"]["absence_requires"]
            if not isinstance(absence, dict) or any(
                not isinstance(absence.get(field), str) or not absence[field]
                for field in required
            ):
                raise BackendContractError("absence requires a mechanical identity receipt")
            launch = record["launch"]
            if (
                absence["backend_instance_id"] != launch["backend_instance_id"]
                or absence["backend_handle"] != launch["backend_handle"]
                or absence["original_process_identity"] != launch["process_identity"]
            ):
                raise BackendContractError("absence receipt does not bind the original process")
        elif status == "unknown":
            result.pop("absence_receipt", None)
            result["replacement_safe"] = False
        return copy.deepcopy(result)

    def cancel(self, binding_value: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise BackendContractError("cancellation reason is required")
        binding, record = self._record(binding_value)
        result = self._validate_receipt(
            self.backend.cancel(binding, record["launch"], reason.strip()),
            binding,
            "cancellation",
        )
        status = result.get("status")
        safe = status in self.contract["cancellation"]["safe_results"]
        result["replacement_safe"] = safe
        if status == "acknowledged":
            if any(
                not isinstance(result.get(field), str) or not result[field]
                for field in self.contract["cancellation"]["acknowledgement_requires"]
            ):
                raise BackendContractError("cancellation acknowledgement is unauthenticated")
            self.fence(binding, reason="backend acknowledged cancellation")
        elif status not in self.contract["cancellation"]["unsafe_results"]:
            raise BackendContractError("backend cancellation returned an unsupported status")
        return copy.deepcopy(result)

    def fence(self, binding_value: dict[str, Any], *, reason: str) -> dict[str, Any]:
        binding = self._binding(binding_value)
        if not isinstance(reason, str) or not reason.strip():
            raise BackendContractError("fence reason is required")
        key = self._execution_key(binding)
        record = self._active.get(key)
        if record is None:
            raise BackendContractError("cannot fence an unknown execution")
        record["fenced"] = True
        self._fenced.add(key)
        return {
            "status": "fenced",
            "replacement_safe": True,
            "binding": copy.deepcopy(binding),
            "fence_receipt": _digest(
                {"binding": binding, "reason": reason.strip(), "launch": record["launch"]}
            ),
        }


class DeterministicFakeBackend:
    """Credential-free backend used by the shared conformance suite."""

    def __init__(
        self,
        *,
        containment: dict[str, str] | None = None,
        inspect_status: str = "live",
        cancel_mode: str = "acknowledged",
    ) -> None:
        self.containment = containment or {
            "process": "isolated-process",
            "filesystem": "worktree",
            "network": "unrestricted",
            "credentials": "ambient",
            "process_control": "inspect-cancel-fence",
        }
        self.inspect_status = inspect_status
        self.cancel_mode = cancel_mode
        self.launch_count = 0
        self.inspect_count = 0
        self.last_envelope: dict[str, Any] = {}
        self.records: dict[str, dict[str, Any]] = {}

    def discover(self) -> dict[str, Any]:
        return {
            "backend_id": "deterministic-fake",
            "backend_version": "1",
            "protocol_version": 1,
            "lifecycle": [
                "launch", "attach", "heartbeat", "progress", "cancel", "inspect",
                "terminal",
            ],
            "containment": copy.deepcopy(self.containment),
            "provider_capabilities": [],
        }

    def launch(
        self, binding: dict[str, str], envelope: dict[str, Any]
    ) -> dict[str, Any]:
        self.launch_count += 1
        self.last_envelope = copy.deepcopy(envelope)
        handle = f"fake-handle-{self.launch_count}"
        process = f"fake-process-{self.launch_count}:birth-1"
        self.records[handle] = {
            "binding": copy.deepcopy(binding),
            "original_process_identity": process,
            "current_process_identity": process,
        }
        return {
            "status": "launched",
            "binding": copy.deepcopy(binding),
            "backend_instance_id": "fake-instance-1",
            "backend_handle": handle,
            "process_identity": process,
            "launch_receipt": _digest({"binding": binding, "handle": handle}),
        }

    def _event(
        self, binding: dict[str, str], launch: dict[str, Any], status: str
    ) -> dict[str, Any]:
        return {
            "status": status,
            "binding": copy.deepcopy(binding),
            "backend_handle": launch["backend_handle"],
            "evidence_receipt": _digest(
                {"binding": binding, "handle": launch["backend_handle"], "status": status}
            ),
        }

    def attach(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        return self._event(binding, launch, "attached")

    def heartbeat(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        return self._event(binding, launch, "heartbeat")

    def progress(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        return self._event(binding, launch, "progress")

    def terminal(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        return self._event(binding, launch, "terminal")

    def cancel(
        self, binding: dict[str, str], launch: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        result = self._event(binding, launch, self.cancel_mode)
        if self.cancel_mode == "acknowledged":
            result.update(
                {
                    "cancellation_id": _digest({"binding": binding, "reason": reason})[:24],
                    "cancellation_receipt": _digest(
                        {"binding": binding, "reason": reason, "status": "acknowledged"}
                    ),
                }
            )
        return result

    def inspect(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        self.inspect_count += 1
        record = self.records[launch["backend_handle"]]
        if record["current_process_identity"] != record["original_process_identity"]:
            result = self._event(binding, launch, "absent")
            result["reason"] = "process_identity_reused"
            result["absence_receipt"] = {
                "backend_instance_id": launch["backend_instance_id"],
                "backend_handle": launch["backend_handle"],
                "original_process_identity": launch["process_identity"],
                "inspection_receipt": _digest(record),
            }
            return result
        return self._event(binding, launch, self.inspect_status)

    def reuse_process(self, handle: str) -> None:
        self.records[handle]["current_process_identity"] = "replacement-process:birth-2"
