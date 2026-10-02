#!/usr/bin/env python3
"""Credential-free reference boundary for external engineering services.

This module is a conformance coordinator, not a production Jira/GitHub client.
It demonstrates the provider-neutral protocol without routing existing clients
behind it or granting adapters controller authority.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol

from external_service_contract import ContractError, validate_contract


class AdapterError(RuntimeError):
    """An adapter violated the external-service protocol."""


class MutationUncertainError(AdapterError):
    """A claimed mutation may have reached the provider and must be reconciled."""

    def __init__(self, reconciliation_key: str) -> None:
        super().__init__(
            f"mutation outcome is uncertain; reconcile durable key {reconciliation_key}"
        )
        self.reconciliation_key = reconciliation_key


class IntentStore(Protocol):
    """Host-owned durable boundary for external mutation intent and receipts.

    A production host supplies this boundary.  The reference coordinator never
    hides intent, idempotency, or ambiguity state in process memory.
    """

    def claim_intent(
        self, intent: dict[str, Any], *, now_seconds: float
    ) -> tuple[Literal["submit", "retry"], str]:
        """Atomically claim a never-submitted or eligible rate-limited intent.

        Implementations must make the claim durable before returning.  A
        separate get-then-put implementation is unsafe: two coordinators could
        both observe absence and submit the same provider side effect.
        """

    def record_acknowledgement(
        self,
        idempotency_key: str,
        acknowledgement: dict[str, Any],
        *,
        now_seconds: float,
    ) -> None: ...

    def get_reconcilable(self, reconciliation_key: str) -> dict[str, Any] | None: ...


class FileIntentStore:
    """Deterministic private-file implementation used by offline conformance tests.

    The supplied path is the durability boundary: callers must place it on a
    host-managed private persistent volume. It uses POSIX ``flock`` around
    every read-modify-write claim, and therefore supports cooperating local
    processes on a filesystem that honors advisory locks. Production hosts on
    a different boundary must provide an equivalent atomic ``claim_intent``.
    This class deliberately has no credentials or provider-routing knowledge.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        with self._locked():
            if not self.path.exists():
                self._write({"intents": {}, "reconcilable": {}})

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - documented POSIX boundary
            raise AdapterError("FileIntentStore requires POSIX advisory locks") from exc
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError("durable intent store is unreadable") from exc
        if not isinstance(value, dict) or set(value) != {"intents", "reconcilable"}:
            raise AdapterError("durable intent store has invalid fields")
        if not isinstance(value["intents"], dict) or not isinstance(
            value["reconcilable"], dict
        ):
            raise AdapterError("durable intent store has invalid records")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory = os.open(self.path.parent, directory_flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_intent(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._locked():
            value = self._read()["intents"].get(idempotency_key)
            if not isinstance(value, dict):
                return None
            intent = value.get("intent")
            return copy.deepcopy(intent) if isinstance(intent, dict) else None

    @staticmethod
    def _mutation_identity(intent: dict[str, Any]) -> str:
        binding = intent["binding"]
        return canonical_digest(
            {
                "repository_id": binding["repository_id"],
                "adapter_id": binding["adapter_id"],
                "adapter_version": binding["adapter_version"],
                "protocol_version": binding["protocol_version"],
                "operation": binding["operation"],
                "operation_key": binding["operation_key"],
                "external_object": binding["external_object"],
                "input_digest": intent["input_digest"],
            }
        )

    def claim_intent(
        self, intent: dict[str, Any], *, now_seconds: float
    ) -> tuple[Literal["submit", "retry"], str]:
        key = intent["idempotency_key"]
        identity = self._mutation_identity(intent)
        reconciliation_key = f"intent-{canonical_digest(intent)}"
        with self._locked():
            value = self._read()
            records = value["intents"]
            existing = records.get(key)
            if existing is None:
                for other_key, other in records.items():
                    if not isinstance(other, dict) or other_key == key:
                        continue
                    other_intent = other.get("intent")
                    if (
                        isinstance(other_intent, dict)
                        and self._mutation_identity(other_intent) == identity
                    ):
                        raise AdapterError(
                            "equivalent mutation already has a durable idempotency key"
                        )
                records[key] = {
                    "intent": copy.deepcopy(intent),
                    "state": "submitting",
                    "reconciliation_key": reconciliation_key,
                }
                value["reconcilable"][reconciliation_key] = key
                self._write(value)
                return "submit", reconciliation_key
            if not isinstance(existing, dict) or existing.get("intent") != intent:
                raise AdapterError(
                    "durable idempotency key conflicts with existing intent"
                )
            state = existing.get("state")
            if state == "rate_limited":
                retry_not_before = existing.get("retry_not_before")
                if not isinstance(retry_not_before, (int, float)) or isinstance(
                    retry_not_before, bool
                ):
                    raise AdapterError("durable rate-limit intent lacks retry timing")
                if now_seconds < retry_not_before:
                    raise AdapterError(
                        "rate-limited mutation is not eligible for retry"
                    )
                existing["state"] = "submitting"
                existing_reconciliation_key = existing.get("reconciliation_key")
                if existing_reconciliation_key != reconciliation_key:
                    raise AdapterError("durable intent reconciliation key is invalid")
                value["reconcilable"][reconciliation_key] = key
                self._write(value)
                return "retry", reconciliation_key
            if state == "ambiguous":
                raise AdapterError("ambiguous mutation requires reconciliation")
            if state == "submitting":
                raise AdapterError("prior mutation submission requires reconciliation")
            raise AdapterError("mutation idempotency key was already submitted")

    def record_acknowledgement(
        self,
        idempotency_key: str,
        acknowledgement: dict[str, Any],
        *,
        now_seconds: float,
    ) -> None:
        with self._locked():
            value = self._read()
            record = value["intents"].get(idempotency_key)
            if not isinstance(record, dict) or not isinstance(
                record.get("intent"), dict
            ):
                raise AdapterError("durable acknowledgement has no matching intent")
            if record.get("state") not in {"submitting", "ambiguous"}:
                raise AdapterError("durable acknowledgement has no claimed submission")
            record["acknowledgement"] = copy.deepcopy(acknowledgement)
            status = acknowledgement["status"]
            if status == "ambiguous":
                reconciliation_key = acknowledgement["reconciliation_key"]
                existing_key = value["reconcilable"].get(reconciliation_key)
                if existing_key is not None and existing_key != idempotency_key:
                    raise AdapterError(
                        "reconciliation_key is already bound to another intent"
                    )
                record["state"] = "ambiguous"
                value["reconcilable"][reconciliation_key] = idempotency_key
            elif status == "rate_limited":
                record["state"] = "rate_limited"
                record["retry_not_before"] = (
                    now_seconds + acknowledgement["rate_limit"]["retry_after_seconds"]
                )
                self._remove_reconciliation_keys(value, idempotency_key)
            else:
                record["state"] = "terminal"
                self._remove_reconciliation_keys(value, idempotency_key)
            self._write(value)

    @staticmethod
    def _remove_reconciliation_keys(
        value: dict[str, Any], idempotency_key: str
    ) -> None:
        for reconciliation_key, key in list(value["reconcilable"].items()):
            if key == idempotency_key:
                del value["reconcilable"][reconciliation_key]

    def get_reconcilable(self, reconciliation_key: str) -> dict[str, Any] | None:
        with self._locked():
            value = self._read()
            key = value["reconcilable"].get(reconciliation_key)
            record = value["intents"].get(key) if isinstance(key, str) else None
            intent = record.get("intent") if isinstance(record, dict) else None
            return copy.deepcopy(intent) if isinstance(intent, dict) else None


def canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterError("value is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _identity(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"type", "id"}:
        raise AdapterError(f"{label} must contain exactly type and id")
    if any(not isinstance(value[key], str) or not value[key] for key in ("type", "id")):
        raise AdapterError(f"{label} values must be nonempty strings")
    return {"type": value["type"], "id": value["id"]}


class ExternalServiceCoordinator:
    """Supervisor-side protocol enforcement for one already-selected adapter."""

    def __init__(
        self,
        contract: dict[str, Any],
        adapter: Any,
        *,
        repository_id: str,
        intent_store: IntentStore,
        allow_test_adapter: bool = False,
        now_seconds: Callable[[], float] = time.time,
    ) -> None:
        try:
            validate_contract(contract)
        except ContractError as exc:
            raise AdapterError(f"invalid external-service contract: {exc}") from exc
        if not isinstance(repository_id, str) or not repository_id:
            raise AdapterError("repository_id must be a nonempty immutable identity")
        self.contract = copy.deepcopy(contract)
        self.adapter = adapter
        self.repository_id = repository_id
        self.intent_store = intent_store
        self.now_seconds = now_seconds
        self.offer = self._validate_offer(adapter.discover())
        if self.offer.get("test_only") and not allow_test_adapter:
            raise AdapterError("test-only adapter requires explicit conformance mode")
        self._negotiated: dict[str, int] = {}

    def _validate_offer(self, offer: Any) -> dict[str, Any]:
        if not isinstance(offer, dict) or set(offer) != {
            "adapter_id",
            "adapter_version",
            "protocol_versions",
            "capabilities",
            "test_only",
        }:
            raise AdapterError("adapter capability offer has invalid fields")
        if any(
            not isinstance(offer.get(key), str) or not offer[key]
            for key in ("adapter_id", "adapter_version")
        ):
            raise AdapterError("adapter identity is incomplete")
        versions = offer.get("protocol_versions")
        if (
            not isinstance(versions, list)
            or any(not isinstance(version, int) or version < 1 for version in versions)
            or len(versions) != len(set(versions))
        ):
            raise AdapterError("adapter protocol versions are invalid")
        capabilities = offer.get("capabilities")
        if not isinstance(capabilities, dict) or any(
            not isinstance(name, str)
            or not name
            or not isinstance(version, int)
            or version < 1
            for name, version in capabilities.items()
        ):
            raise AdapterError("adapter capability offer is invalid")
        if not isinstance(offer.get("test_only"), bool):
            raise AdapterError("adapter test_only flag must be boolean")
        return copy.deepcopy(offer)

    def negotiate(self, required: dict[str, int]) -> dict[str, Any]:
        if self.contract["protocol_version"] not in self.offer["protocol_versions"]:
            raise AdapterError("incompatible adapter protocol before reservation")
        if not isinstance(required, dict) or not required:
            raise AdapterError("required capabilities must be a nonempty mapping")
        unsupported: list[dict[str, Any]] = []
        for capability, version in required.items():
            configured = self.contract["capabilities"].get(capability)
            offered = self.offer["capabilities"].get(capability)
            if (
                configured is None
                or not isinstance(version, int)
                or version < 1
                or configured["version"] < version
                or offered is None
                or offered < version
            ):
                unsupported.append(
                    {
                        "status": self.contract["unsupported"]["status"],
                        "capability": capability,
                        "requested_version": version,
                        "supported_versions": [] if offered is None else [offered],
                    }
                )
        if unsupported:
            raise AdapterError(
                "unsupported capability before reservation: "
                + json.dumps(unsupported, sort_keys=True)
            )
        self._negotiated.update(required)
        return {
            "protocol_version": self.contract["protocol_version"],
            "adapter_id": self.offer["adapter_id"],
            "adapter_version": self.offer["adapter_version"],
            "capabilities": copy.deepcopy(required),
        }

    def _require(self, operation: str, kind: str) -> None:
        capability = self.contract["capabilities"].get(operation)
        if capability is None or capability["kind"] != kind:
            raise AdapterError(f"operation {operation} is not a {kind} capability")
        if self._negotiated.get(operation, 0) < capability["version"]:
            raise AdapterError(f"operation {operation} was not negotiated")

    def _redaction_check(self, value: Any, path: str = "payload") -> None:
        forbidden = {
            key.lower() for key in self.contract["redaction"]["forbidden_keys"]
        }
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise AdapterError(f"{path} contains a non-string key")
                if key.lower() in forbidden:
                    raise AdapterError(f"{path} contains forbidden key {key}")
                self._redaction_check(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._redaction_check(child, f"{path}[{index}]")
        elif isinstance(value, float) and not math.isfinite(value):
            raise AdapterError(f"{path} contains a non-finite number")
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise AdapterError(f"{path} contains a non-JSON value")

    def _validate_normalized_fields(
        self, operation: str, kind: str, value: Any, label: str
    ) -> None:
        """Validate a capability's complete normalized object schema.

        The contract deliberately does not use a permissive JSON-schema
        dialect.  Each v1 capability declares its exact fields, required
        fields, scalar/array types, and small value constraints.  This keeps
        provider-shaped data out of durable controller state.
        """
        schema = self.contract["normalization"]["capabilities"][operation][kind]
        if not isinstance(value, dict):
            raise AdapterError(f"{label} must be a normalized object")
        self._redaction_check(value, label)
        properties = schema["properties"]
        unknown = set(value) - set(properties)
        if unknown:
            raise AdapterError(f"{label} contains unknown normalized fields")
        missing = set(schema["required"]) - set(value)
        if missing:
            raise AdapterError(f"{label} omits required normalized fields")
        if len(value) < schema["min_properties"]:
            raise AdapterError(f"{label} has too few normalized fields")
        for key, child in value.items():
            rule = properties[key]
            expected_type = rule["type"]
            valid = (
                expected_type == "string"
                and isinstance(child, str)
                or expected_type == "integer"
                and isinstance(child, int)
                and not isinstance(child, bool)
                or expected_type == "boolean"
                and isinstance(child, bool)
                or expected_type == "array"
                and isinstance(child, list)
            )
            if not valid:
                raise AdapterError(f"{label}.{key} has an invalid normalized type")
            if expected_type == "string" and len(child) < rule.get("min_length", 0):
                raise AdapterError(
                    f"{label}.{key} is shorter than the normalized minimum"
                )
            if expected_type == "integer" and child < rule.get("minimum", child):
                raise AdapterError(f"{label}.{key} is below the normalized minimum")
            if expected_type == "array":
                if len(child) < rule.get("min_items", 0):
                    raise AdapterError(f"{label}.{key} has too few normalized items")
                item_type = rule["item_type"]
                if any(
                    (item_type == "string" and not isinstance(item, str))
                    or (
                        item_type == "integer"
                        and (not isinstance(item, int) or isinstance(item, bool))
                    )
                    or (item_type == "boolean" and not isinstance(item, bool))
                    for item in child
                ):
                    raise AdapterError(
                        f"{label}.{key} has invalid normalized array items"
                    )
            if "enum" in rule and child not in rule["enum"]:
                raise AdapterError(f"{label}.{key} is outside the normalized enum")

    def _binding(
        self,
        operation: str,
        *,
        operation_key: str,
        request_id: str,
        external_object: dict[str, str],
        material: Any,
    ) -> dict[str, Any]:
        if any(
            not isinstance(value, str) or not value
            for value in (operation_key, request_id)
        ):
            raise AdapterError("operation_key and request_id must be nonempty strings")
        target = _identity(external_object, "external_object")
        allowed_types = self.contract["capabilities"][operation]["object_types"]
        if target["type"] not in allowed_types:
            raise AdapterError(
                f"external_object type {target['type']} is invalid for {operation}"
            )
        seed = {
            "repository_id": self.repository_id,
            "adapter_id": self.offer["adapter_id"],
            "adapter_version": self.offer["adapter_version"],
            "protocol_version": self.contract["protocol_version"],
            "operation": operation,
            "operation_key": operation_key,
            "request_id": request_id,
            "external_object": target,
            "material": material,
        }
        return {key: value for key, value in seed.items() if key != "material"} | {
            "request_digest": canonical_digest(seed)
        }

    def _exact_binding(self, value: Any, expected: dict[str, Any]) -> None:
        if value != expected:
            raise AdapterError("adapter response is not bound to the exact request")

    def _validate_evidence(self, value: Any, binding: dict[str, Any]) -> dict[str, Any]:
        required = set(self.contract["evidence"]["required"])
        if not isinstance(value, dict) or set(value) != required:
            raise AdapterError("normalized evidence has invalid fields")
        self._exact_binding(value.get("binding"), binding)
        if not isinstance(value.get("observed_at"), str) or not value["observed_at"]:
            raise AdapterError("normalized evidence requires observed_at")
        if not isinstance(value.get("data"), dict):
            raise AdapterError("normalized evidence data must be an object")
        self._validate_normalized_fields(
            binding["operation"], "evidence", value["data"], "evidence.data"
        )
        expected = canonical_digest(
            {
                "binding": binding,
                "observed_at": value["observed_at"],
                "data": value["data"],
            }
        )
        if value.get("evidence_digest") != expected:
            raise AdapterError("normalized evidence digest is invalid")
        return copy.deepcopy(value)

    def read(
        self,
        operation: str,
        *,
        operation_key: str,
        request_id: str,
        external_object: dict[str, str],
        query: dict[str, Any],
    ) -> dict[str, Any]:
        self._require(operation, "read")
        if not isinstance(query, dict):
            raise AdapterError("read query must be an object")
        self._validate_normalized_fields(operation, "request", query, "query")
        binding = self._binding(
            operation,
            operation_key=operation_key,
            request_id=request_id,
            external_object=external_object,
            material=query,
        )
        return self._validate_evidence(self.adapter.read(binding, query), binding)

    def read_collection(
        self,
        operation: str,
        *,
        operation_key: str,
        request_id: str,
        external_object: dict[str, str],
        query: dict[str, Any],
    ) -> dict[str, Any]:
        self._require(operation, "collection-read")
        if not isinstance(query, dict):
            raise AdapterError("collection query must be an object")
        self._validate_normalized_fields(operation, "request", query, "query")
        binding = self._binding(
            operation,
            operation_key=operation_key,
            request_id=request_id,
            external_object=external_object,
            material=query,
        )
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_items: set[tuple[str, str]] = set()
        items: list[dict[str, Any]] = []
        page_count = 0
        while True:
            if page_count >= self.contract["pagination"]["max_pages"]:
                raise AdapterError("collection exceeded the maximum page count")
            page = self.adapter.read_page(binding, query, cursor)
            if not isinstance(page, dict) or set(page) != {
                "binding",
                "page_index",
                "cursor",
                "request_digest",
                "items",
                "next_cursor",
                "complete",
                "final_proof",
            }:
                raise AdapterError("collection page has invalid fields")
            self._exact_binding(page["binding"], binding)
            if (
                page["page_index"] != page_count
                or page["cursor"] != cursor
                or page["request_digest"] != binding["request_digest"]
            ):
                raise AdapterError(
                    "collection page is out of sequence or request-bound incorrectly"
                )
            if not isinstance(page["items"], list) or not isinstance(
                page["complete"], bool
            ):
                raise AdapterError(
                    "collection page items or completion flag is invalid"
                )
            for item in page["items"]:
                if not isinstance(item, dict) or set(item) != {
                    "object_identity",
                    "data",
                }:
                    raise AdapterError("collection item has invalid fields")
                identity = _identity(
                    item["object_identity"], "collection item identity"
                )
                if (
                    identity["type"]
                    not in self.contract["capabilities"][operation]["item_types"]
                ):
                    raise AdapterError(
                        "collection item type is invalid for this operation"
                    )
                key = (identity["type"], identity["id"])
                if key in seen_items:
                    raise AdapterError("collection contains a duplicate item identity")
                if not isinstance(item["data"], dict):
                    raise AdapterError("collection item data must be an object")
                self._validate_normalized_fields(
                    operation,
                    "item",
                    item["data"],
                    "collection item data",
                )
                seen_items.add(key)
                items.append(copy.deepcopy(item))
            page_count += 1
            next_cursor = page["next_cursor"]
            if page["complete"]:
                if next_cursor is not None:
                    raise AdapterError(
                        "complete collection page cannot include next_cursor"
                    )
                proof = page["final_proof"]
                identities = [
                    f"{kind}:{identifier}" for kind, identifier in sorted(seen_items)
                ]
                required_proof = set(
                    self.contract["pagination"]["final_proof_required"]
                )
                if not isinstance(proof, dict) or set(proof) != required_proof:
                    raise AdapterError("collection completeness proof is invalid")
                expected_values = {
                    "page_count": page_count,
                    "item_count": len(items),
                    "item_identities_digest": canonical_digest(identities),
                }
                if any(
                    proof.get(key) != expected
                    for key, expected in expected_values.items()
                ):
                    raise AdapterError("collection completeness proof is invalid")
                if (
                    proof["completion_basis"]
                    not in self.contract["pagination"]["completion_bases"]
                ):
                    raise AdapterError("collection completion basis is invalid")
                provider_total = proof["provider_total"]
                if proof["completion_basis"] == "provider-total":
                    if not isinstance(provider_total, int) or provider_total != len(
                        items
                    ):
                        raise AdapterError(
                            "provider total does not prove collection completeness"
                        )
                elif provider_total is not None:
                    raise AdapterError(
                        "terminal-cursor proof cannot claim a provider total"
                    )
                return {
                    "binding": binding,
                    "items": items,
                    "final_proof": proof,
                }
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise AdapterError("collection cursor is missing or repeated")
            if page["final_proof"] is not None:
                raise AdapterError(
                    "nonterminal collection page cannot include final proof"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def mutate(
        self,
        operation: str,
        *,
        operation_key: str,
        request_id: str,
        external_object: dict[str, str],
        input_value: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(operation, "mutation")
        if (
            not isinstance(input_value, dict)
            or not isinstance(idempotency_key, str)
            or not idempotency_key
        ):
            raise AdapterError("mutation input and idempotency key are required")
        self._validate_normalized_fields(
            operation, "request", input_value, "mutation input"
        )
        binding = self._binding(
            operation,
            operation_key=operation_key,
            request_id=request_id,
            external_object=external_object,
            material={"input": input_value, "idempotency_key": idempotency_key},
        )
        intent = {
            "binding": binding,
            "idempotency_key": idempotency_key,
            "input_digest": canonical_digest(input_value),
            "input": copy.deepcopy(input_value),
        }
        # An atomic durable claim is made before provider submission.  It
        # prevents competing coordinators from issuing the same effect and
        # lets a rate-limited response retry only this exact bound intent.
        _claim, reconciliation_key = self.intent_store.claim_intent(
            intent, now_seconds=self.now_seconds()
        )
        try:
            result = self._validate_ack(
                self.adapter.mutate(copy.deepcopy(intent)), intent
            )
            self.intent_store.record_acknowledgement(
                idempotency_key, result, now_seconds=self.now_seconds()
            )
        except MutationUncertainError:
            raise
        except Exception as exc:
            # The durable claim precedes the provider call. Any exception after
            # that point may have followed a completed external side effect, so
            # expose the already-persisted reconciliation reference and never
            # turn the outcome into a blind retry.
            raise MutationUncertainError(reconciliation_key) from exc
        return result

    def _validate_ack(self, value: Any, intent: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AdapterError("mutation acknowledgement must be an object")
        base = {"binding", "status", "acknowledgement_digest"}
        status = value.get("status")
        if status not in self.contract["mutations"]["statuses"]:
            raise AdapterError("mutation acknowledgement status is invalid")
        allowed = (
            base
            | {
                "completed": {"external_object", "receipt"},
                "ambiguous": {"reconciliation_key", "intent_digest"},
                "rate_limited": {"rate_limit"},
                "rejected": {"reason"},
            }[status]
        )
        if set(value) != allowed:
            raise AdapterError("mutation acknowledgement has invalid fields")
        self._exact_binding(value.get("binding"), intent["binding"])
        if status == "completed":
            external_object = _identity(
                value["external_object"], "mutation external_object"
            )
            if external_object != intent["binding"]["external_object"]:
                raise AdapterError(
                    "completed mutation external_object is not the bound target"
                )
            if not isinstance(value["receipt"], dict):
                raise AdapterError("completed mutation requires normalized receipt")
            self._validate_normalized_fields(
                intent["binding"]["operation"],
                "receipt",
                value["receipt"],
                "mutation receipt",
            )
        elif status == "ambiguous":
            if (
                not isinstance(value["reconciliation_key"], str)
                or not value["reconciliation_key"]
            ):
                raise AdapterError("ambiguous mutation requires reconciliation_key")
            if value["intent_digest"] != canonical_digest(intent):
                raise AdapterError(
                    "ambiguous mutation does not bind its original intent"
                )
        elif status == "rate_limited":
            self._validate_rate_limit(value["rate_limit"])
        elif not isinstance(value["reason"], str) or not value["reason"]:
            raise AdapterError("rejected mutation requires a reason")
        expected = canonical_digest(
            {
                key: child
                for key, child in value.items()
                if key != "acknowledgement_digest"
            }
        )
        if value["acknowledgement_digest"] != expected:
            raise AdapterError("mutation acknowledgement digest is invalid")
        return copy.deepcopy(value)

    def _validate_rate_limit(self, value: Any) -> None:
        required = set(self.contract["retry_and_rate_limits"]["rate_limit_required"])
        if not isinstance(value, dict) or set(value) != required:
            raise AdapterError("rate-limit evidence has invalid fields")
        if not isinstance(value["scope"], str) or not value["scope"]:
            raise AdapterError("rate-limit scope is required")
        if not isinstance(value["remaining"], int) or value["remaining"] < 0:
            raise AdapterError("rate-limit remaining must be a nonnegative integer")
        if not isinstance(value["reset_at"], str) or not value["reset_at"]:
            raise AdapterError("rate-limit reset_at is required")
        if (
            not isinstance(value["retry_after_seconds"], int)
            or value["retry_after_seconds"] < 0
        ):
            raise AdapterError("rate-limit retry_after_seconds is invalid")

    def reconcile(self, reconciliation_key: str) -> dict[str, Any]:
        intent = self.intent_store.get_reconcilable(reconciliation_key)
        if intent is None:
            raise AdapterError("no uncertain intent matches reconciliation_key")
        try:
            result = self._validate_ack(
                self.adapter.reconcile(reconciliation_key, copy.deepcopy(intent)),
                intent,
            )
            self.intent_store.record_acknowledgement(
                intent["idempotency_key"], result, now_seconds=self.now_seconds()
            )
        except MutationUncertainError:
            raise
        except Exception as exc:
            raise MutationUncertainError(reconciliation_key) from exc
        return result


class DeterministicExternalServiceAdapter:
    """Offline fixture adapter for protocol conformance only."""

    def __init__(
        self,
        *,
        capabilities: dict[str, int],
        pages: list[list[dict[str, Any]]] | None = None,
        read_data: dict[str, Any] | None = None,
        mutation_status: str = "completed",
    ) -> None:
        self.capabilities = copy.deepcopy(capabilities)
        self.pages = copy.deepcopy(pages or [[]])
        self.read_data = copy.deepcopy(read_data or {"status": "Ready"})
        self.mutation_status = mutation_status
        self.read_calls = 0
        self.mutation_calls = 0
        self.reconcile_calls = 0

    def discover(self) -> dict[str, Any]:
        return {
            "adapter_id": "fixture-engineering-service",
            "adapter_version": "fixture-1",
            "protocol_versions": [1],
            "capabilities": copy.deepcopy(self.capabilities),
            "test_only": True,
        }

    def read(self, binding: dict[str, Any], _query: dict[str, Any]) -> dict[str, Any]:
        self.read_calls += 1
        observed_at = "2030-01-01T00:00:00Z"
        data = copy.deepcopy(self.read_data)
        evidence = {
            "binding": copy.deepcopy(binding),
            "observed_at": observed_at,
            "data": data,
        }
        return {**evidence, "evidence_digest": canonical_digest(evidence)}

    def read_page(
        self,
        binding: dict[str, Any],
        _query: dict[str, Any],
        cursor: str | None,
    ) -> dict[str, Any]:
        self.read_calls += 1
        index = 0 if cursor is None else int(cursor.removeprefix("page-"))
        page_items = copy.deepcopy(self.pages[index])
        complete = index == len(self.pages) - 1
        identities = [
            f"{item['object_identity']['type']}:{item['object_identity']['id']}"
            for page in self.pages
            for item in page
        ]
        return {
            "binding": copy.deepcopy(binding),
            "page_index": index,
            "cursor": cursor,
            "request_digest": binding["request_digest"],
            "items": page_items,
            "next_cursor": None if complete else f"page-{index + 1}",
            "complete": complete,
            "final_proof": (
                None
                if not complete
                else {
                    "page_count": len(self.pages),
                    "item_count": len(identities),
                    "item_identities_digest": canonical_digest(sorted(identities)),
                    "completion_basis": "provider-total",
                    "provider_total": len(identities),
                }
            ),
        }

    def _ack(self, intent: dict[str, Any], status: str) -> dict[str, Any]:
        base: dict[str, Any] = {
            "binding": copy.deepcopy(intent["binding"]),
            "status": status,
        }
        if status == "completed":
            base.update(
                external_object=copy.deepcopy(intent["binding"]["external_object"]),
                receipt={"provider_receipt_id": "receipt-fixture-1"},
            )
        elif status == "ambiguous":
            base.update(
                reconciliation_key="reconcile-fixture-1",
                intent_digest=canonical_digest(intent),
            )
        elif status == "rate_limited":
            base["rate_limit"] = {
                "scope": "fixture-repository",
                "remaining": 0,
                "reset_at": "2030-01-01T00:01:00Z",
                "retry_after_seconds": 60,
            }
        else:
            base["reason"] = "fixture permanent rejection"
        return {**base, "acknowledgement_digest": canonical_digest(base)}

    def mutate(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.mutation_calls += 1
        return self._ack(intent, self.mutation_status)

    def reconcile(
        self, reconciliation_key: str, intent: dict[str, Any]
    ) -> dict[str, Any]:
        self.reconcile_calls += 1
        if not (
            reconciliation_key == "reconcile-fixture-1"
            or reconciliation_key.startswith("intent-")
        ):
            raise AdapterError("fixture reconciliation key mismatch")
        return self._ack(intent, "completed")
