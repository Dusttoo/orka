#!/usr/bin/env python3
"""Provider-neutral capability negotiation for disposable phase workers.

This module is deliberately free of provider transports.  Capability
negotiation happens before a controller reservation or model submission, so a
mixed-version adapter is a route compatibility failure rather than a provider
outage or a charged attempt.
"""

from __future__ import annotations

from typing import Any

from phase_worker_contract import ContractError, validate_envelope


class AdapterProtocolError(RuntimeError):
    """A route cannot consume the supervisor's phase-worker contract."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


PROFILE_CAPABILITY = {
    "codex-desktop": "desktop-subscription",
    "claude-desktop": "desktop-subscription",
    "api": "provider-receipts",
}

MANDATORY_CAPABILITIES = (
    "attempt-fencing",
    "cancellation-ack-or-fence",
    "fresh-context-per-dispatch",
    "immutable-job-identity",
    "structured-progress",
    "structured-terminal-result",
)


def capability_offer(
    profile: str,
    *,
    adapter_version: str,
    protocol_version: int = 1,
    supported_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Return an adapter-owned offer without contacting a model provider."""

    if profile not in PROFILE_CAPABILITY:
        raise AdapterProtocolError("unsupported_adapter", profile)
    if not isinstance(adapter_version, str) or not adapter_version.strip():
        raise AdapterProtocolError(
            "invalid_adapter_version", "adapter version must be nonempty"
        )
    capabilities = supported_capabilities or [
        *MANDATORY_CAPABILITIES,
        PROFILE_CAPABILITY[profile],
    ]
    return {
        "kind": "capability_offer",
        "protocol_version": protocol_version,
        "adapter": profile,
        "adapter_version": adapter_version.strip(),
        "supported_capabilities": list(capabilities),
        "fresh_context_per_dispatch": True,
    }


def negotiate(contract: dict[str, Any], offer: dict[str, Any]) -> dict[str, Any]:
    """Validate one offer and return a sanitized compatibility receipt."""

    try:
        validate_envelope(contract, offer)
    except ContractError as exc:
        message = str(exc)
        code = (
            "unsupported_protocol"
            if "protocol_version" in message
            else "missing_capability"
            if "capabilit" in message
            else "malformed_offer"
        )
        raise AdapterProtocolError(code, message) from exc
    return {
        "compatible": True,
        "protocol_version": offer["protocol_version"],
        "adapter": offer["adapter"],
        "adapter_version": offer["adapter_version"],
        "supported_capabilities": sorted(set(offer["supported_capabilities"])),
    }


def classify_route_failure(error: BaseException) -> dict[str, Any]:
    """Separate local protocol drift from a genuine provider incident."""

    if isinstance(error, AdapterProtocolError):
        return {
            "class": "protocol_incompatibility",
            "scope": "route",
            "provider_outage": False,
            "code": error.code,
            "detail": error.detail[:2000],
        }
    return {
        "class": "provider_outage",
        "scope": "provider",
        "provider_outage": True,
        "code": "provider_transport",
        "detail": str(error)[:2000],
    }
