#!/usr/bin/env python3
"""Durable, provider-neutral lifecycle for one disposable phase execution.

The supervisor owns this state machine. Adapters only advertise capabilities
and transport envelopes; they do not choose identities, retry policy, or
whether a replacement may start.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from phase_worker_contract import (
    ContractError,
    load_json,
    validate_contract,
    validate_envelope,
)


class PhaseExecutionError(RuntimeError):
    pass


STATE_SCHEMA = "orka.phase-execution-state/v1"
REPLACEABLE_OUTCOMES = {
    "malformed_result",
    "recoverable",
    "timeout_with_progress",
    "timeout_without_progress",
}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _default_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class PhaseExecutionRuntime:
    """Apply phase-worker protocol envelopes to JSON-serializable state."""

    def __init__(
        self,
        contract_path: Path,
        *,
        repository_id: str,
        supervisor_fence: str,
        id_factory: Callable[[str], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not repository_id or not supervisor_fence:
            raise PhaseExecutionError("repository and supervisor fence are required")
        try:
            self.contract = load_json(contract_path)
            validate_contract(self.contract)
        except ContractError as exc:
            raise PhaseExecutionError(str(exc)) from exc
        self.repository_id = repository_id
        self.supervisor_fence = supervisor_fence
        self.id_factory = id_factory or _default_id
        self.clock = clock or time.time

    def _id(self, prefix: str) -> str:
        value = self.id_factory(prefix)
        if not isinstance(value, str) or not value:
            raise PhaseExecutionError(f"{prefix} identity must be a nonempty string")
        return value

    def create_job(
        self,
        *,
        ticket_id: str,
        phase: str,
        attempt_token: str,
        worktree_id: str,
        sanitized_input: dict[str, Any],
        job_id: str | None = None,
    ) -> dict[str, Any]:
        values = {
            "ticket_id": ticket_id,
            "phase": phase,
            "attempt_token": attempt_token,
            "worktree_id": worktree_id,
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise PhaseExecutionError("phase job identity values must be nonempty strings")
        if not isinstance(sanitized_input, dict):
            raise PhaseExecutionError("sanitized phase input must be an object")
        retained = json.dumps(sanitized_input, sort_keys=True)
        if any(
            forbidden in retained
            for forbidden in (
                '"conversation_id"',
                '"previous_conversation_id"',
                '"provider_session_id"',
                '"resume_session"',
            )
        ):
            raise PhaseExecutionError("phase input cannot retain model conversation state")
        stable_job_id = job_id or self._id("job")
        operation_key = canonical_digest(
            {
                "job_id": stable_job_id,
                "ticket_id": ticket_id,
                "phase": phase,
                "attempt_token": attempt_token,
                "repository_id": self.repository_id,
            }
        )
        return {
            "schema_version": STATE_SCHEMA,
            "job_id": stable_job_id,
            **values,
            "repository_id": self.repository_id,
            "supervisor_fence": self.supervisor_fence,
            "sanitized_input": sanitized_input,
            "external_operation_key": operation_key,
            "status": "queued",
            "active": None,
            "executions": [],
            "terminal": None,
        }

    def _validate_offer(self, offer: dict[str, Any]) -> None:
        try:
            validate_envelope(self.contract, offer)
        except ContractError as exc:
            raise PhaseExecutionError(f"adapter capability rejected: {exc}") from exc

    def _identity(
        self, state: dict[str, Any], dispatch_id: str, execution_unit_id: str
    ) -> dict[str, str]:
        return {
            "job_id": state["job_id"],
            "ticket_id": state["ticket_id"],
            "phase": state["phase"],
            "attempt_token": state["attempt_token"],
            "supervisor_fence": state["supervisor_fence"],
            "repository_id": state["repository_id"],
            "worktree_id": state["worktree_id"],
            "dispatch_id": dispatch_id,
            "execution_unit_id": execution_unit_id,
        }

    def dispatch(
        self,
        state: dict[str, Any],
        capability_offer: dict[str, Any],
        *,
        dispatch_id: str | None = None,
        execution_unit_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_state(state)
        self._validate_offer(capability_offer)
        if state["status"] == "terminal":
            raise PhaseExecutionError("terminal phase jobs cannot be dispatched")
        if state.get("active") is not None:
            raise PhaseExecutionError("phase job already has an active execution")
        if state["status"] not in {"queued", "replaceable"}:
            raise PhaseExecutionError(
                f"phase job cannot dispatch from {state['status']}"
            )
        dispatch_id = dispatch_id or self._id("dispatch")
        execution_unit_id = execution_unit_id or self._id("execution")
        identity = self._identity(state, dispatch_id, execution_unit_id)
        sanitized_input = {
            **state["sanitized_input"],
            "_orka_external_operation_key": state["external_operation_key"],
        }
        envelope = {
            "kind": "job",
            "protocol_version": self.contract["protocol_version"],
            **identity,
            "sanitized_input": sanitized_input,
            "required_capabilities": list(self.contract["capabilities"]["mandatory"]),
            "fresh_context": True,
        }
        try:
            validate_envelope(self.contract, envelope, expected_identity=identity)
        except ContractError as exc:
            raise PhaseExecutionError(str(exc)) from exc
        record = {
            "identity": identity,
            "job": envelope,
            "capability_offer_digest": canonical_digest(capability_offer),
            "external_operation_key": state["external_operation_key"],
            "status": "running",
            "started_at": self.clock(),
            "last_progress_sequence": 0,
            "last_heartbeat_at": "",
            "cancellation": None,
            "events": [],
            "terminal": None,
        }
        state["executions"].append(record)
        state["active"] = record
        state["status"] = "running"
        return record

    def bind_attachment(
        self, state: dict[str, Any], adapter_identity: dict[str, Any]
    ) -> dict[str, Any]:
        """Bind host execution evidence to the exact protocol execution once."""

        active = self._active(state)
        if (
            not isinstance(adapter_identity, dict)
            or not isinstance(adapter_identity.get("invocation_id"), str)
            or not adapter_identity["invocation_id"]
        ):
            raise PhaseExecutionError(
                "adapter attachment requires an execution invocation identity"
            )
        binding = {
            "identity": dict(active["identity"]),
            "adapter_execution_identity": dict(adapter_identity),
        }
        binding["binding_digest"] = canonical_digest(binding)
        prior = active.get("attachment")
        if prior is not None:
            if prior.get("binding_digest") == binding["binding_digest"]:
                return {"applied": False, "duplicate": True, **prior}
            raise PhaseExecutionError("phase execution attachment is already bound")
        active["attachment"] = binding
        return {"applied": True, "duplicate": False, **binding}

    def _validate_state(self, state: dict[str, Any]) -> None:
        required = {
            "schema_version",
            "job_id",
            "ticket_id",
            "phase",
            "attempt_token",
            "supervisor_fence",
            "repository_id",
            "worktree_id",
            "sanitized_input",
            "external_operation_key",
            "status",
            "active",
            "executions",
            "terminal",
        }
        if not isinstance(state, dict) or not required <= set(state):
            raise PhaseExecutionError("phase execution state is incomplete")
        if state["schema_version"] != STATE_SCHEMA:
            raise PhaseExecutionError("unsupported phase execution state schema")
        if state["repository_id"] != self.repository_id:
            raise PhaseExecutionError("phase state belongs to another repository")
        if state["supervisor_fence"] != self.supervisor_fence:
            raise PhaseExecutionError("phase state belongs to another supervisor fence")
        if not isinstance(state["executions"], list):
            raise PhaseExecutionError("phase executions must be an array")

    def _active(self, state: dict[str, Any]) -> dict[str, Any]:
        self._validate_state(state)
        active = state.get("active")
        if not isinstance(active, dict):
            raise PhaseExecutionError("phase job has no active execution")
        return active

    def _duplicate_event(
        self, state: dict[str, Any], envelope: dict[str, Any]
    ) -> dict[str, Any] | None:
        digest = canonical_digest(envelope)
        for execution in reversed(state["executions"]):
            terminal = execution.get("terminal")
            if isinstance(terminal, dict) and terminal.get("digest") == digest:
                return {
                    "applied": False,
                    "duplicate": True,
                    "outcome": terminal.get("outcome"),
                }
            for event in execution.get("events") or []:
                if event.get("digest") == digest:
                    return {"applied": False, "duplicate": True}
        return None

    def ingest(
        self, state: dict[str, Any], envelope: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_state(state)
        duplicate = self._duplicate_event(state, envelope)
        if duplicate is not None:
            return duplicate
        active = self._active(state)
        identity = active["identity"]
        try:
            validate_envelope(self.contract, envelope, expected_identity=identity)
        except ContractError as exc:
            raise PhaseExecutionError(str(exc)) from exc
        kind = envelope["kind"]
        digest = canonical_digest(envelope)
        if kind == "progress":
            expected = int(active["last_progress_sequence"]) + 1
            if envelope["sequence"] != expected:
                raise PhaseExecutionError(
                    f"progress sequence must be exactly {expected}"
                )
            active["last_progress_sequence"] = envelope["sequence"]
            active["events"].append(
                {"kind": kind, "digest": digest, "envelope": envelope}
            )
            return {"applied": True, "duplicate": False}
        if kind == "heartbeat":
            active["last_heartbeat_at"] = envelope["observed_at"]
            active["events"].append(
                {"kind": kind, "digest": digest, "envelope": envelope}
            )
            return {"applied": True, "duplicate": False}
        if kind == "cancellation_ack":
            cancellation = active.get("cancellation") or {}
            if envelope["cancellation_id"] != cancellation.get("cancellation_id"):
                raise PhaseExecutionError("cancellation id does not match active request")
            return self._finish(
                state,
                active,
                outcome="cancelled_attempt",
                digest=digest,
                envelope=envelope,
                replaceable=False,
            )
        if kind == "terminal":
            return self._finish(
                state,
                active,
                outcome=envelope["outcome"],
                digest=digest,
                envelope=envelope,
                replaceable=envelope["outcome"] in REPLACEABLE_OUTCOMES,
            )
        raise PhaseExecutionError(f"{kind} cannot be ingested as worker evidence")

    def _finish(
        self,
        state: dict[str, Any],
        active: dict[str, Any],
        *,
        outcome: str,
        digest: str,
        envelope: dict[str, Any],
        replaceable: bool,
    ) -> dict[str, Any]:
        terminal = {
            "outcome": outcome,
            "digest": digest,
            "envelope": envelope,
            "observed_at": self.clock(),
        }
        active["terminal"] = terminal
        active["status"] = "replaceable" if replaceable else "terminal"
        state["active"] = None
        state["status"] = "replaceable" if replaceable else "terminal"
        state["terminal"] = terminal
        return {
            "applied": True,
            "duplicate": False,
            "outcome": outcome,
            "replaceable": replaceable,
        }

    def reject_output(self, state: dict[str, Any], diagnostic: str) -> dict[str, Any]:
        active = self._active(state)
        if not isinstance(diagnostic, str) or not diagnostic.strip():
            raise PhaseExecutionError("invalid output requires a diagnostic")
        bounded = diagnostic.strip()[:2000]
        envelope = {
            "kind": "supervisor_rejection",
            **active["identity"],
            "outcome": "malformed_result",
            "diagnostic": bounded,
        }
        self._finish(
            state,
            active,
            outcome="malformed_result",
            digest=canonical_digest(envelope),
            envelope=envelope,
            replaceable=True,
        )
        return {"outcome": "malformed_result", "diagnostic": bounded}

    def request_cancel(
        self, state: dict[str, Any], *, reason: str, deadline: str
    ) -> dict[str, Any]:
        active = self._active(state)
        if active.get("cancellation") is not None:
            return active["cancellation"]
        if not reason.strip() or not deadline.strip():
            raise PhaseExecutionError("cancellation requires reason and deadline")
        envelope = {
            "kind": "cancellation",
            "protocol_version": self.contract["protocol_version"],
            **active["identity"],
            "cancellation_id": self._id("cancel"),
            "reason": reason.strip()[:2000],
            "deadline": deadline.strip(),
        }
        try:
            validate_envelope(
                self.contract, envelope, expected_identity=active["identity"]
            )
        except ContractError as exc:
            raise PhaseExecutionError(str(exc)) from exc
        active["cancellation"] = envelope
        active["status"] = "cancelling"
        state["status"] = "cancelling"
        return envelope

    def fence_cancellation(
        self, state: dict[str, Any], cancellation_id: str, receipt: str
    ) -> dict[str, Any]:
        active = self._active(state)
        cancellation = active.get("cancellation") or {}
        if cancellation.get("cancellation_id") != cancellation_id:
            raise PhaseExecutionError("cancellation id does not match active request")
        if not isinstance(receipt, str) or not receipt.strip():
            raise PhaseExecutionError("fencing requires an absence or deadline receipt")
        fenced = {
            "status": "fenced",
            "cancellation_id": cancellation_id,
            "execution_unit_id": active["identity"]["execution_unit_id"],
            "receipt": receipt.strip()[:2000],
            "observed_at": self.clock(),
        }
        active["status"] = "fenced"
        active["terminal"] = {
            "outcome": "cancelled_attempt",
            "digest": canonical_digest(fenced),
            "envelope": fenced,
            "observed_at": fenced["observed_at"],
        }
        state["active"] = None
        state["status"] = "replaceable"
        state["terminal"] = active["terminal"]
        return fenced

    def reconcile_restart(
        self, state: dict[str, Any], observation: dict[str, Any]
    ) -> dict[str, Any]:
        active = self._active(state)
        identity = active["identity"]
        for field in ("dispatch_id", "execution_unit_id"):
            if observation.get(field) != identity[field]:
                raise PhaseExecutionError(
                    f"restart observation {field} does not match active execution"
                )
        status = observation.get("status")
        if status == "live":
            return {"action": "keep-running", "duplicate_worker": False}
        if status not in {"absent", "terminal"}:
            raise PhaseExecutionError("restart observation status is unsupported")
        receipt = observation.get("receipt")
        if not isinstance(receipt, str) or not receipt.strip():
            raise PhaseExecutionError("stopped execution requires a mechanical receipt")
        self.reject_output(state, f"execution {status} during supervisor restart: {receipt}")
        return {"action": "replace", "duplicate_worker": False}
