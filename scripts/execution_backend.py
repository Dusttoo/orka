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
from pathlib import Path
from typing import Any

from execution_backend_contract import ContractError, validate_contract
from phase_worker_contract import (
    ContractError as PhaseContractError,
    load_json as load_phase_contract,
    validate_contract as validate_phase_contract,
    validate_envelope as validate_phase_envelope,
)


class BackendContractError(RuntimeError):
    pass


class TransactionalBackendState:
    """Supervisor-owned persistence adapter for production backend evidence."""

    def __init__(self, store: Any, *, writer_identity: str) -> None:
        self.store = store
        self.writer_identity = writer_identity

    @staticmethod
    def _key(binding: dict[str, str]) -> str:
        return _digest(binding)

    def consume(
        self,
        binding: dict[str, str],
        *,
        backend_id: str,
        envelope_digest: str,
    ) -> None:
        try:
            self.store.consume_execution_key(
                binding=binding,
                backend_id=backend_id,
                envelope_digest=envelope_digest,
                idempotency_key=f"execution-intent:{self._key(binding)}",
                writer_identity=self.writer_identity,
            )
        except Exception as exc:
            raise BackendContractError(str(exc)) from exc

    def record(
        self,
        binding: dict[str, str],
        operation: str,
        receipt: dict[str, Any],
        target_state: str,
    ) -> None:
        try:
            current = self.store.execution_record(binding)
            if (
                operation in {"heartbeat", "progress", "inspect"}
                and isinstance(current, dict)
                and current.get("state") == "cancelling"
            ):
                target_state = "cancelling"
            self.store.record_execution_receipt(
                binding=binding,
                operation=operation,
                receipt=receipt,
                target_state=target_state,
                idempotency_key=(
                    f"execution-receipt:{self._key(binding)}:{operation}:"
                    f"{_digest(receipt)}"
                ),
                writer_identity=self.writer_identity,
            )
        except Exception as exc:
            raise BackendContractError(str(exc)) from exc

    def load(self, binding: dict[str, str]) -> dict[str, Any] | None:
        try:
            return self.store.execution_record(binding)
        except Exception as exc:
            raise BackendContractError(str(exc)) from exc


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _launch_receipt_digest(
    binding: dict[str, str],
    *,
    backend_instance_id: str,
    backend_handle: str,
    process_identity: str,
    envelope_digest: str,
) -> str:
    return _digest(
        {
            "binding": binding,
            "backend_instance_id": backend_instance_id,
            "backend_handle": backend_handle,
            "process_identity": process_identity,
            "envelope_digest": envelope_digest,
        }
    )


def _cancellation_receipt_digest(
    binding: dict[str, str],
    launch: dict[str, Any],
    request: dict[str, Any],
    acknowledgement: dict[str, Any],
) -> str:
    return _digest(
        {
            "binding": binding,
            "backend_instance_id": launch["backend_instance_id"],
            "backend_handle": launch["backend_handle"],
            "process_identity": launch["process_identity"],
            "launch_receipt": launch["launch_receipt"],
            "request_digest": _digest(request),
            "acknowledgement_digest": _digest(acknowledgement),
        }
    )


class BackendCoordinator:
    """Validate one backend without leaking provider behavior into scheduling."""

    def __init__(
        self,
        contract: dict[str, Any],
        backend: Any,
        *,
        allow_test_backend: bool = False,
        phase_contract_path: Path | None = None,
        state_store: TransactionalBackendState | None = None,
    ) -> None:
        try:
            validate_contract(contract)
        except ContractError as exc:
            raise BackendContractError(str(exc)) from exc
        self.contract = copy.deepcopy(contract)
        self.backend = backend
        self.allow_test_backend = allow_test_backend
        self.state_store = state_store
        try:
            self.phase_contract = load_phase_contract(
                phase_contract_path
                or Path(__file__).resolve().parent.parent
                / "contracts/phase-worker-protocol-v1.json"
            )
            validate_phase_contract(self.phase_contract)
        except PhaseContractError as exc:
            raise BackendContractError(str(exc)) from exc
        self.offer: dict[str, Any] = {}
        self._active: dict[tuple[str, ...], dict[str, Any]] = {}
        self._fenced: set[tuple[str, ...]] = set()
        self._terminal: set[tuple[str, ...]] = set()
        # The in-memory index supports credential-free conformance tests.
        # Production supplies TransactionalBackendState, which consumes the
        # same key in the supervisor event store before backend invocation.
        self._launched_keys: set[tuple[str, ...]] = set()

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
        if offer.get("test_only") is True and not self.allow_test_backend:
            raise BackendContractError(
                "test-only backend requires explicit conformance mode"
            )
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
    ) -> dict[str, str]:
        if not isinstance(envelope, dict):
            raise BackendContractError("launch requires a phase-worker job envelope")
        ticket_id = envelope.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            raise BackendContractError("phase job invalid: job is missing required field ticket_id")
        identity = {**binding, "ticket_id": ticket_id}
        try:
            validate_phase_envelope(
                self.phase_contract, envelope, expected_identity=identity
            )
        except PhaseContractError as exc:
            raise BackendContractError(f"phase job invalid: {exc}") from exc
        return identity

    def _validate_receipt(
        self, receipt: dict[str, Any], binding: dict[str, str], label: str
    ) -> dict[str, Any]:
        if not isinstance(receipt, dict):
            raise BackendContractError(f"backend {label} receipt must be an object")
        observed = receipt.get("binding")
        if observed != binding:
            raise BackendContractError(f"backend {label} receipt has a stale binding")
        return receipt

    def _validate_launch_receipt(
        self,
        receipt: dict[str, Any],
        binding: dict[str, str],
        envelope_digest: str,
    ) -> dict[str, Any]:
        receipt = self._validate_receipt(receipt, binding, "launch")
        for field in ("backend_instance_id", "backend_handle", "process_identity"):
            if not isinstance(receipt.get(field), str) or not receipt[field]:
                raise BackendContractError(f"backend launch receipt requires {field}")
        if receipt.get("envelope_digest") != envelope_digest:
            raise BackendContractError("launch receipt does not bind the phase envelope")
        expected = _launch_receipt_digest(
            binding,
            backend_instance_id=receipt["backend_instance_id"],
            backend_handle=receipt["backend_handle"],
            process_identity=receipt["process_identity"],
            envelope_digest=envelope_digest,
        )
        if receipt.get("launch_receipt") != expected:
            raise BackendContractError(
                "launch receipt is missing or mechanically mismatched"
            )
        return receipt

    def launch(
        self, binding_value: dict[str, Any], envelope: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_negotiated()
        binding = self._binding(binding_value)
        phase_identity = self._validate_envelope(binding, envelope)
        logical = self._logical_key(binding)
        for record in self._active.values():
            if record["logical_key"] == logical and not record["fenced"]:
                raise BackendContractError("logical phase already has an active execution")
        key = self._execution_key(binding)
        if key in self._launched_keys:
            raise BackendContractError(
                "previously launched execution identity cannot be reused"
            )
        # A launch may have occurred even when its receipt is malformed or the
        # transport fails after dispatch, so this identity is consumed before
        # invoking the backend and remains a tombstone. Production persistence
        # performs this write transactionally before process/provider work.
        self._launched_keys.add(key)
        envelope_digest = _digest(envelope)
        if self.state_store is not None:
            self.state_store.consume(
                binding,
                backend_id=str(self.offer["backend_id"]),
                envelope_digest=envelope_digest,
            )
        try:
            receipt = self._validate_launch_receipt(
                self.backend.launch(copy.deepcopy(binding), copy.deepcopy(envelope)),
                binding,
                envelope_digest,
            )
        except Exception as exc:
            if self.state_store is not None:
                self.state_store.record(
                    binding,
                    "uncertain",
                    {
                        "binding": copy.deepcopy(binding),
                        "status": "uncertain",
                        "diagnostic_digest": _digest(
                            {"type": type(exc).__name__, "message": str(exc)[:2000]}
                        ),
                    },
                    "uncertain",
                )
            raise
        self._active[key] = {
            "binding": binding,
            "phase_identity": phase_identity,
            "logical_key": logical,
            "launch": copy.deepcopy(receipt),
            "fenced": False,
        }
        if self.state_store is not None:
            self.state_store.record(binding, "launch", receipt, "launched")
        return copy.deepcopy(receipt)

    def restore(
        self,
        binding_value: dict[str, Any],
        envelope: dict[str, Any],
        launch_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Rehydrate one exact active execution without invoking launch again."""

        self._require_negotiated()
        binding = self._binding(binding_value)
        phase_identity = self._validate_envelope(binding, envelope)
        persisted = self.state_store.load(binding) if self.state_store is not None else None
        persisted_terminal: dict[str, Any] | None = None
        if persisted is not None:
            if persisted.get("state") in {"fenced", "uncertain"}:
                raise BackendContractError(
                    f"execution backend record is not active: {persisted.get('state')}"
                )
            if persisted.get("backend_id") != self.offer.get("backend_id"):
                raise BackendContractError("execution backend identity changed after launch")
            launch_receipt = persisted.get("launch_receipt")
            if persisted.get("state") == "terminal":
                persisted_terminal = persisted.get("terminal_receipt")
                if not isinstance(persisted_terminal, dict):
                    raise BackendContractError(
                        "terminal execution backend record lacks its receipt"
                    )
        envelope_digest = _digest(envelope)
        receipt = self._validate_launch_receipt(
            launch_receipt or {}, binding, envelope_digest
        )
        key = self._execution_key(binding)
        self._launched_keys.add(key)
        self._active[key] = {
            "binding": binding,
            "phase_identity": phase_identity,
            "logical_key": self._logical_key(binding),
            "launch": copy.deepcopy(receipt),
            "fenced": False,
            "persisted_terminal": copy.deepcopy(persisted_terminal),
        }
        if persisted_terminal is not None:
            self._validate_receipt(persisted_terminal, binding, "terminal")
            self._validate_terminal_evidence(
                persisted_terminal, self._active[key]
            )
            self._terminal.add(key)
        return copy.deepcopy(receipt)

    def _record(
        self, binding_value: dict[str, Any], *, allow_terminal: bool = False
    ) -> tuple[dict[str, str], dict[str, Any]]:
        binding = self._binding(binding_value)
        key = self._execution_key(binding)
        record = self._active.get(key)
        if (
            record is None
            or record.get("fenced")
            or key in self._fenced
            or (key in self._terminal and not allow_terminal)
        ):
            raise BackendContractError("stale or fenced execution cannot report activity")
        return binding, record

    def attach(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        receipt = self._validate_receipt(
            self.backend.attach(binding, record["launch"]), binding, "attach"
        )
        if self.state_store is not None:
            self.state_store.record(binding, "attach", receipt, "attached")
        return copy.deepcopy(receipt)

    def heartbeat(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        receipt = self._validate_receipt(
            self.backend.heartbeat(binding, record["launch"]), binding, "heartbeat"
        )
        if self.state_store is not None:
            self.state_store.record(binding, "heartbeat", receipt, "attached")
        return copy.deepcopy(receipt)

    def progress(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value)
        receipt = self._validate_receipt(
            self.backend.progress(binding, record["launch"]), binding, "progress"
        )
        if self.state_store is not None:
            self.state_store.record(binding, "progress", receipt, "attached")
        return copy.deepcopy(receipt)

    def terminal(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding = self._binding(binding_value)
        key = self._execution_key(binding)
        existing = self._active.get(key)
        if existing is not None and key in self._terminal:
            persisted = existing.get("persisted_terminal")
            if isinstance(persisted, dict):
                return copy.deepcopy(persisted)
        binding, record = self._record(binding_value)
        result = self._validate_receipt(
            self.backend.terminal(binding, record["launch"]), binding, "terminal"
        )
        self._validate_terminal_evidence(result, record)
        if self.state_store is not None:
            self.state_store.record(binding, "terminal", result, "terminal")
        record["persisted_terminal"] = copy.deepcopy(result)
        self._terminal.add(self._execution_key(binding))
        return copy.deepcopy(result)

    def _validate_terminal_evidence(
        self, result: dict[str, Any], record: dict[str, Any]
    ) -> None:
        if result.get("status") != "terminal":
            raise BackendContractError("terminal evidence invalid: status is not terminal")
        envelope = result.get("terminal_envelope")
        try:
            validate_phase_envelope(
                self.phase_contract,
                envelope,
                expected_identity=record["phase_identity"],
            )
        except (PhaseContractError, TypeError) as exc:
            raise BackendContractError(f"terminal evidence invalid: {exc}") from exc
        if envelope.get("kind") != "terminal":
            raise BackendContractError("terminal evidence invalid: expected terminal envelope")

    def inspect(self, binding_value: dict[str, Any]) -> dict[str, Any]:
        binding, record = self._record(binding_value, allow_terminal=True)
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
        elif status == "terminal":
            try:
                self._validate_terminal_evidence(result, record)
            except BackendContractError:
                result = {
                    "status": "unknown",
                    "binding": copy.deepcopy(binding),
                    "reason": "terminal_evidence_invalid",
                    "replacement_safe": False,
                }
        elif status == "unknown":
            result.pop("absence_receipt", None)
            result["replacement_safe"] = False
        if self.state_store is not None:
            self.state_store.record(binding, "inspect", result, "attached")
        return copy.deepcopy(result)

    def cancel(self, binding_value: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise BackendContractError("cancellation reason is required")
        binding, record = self._record(binding_value)
        cancellation_id = _digest(
            {
                "binding": binding,
                "launch_receipt": record["launch"]["launch_receipt"],
                "reason": reason.strip(),
            }
        )[:24]
        request = {
            **record["phase_identity"],
            "kind": "cancellation",
            "protocol_version": 1,
            "cancellation_id": cancellation_id,
            "reason": reason.strip(),
            "deadline": "supervisor-policy",
        }
        try:
            validate_phase_envelope(
                self.phase_contract,
                request,
                expected_identity=record["phase_identity"],
            )
        except PhaseContractError as exc:
            raise BackendContractError(f"cancellation request invalid: {exc}") from exc
        result = self._validate_receipt(
            self.backend.cancel(binding, record["launch"], request),
            binding,
            "cancellation",
        )
        status = result.get("status")
        if status == "acknowledged":
            acknowledgement = result.get("cancellation_ack")
            try:
                validate_phase_envelope(
                    self.phase_contract,
                    acknowledgement,
                    expected_identity=record["phase_identity"],
                )
            except (PhaseContractError, TypeError) as exc:
                raise BackendContractError(
                    f"cancellation acknowledgement is invalid: {exc}"
                ) from exc
            if acknowledgement.get("cancellation_id") != cancellation_id:
                raise BackendContractError(
                    "cancellation acknowledgement does not bind the request"
                )
            expected_receipt = _cancellation_receipt_digest(
                binding, record["launch"], request, acknowledgement
            )
            verifier = getattr(self.backend, "verify_cancellation_ack", None)
            if (
                result.get("cancellation_receipt") != expected_receipt
                or not callable(verifier)
                or verifier(binding, record["launch"], request, acknowledgement, expected_receipt)
                is not True
            ):
                raise BackendContractError(
                    "cancellation acknowledgement failed mechanical verification"
                )
            if self.state_store is not None:
                self.state_store.record(binding, "cancel", result, "cancelling")
            result["supervisor_fence"] = self.fence(
                binding, reason="backend acknowledged cancellation"
            )
        elif status not in self.contract["cancellation"]["unsafe_results"]:
            raise BackendContractError("backend cancellation returned an unsupported status")
        result["replacement_safe"] = status == "acknowledged"
        if self.state_store is not None and status != "acknowledged":
            self.state_store.record(binding, "cancel", result, "cancelling")
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
        receipt = {
            "status": "fenced",
            "replacement_safe": True,
            "binding": copy.deepcopy(binding),
            "fence_receipt": _digest(
                {"binding": binding, "reason": reason.strip(), "launch": record["launch"]}
            ),
        }
        if self.state_store is not None:
            self.state_store.record(binding, "fence", receipt, "fenced")
        return receipt


class DeterministicFakeBackend:
    """Credential-free test double; never a production-selectable backend."""

    def __init__(
        self,
        *,
        containment: dict[str, str] | None = None,
        inspect_status: str = "live",
        cancel_mode: str = "acknowledged",
        launch_receipt_mode: str = "valid",
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
        self.launch_receipt_mode = launch_receipt_mode
        self.terminal_mode = "valid"
        self.inspect_terminal_evidence = True
        self.launch_count = 0
        self.inspect_count = 0
        self.last_envelope: dict[str, Any] = {}
        self.records: dict[str, dict[str, Any]] = {}

    def discover(self) -> dict[str, Any]:
        return {
            "backend_id": "deterministic-fake",
            "backend_version": "1",
            "protocol_version": 1,
            "test_only": True,
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
        envelope_digest = _digest(envelope)
        self.records[handle] = {
            "binding": copy.deepcopy(binding),
            "envelope": copy.deepcopy(envelope),
            "original_process_identity": process,
            "current_process_identity": process,
        }
        result = {
            "status": "launched",
            "binding": copy.deepcopy(binding),
            "backend_instance_id": "fake-instance-1",
            "backend_handle": handle,
            "process_identity": process,
            "envelope_digest": envelope_digest,
            "launch_receipt": _launch_receipt_digest(
                binding,
                backend_instance_id="fake-instance-1",
                backend_handle=handle,
                process_identity=process,
                envelope_digest=envelope_digest,
            ),
        }
        if self.launch_receipt_mode == "missing":
            result.pop("launch_receipt")
        elif self.launch_receipt_mode == "mismatched":
            result["launch_receipt"] = "mismatched-launch-receipt"
        elif self.launch_receipt_mode == "envelope-mismatch":
            result["envelope_digest"] = "mismatched-envelope-digest"
        return result

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

    def _terminal_envelope(
        self, binding: dict[str, str], launch: dict[str, Any]
    ) -> dict[str, Any]:
        envelope = self.records[launch["backend_handle"]]["envelope"]
        result = {
            **binding,
            "ticket_id": envelope["ticket_id"],
            "kind": "terminal",
            "protocol_version": 1,
            "outcome": "recoverable",
            "summary": "deterministic fake terminal evidence",
            "artifacts": {},
            "evidence": {"launch_receipt": launch["launch_receipt"]},
        }
        if self.terminal_mode == "mismatched":
            result["execution_unit_id"] = "another-execution"
        return result

    def terminal(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        result = self._event(binding, launch, "terminal")
        result["terminal_envelope"] = self._terminal_envelope(binding, launch)
        return result

    def cancel(
        self, binding: dict[str, str], launch: dict[str, Any], request: dict[str, Any]
    ) -> dict[str, Any]:
        result = self._event(binding, launch, self.cancel_mode)
        if self.cancel_mode in {"acknowledged", "mismatched", "false-verification"}:
            result["status"] = "acknowledged"
            acknowledgement = {
                **{
                    field: request[field]
                    for field in (
                        "job_id", "ticket_id", "phase", "attempt_token",
                        "supervisor_fence", "dispatch_id", "execution_unit_id",
                    )
                },
                "kind": "cancellation_ack",
                "protocol_version": 1,
                "cancellation_id": request["cancellation_id"],
                "status": "acknowledged",
                "terminal_receipt": {
                    "backend_handle": launch["backend_handle"],
                    "process_identity": launch["process_identity"],
                },
            }
            if self.cancel_mode == "mismatched":
                acknowledgement["cancellation_id"] = "another-cancellation"
            result["cancellation_ack"] = acknowledgement
            result["cancellation_receipt"] = _cancellation_receipt_digest(
                binding, launch, request, acknowledgement
            )
        return result

    def verify_cancellation_ack(
        self,
        binding: dict[str, str],
        launch: dict[str, Any],
        request: dict[str, Any],
        acknowledgement: dict[str, Any],
        receipt: str,
    ) -> bool:
        if self.cancel_mode == "false-verification":
            return False
        return receipt == _cancellation_receipt_digest(
            binding, launch, request, acknowledgement
        )

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
        result = self._event(binding, launch, self.inspect_status)
        if self.inspect_status == "terminal" and self.inspect_terminal_evidence:
            result["terminal_envelope"] = self._terminal_envelope(binding, launch)
        return result

    def reuse_process(self, handle: str) -> None:
        self.records[handle]["current_process_identity"] = "replacement-process:birth-2"
