#!/usr/bin/env python3
"""Validation helpers for the external-service adapter v1 contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    """The contract definition is incomplete or weakens a required boundary."""


def load_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read external-service contract: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("external-service contract must be an object")
    return value


def _strings(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ContractError(f"{label} must be a unique string array")
    return value


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if contract.get("schema_version") != 1:
        raise ContractError("schema_version must be 1")
    if contract.get("contract_id") != "orka.external-service-adapter":
        raise ContractError("contract_id must be orka.external-service-adapter")
    if contract.get("protocol_version") != 1:
        raise ContractError("protocol_version must be 1")

    compatibility = contract.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ContractError("compatibility policy is required")
    if compatibility.get("negotiation") != "exact-major-additive-capabilities":
        raise ContractError(
            "protocol negotiation must preserve exact major compatibility"
        )
    if compatibility.get("supported_protocol_versions") != [1]:
        raise ContractError("v1 must advertise only protocol version 1")
    if (
        compatibility.get("unknown_required_capabilities")
        != "reject-before-reservation"
    ):
        raise ContractError(
            "missing required capabilities must fail before reservation"
        )
    if compatibility.get("incompatible_protocol") != "reject-before-reservation":
        raise ContractError("incompatible protocols must fail before reservation")

    capabilities = contract.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        raise ContractError("capabilities are required")
    expected_kinds = {"read", "collection-read", "mutation"}
    for name, capability in capabilities.items():
        if not isinstance(name, str) or not name or not isinstance(capability, dict):
            raise ContractError("capability definitions must be named objects")
        if (
            capability.get("version") != 1
            or capability.get("kind") not in expected_kinds
        ):
            raise ContractError(f"capability {name} has an invalid version or kind")
        _strings(capability.get("object_types"), f"capabilities.{name}.object_types")
        if capability["kind"] == "collection-read":
            _strings(capability.get("item_types"), f"capabilities.{name}.item_types")
        elif "item_types" in capability:
            raise ContractError(f"non-collection capability {name} defines item_types")

    required_capabilities = {
        "issues.inventory.read",
        "issues.dependencies.read",
        "issues.status.read",
        "issues.status.transition",
        "scm.pull-request.read",
        "scm.ci.read",
        "scm.approval.read",
        "scm.comment.create",
        "scm.merge-receipt.read",
    }
    if not required_capabilities <= set(capabilities):
        raise ContractError("contract omits a mandatory engineering-service capability")

    binding = contract.get("binding")
    expected_binding = {
        "repository_id",
        "adapter_id",
        "adapter_version",
        "protocol_version",
        "operation",
        "operation_key",
        "request_id",
        "request_digest",
        "external_object",
    }
    if (
        not isinstance(binding, dict)
        or set(_strings(binding.get("required"), "binding.required"))
        != expected_binding
    ):
        raise ContractError("request binding is incomplete")
    if set(
        _strings(
            binding.get("external_object_required"), "binding.external_object_required"
        )
    ) != {"type", "id"}:
        raise ContractError("external object identity is incomplete")
    if binding.get("response_must_echo_exact_request_binding") is not True:
        raise ContractError("responses must echo the exact request binding")
    if binding.get("repository_renames_preserve_immutable_id") is not True:
        raise ContractError("repository identity must survive provider renames")

    evidence = contract.get("evidence")
    if not isinstance(evidence, dict):
        raise ContractError("evidence policy is required")
    if (
        evidence.get("normalized_only") is not True
        or evidence.get("vendor_payload_allowed") is not False
    ):
        raise ContractError("vendor payloads cannot enter supervisor state")
    if set(_strings(evidence.get("required"), "evidence.required")) != {
        "binding",
        "observed_at",
        "evidence_digest",
        "data",
    }:
        raise ContractError("normalized evidence fields are incomplete")
    if evidence.get("unknown_fields") != "reject":
        raise ContractError("unknown evidence fields must fail closed")
    if evidence.get("data_must_be_object") is not True:
        raise ContractError("normalized evidence data must be structured")

    normalization = contract.get("normalization")
    if not isinstance(normalization, dict) or normalization.get("schema_version") != 1:
        raise ContractError("versioned normalized schemas are required")
    if normalization.get("unknown_fields") != "reject-recursively":
        raise ContractError("normalized schemas must reject unknown fields recursively")
    schemas = normalization.get("capabilities")
    if not isinstance(schemas, dict) or set(schemas) != set(capabilities):
        raise ContractError("every capability requires a normalized schema")
    for name, schema in schemas.items():
        if not isinstance(schema, dict) or set(schema) != {
            "request",
            "evidence",
            "item",
            "receipt",
        }:
            raise ContractError(f"capability {name} normalized schema is incomplete")
        for field_kind, fields in schema.items():
            if not isinstance(fields, dict) or set(fields) != {
                "required",
                "min_properties",
                "properties",
            }:
                raise ContractError(
                    f"normalization.capabilities.{name}.{field_kind} is incomplete"
                )
            required = _strings(
                fields["required"],
                f"normalization.capabilities.{name}.{field_kind}.required",
                allow_empty=True,
            )
            if (
                not isinstance(fields["min_properties"], int)
                or isinstance(fields["min_properties"], bool)
                or fields["min_properties"] < 0
            ):
                raise ContractError(
                    f"normalization.capabilities.{name}.{field_kind}.min_properties is invalid"
                )
            properties = fields["properties"]
            if not isinstance(properties, dict) or any(
                not isinstance(field, str) or not field or not isinstance(rule, dict)
                for field, rule in properties.items()
            ):
                raise ContractError(
                    f"normalization.capabilities.{name}.{field_kind}.properties is invalid"
                )
            if not set(required) <= set(properties) or fields["min_properties"] > len(
                properties
            ):
                raise ContractError(
                    f"normalization.capabilities.{name}.{field_kind} has impossible requirements"
                )
            for field, rule in properties.items():
                allowed = {
                    "type",
                    "min_length",
                    "minimum",
                    "enum",
                    "item_type",
                    "min_items",
                }
                if set(rule) - allowed or rule.get("type") not in {
                    "string",
                    "integer",
                    "boolean",
                    "array",
                }:
                    raise ContractError(
                        f"normalization.capabilities.{name}.{field_kind}.{field} has invalid type rule"
                    )
                field_type = rule["type"]
                if field_type == "array":
                    if rule.get("item_type") not in {"string", "integer", "boolean"}:
                        raise ContractError(
                            f"normalization.capabilities.{name}.{field_kind}.{field} has invalid array item type"
                        )
                    if (
                        not isinstance(rule.get("min_items", 0), int)
                        or isinstance(rule.get("min_items", 0), bool)
                        or rule.get("min_items", 0) < 0
                    ):
                        raise ContractError(
                            f"normalization.capabilities.{name}.{field_kind}.{field} has invalid min_items"
                        )
                elif "item_type" in rule or "min_items" in rule:
                    raise ContractError(
                        f"normalization.capabilities.{name}.{field_kind}.{field} misuses array constraints"
                    )
                if "min_length" in rule and (
                    field_type != "string"
                    or not isinstance(rule["min_length"], int)
                    or isinstance(rule["min_length"], bool)
                    or rule["min_length"] < 0
                ):
                    raise ContractError(
                        f"normalization.capabilities.{name}.{field_kind}.{field} has invalid min_length"
                    )
                if "minimum" in rule and (
                    field_type != "integer"
                    or not isinstance(rule["minimum"], int)
                    or isinstance(rule["minimum"], bool)
                ):
                    raise ContractError(
                        f"normalization.capabilities.{name}.{field_kind}.{field} has invalid minimum"
                    )
                if "enum" in rule and (
                    not isinstance(rule["enum"], list)
                    or not rule["enum"]
                    or any(not isinstance(value, str) for value in rule["enum"])
                ):
                    raise ContractError(
                        f"normalization.capabilities.{name}.{field_kind}.{field} has invalid enum"
                    )
        kind = capabilities[name]["kind"]
        if kind == "collection-read" and schema["evidence"]["properties"]:
            raise ContractError(
                f"collection capability {name} cannot define evidence fields"
            )
        if kind != "collection-read" and schema["item"]["properties"]:
            raise ContractError(
                f"non-collection capability {name} cannot define item fields"
            )
        if kind == "mutation" and not schema["receipt"]["properties"]:
            raise ContractError(f"mutation capability {name} requires a receipt schema")
        if kind != "mutation" and schema["receipt"]["properties"]:
            raise ContractError(
                f"non-mutation capability {name} cannot define receipt fields"
            )

    pagination = contract.get("pagination")
    if not isinstance(pagination, dict):
        raise ContractError("pagination proof policy is required")
    if pagination.get("required_for") != ["collection-read"]:
        raise ContractError("all collection reads require pagination proof")
    if set(_strings(pagination.get("page_required"), "pagination.page_required")) != {
        "page_index",
        "cursor",
        "request_digest",
        "items",
        "next_cursor",
        "complete",
    }:
        raise ContractError("page proof is incomplete")
    if set(
        _strings(
            pagination.get("final_proof_required"), "pagination.final_proof_required"
        )
    ) != {
        "page_count",
        "item_count",
        "item_identities_digest",
        "completion_basis",
        "provider_total",
    }:
        raise ContractError("final collection proof is incomplete")
    if set(
        _strings(pagination.get("completion_bases"), "pagination.completion_bases")
    ) != {"provider-total", "terminal-cursor"}:
        raise ContractError("collection completion bases are incomplete")
    if set(_strings(pagination.get("item_required"), "pagination.item_required")) != {
        "object_identity",
        "data",
    } or set(
        _strings(
            pagination.get("item_identity_required"),
            "pagination.item_identity_required",
        )
    ) != {
        "type",
        "id",
    }:
        raise ContractError("paginated item identity is incomplete")
    if not isinstance(pagination.get("max_pages"), int) or pagination["max_pages"] < 1:
        raise ContractError("pagination requires a positive maximum page count")
    if (
        pagination.get("duplicate_item_identity") != "reject"
        or pagination.get("repeated_cursor") != "reject"
    ):
        raise ContractError("pagination must reject duplicate identities and cursors")
    if pagination.get("incomplete_terminal_page") != "reject":
        raise ContractError("incomplete terminal pages must fail closed")

    mutations = contract.get("mutations")
    if not isinstance(mutations, dict):
        raise ContractError("mutation lifecycle is required")
    if set(_strings(mutations.get("statuses"), "mutations.statuses")) != {
        "completed",
        "rejected",
        "rate_limited",
        "ambiguous",
    }:
        raise ContractError("mutation statuses are incomplete")
    if set(_strings(mutations.get("intent_required"), "mutations.intent_required")) != {
        "binding",
        "idempotency_key",
        "input_digest",
        "input",
    } or set(
        _strings(
            mutations.get("acknowledgement_required"),
            "mutations.acknowledgement_required",
        )
    ) != {
        "binding",
        "status",
        "acknowledgement_digest",
    }:
        raise ContractError("mutation intent or acknowledgement binding is incomplete")
    if set(
        _strings(mutations.get("completed_requires"), "mutations.completed_requires")
    ) != {"external_object", "receipt"} or set(
        _strings(mutations.get("ambiguous_requires"), "mutations.ambiguous_requires")
    ) != {
        "reconciliation_key",
        "intent_digest",
    }:
        raise ContractError("mutation completion or ambiguity proof is incomplete")
    if mutations.get("blind_retry_after_ambiguous") is not False:
        raise ContractError("ambiguous mutations cannot be retried blindly")
    if mutations.get("reconciliation_operation") != "reconcile":
        raise ContractError("ambiguous mutations require reconciliation")
    if mutations.get("reconciliation_must_bind_original_intent") is not True:
        raise ContractError("reconciliation must bind the original intent")
    if mutations.get("intent_store_requires_atomic_claim") is not True:
        raise ContractError("mutation intent store must provide an atomic claim")
    if mutations.get("pre_ack_reconciliation_key") != "durable-host-generated":
        raise ContractError("claimed mutations require a durable reconciliation key")
    if mutations.get("reconciliation_key_collision") != "reject":
        raise ContractError("reconciliation key collisions must fail closed")
    if mutations.get("file_store_commit") != "file-and-parent-directory-fsync":
        raise ContractError("file intent stores must durably commit directory entries")
    if mutations.get("rate_limited_retry") != "same-exact-intent-after-retry-after":
        raise ContractError("rate-limited retries must reuse the exact intent")
    if mutations.get("uncertain_submission") != "reconcile-only":
        raise ContractError("uncertain mutation submission must require reconciliation")

    redaction = contract.get("redaction")
    forbidden = set(
        _strings((redaction or {}).get("forbidden_keys"), "redaction.forbidden_keys")
    )
    if (
        not {"authorization", "credentials", "raw_payload", "secret", "token"}
        <= forbidden
    ):
        raise ContractError("redaction policy omits credential or raw-payload keys")
    if (
        redaction.get("case_insensitive") is not True
        or redaction.get("applies_recursively") is not True
    ):
        raise ContractError("redaction must be recursive and case-insensitive")
    if redaction.get("credentials_may_cross_adapter_boundary") is not False:
        raise ContractError("credentials cannot cross the adapter boundary")

    retries = contract.get("retry_and_rate_limits")
    if not isinstance(retries, dict):
        raise ContractError("retry classification policy is required")
    if (
        set(_strings(retries.get("classifications"), "retry classifications"))
        != {"success", "permanent", "transient", "rate_limited", "ambiguous"}
        or retries.get("ambiguous_is_retryable") is not False
    ):
        raise ContractError("retry classifications must preserve ambiguity")
    if set(_strings(retries.get("rate_limit_required"), "rate_limit_required")) != {
        "scope",
        "remaining",
        "reset_at",
        "retry_after_seconds",
    }:
        raise ContractError("rate-limit evidence is incomplete")
    if retries.get("rate_limited_retry_requires_reset_or_retry_after") is not True:
        raise ContractError("rate-limited retries require provider timing evidence")
    if (
        retries.get("unknown_error_before_claim_is") != "permanent"
        or retries.get("unknown_error_after_claim_is") != "reconcile-only"
    ):
        raise ContractError("unknown adapter errors must fail closed by claim phase")

    unsupported = contract.get("unsupported")
    if not isinstance(unsupported, dict) or unsupported.get("status") != "unsupported":
        raise ContractError("unsupported capability response is required")
    if (
        set(_strings(unsupported.get("required"), "unsupported.required"))
        != {"capability", "requested_version", "supported_versions"}
        or unsupported.get("must_not_mutate") is not True
    ):
        raise ContractError("unsupported capabilities must fail without mutation")

    authority = contract.get("authority_boundary")
    forbidden_authority = set(
        _strings((authority or {}).get("forbidden"), "authority_boundary.forbidden")
    )
    if (
        not {
            "schedule_work",
            "reserve_budget",
            "change_controller_state",
            "authorize_merge",
            "retry_ambiguous_mutation",
        }
        <= forbidden_authority
    ):
        raise ContractError("adapter authority boundary is too broad")

    mapping = contract.get("compatibility_map")
    if not isinstance(mapping, dict) or set(mapping) != {"jira", "github"}:
        raise ContractError("compatibility map must cover Jira and GitHub")
    for provider, entry in mapping.items():
        offered = set(
            _strings(
                entry.get("capabilities"), f"compatibility_map.{provider}.capabilities"
            )
        )
        pending = set(
            _strings(
                entry.get("pending_integration"),
                f"compatibility_map.{provider}.pending_integration",
                allow_empty=True,
            )
        )
        if not (offered | pending) <= set(capabilities):
            raise ContractError(f"{provider} maps an undefined capability")

    tests = contract.get("test_adapters")
    if tests != {
        "explicit_conformance_mode_required": True,
        "production_selection_allowed": False,
        "credentials_allowed": False,
    }:
        raise ContractError("conformance adapters must remain offline and test-only")

    return {
        "contract_id": contract["contract_id"],
        "protocol_version": contract["protocol_version"],
        "capability_count": len(capabilities),
        "provider_count": len(mapping),
    }
