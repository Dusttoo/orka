#!/usr/bin/env python3
"""Deterministic resource admission for the durable sprint supervisor."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable


class AdmissionError(RuntimeError):
    pass


RESOURCE_KINDS = {
    "repository",
    "ticket",
    "pr",
    "worktree",
    "migration",
    "provider_route",
    "visual_qa",
    "heavy_process",
}
CLAIM_SCHEMA = "orka.resource-claims/v1"
ACTIVE_STATES = {"reserved", "running", "launch_uncertain"}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}\Z")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_claim(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "key",
        "units",
        "capacity",
        "source",
    }:
        raise AdmissionError(
            "resource claim must contain kind, key, units, capacity, and source"
        )
    kind = value.get("kind")
    key = value.get("key")
    source = value.get("source")
    units = value.get("units")
    capacity = value.get("capacity")
    if kind not in RESOURCE_KINDS:
        raise AdmissionError(f"unsupported resource kind: {kind}")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise AdmissionError("resource claim key is not canonical")
    if not isinstance(source, str) or not KEY.fullmatch(source):
        raise AdmissionError("resource claim source is not canonical")
    if (
        isinstance(units, bool)
        or not isinstance(units, int)
        or units < 1
        or isinstance(capacity, bool)
        or not isinstance(capacity, int)
        or capacity < 1
        or units > capacity
    ):
        raise AdmissionError("resource claim units and capacity must be positive integers")
    return {
        "kind": kind,
        "key": key,
        "units": units,
        "capacity": capacity,
        "source": source,
    }


def normalize_claims(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise AdmissionError("resource claims must be a nonempty array")
    claims = [normalize_claim(value) for value in values]
    identities = [(claim["kind"], claim["key"]) for claim in claims]
    if len(identities) != len(set(identities)):
        raise AdmissionError("one job cannot claim the same resource more than once")
    return sorted(claims, key=lambda claim: (claim["kind"], claim["key"]))


def claim_set_digest(claims: list[dict[str, Any]]) -> str:
    return canonical_digest(normalize_claims(claims))


def repository_key(repository: Path) -> str:
    return canonical_digest(str(repository.resolve()))[:32]


def automatic_claims(
    repository: Path,
    ticket: str,
    *,
    concurrency: int,
    heavy_capacity: int,
    route_identity: str,
    additional: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if concurrency < 1 or heavy_capacity < 1:
        raise AdmissionError("resource capacities must be positive")
    values: list[dict[str, Any]] = [
        {
            "kind": "repository",
            "key": repository_key(repository),
            "units": 1,
            "capacity": concurrency,
            "source": "supervisor",
        },
        {
            "kind": "ticket",
            "key": ticket,
            "units": 1,
            "capacity": 1,
            "source": "controller",
        },
        {
            "kind": "provider_route",
            "key": route_identity,
            "units": 1,
            "capacity": concurrency,
            "source": "routing-config",
        },
        {
            "kind": "heavy_process",
            "key": "host",
            "units": 1,
            "capacity": heavy_capacity,
            "source": "repository-config",
        },
    ]
    values.extend(additional or [])
    return normalize_claims(values)


def _held_usage(jobs: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    held: dict[tuple[str, str], dict[str, Any]] = {}
    for run_ref, job in sorted(jobs.items()):
        if job.get("state") not in ACTIVE_STATES:
            continue
        if job.get("resource_claim_schema") != CLAIM_SCHEMA:
            raise AdmissionError(f"job {run_ref} has an unsupported resource claim schema")
        claims = normalize_claims(job.get("resource_claims"))
        observed_digest = claim_set_digest(claims)
        if job.get("resource_claim_digest") != observed_digest:
            raise AdmissionError(f"job {run_ref} resource claim digest changed")
        for claim in claims:
            identity = (claim["kind"], claim["key"])
            current = held.setdefault(
                identity,
                {"capacity": claim["capacity"], "units": 0, "holders": []},
            )
            if current["capacity"] != claim["capacity"]:
                raise AdmissionError(
                    f"resource {claim['kind']}:{claim['key']} has conflicting capacities"
                )
            current["units"] += claim["units"]
            current["holders"].append(run_ref)
            if current["units"] > current["capacity"]:
                raise AdmissionError(
                    f"resource {claim['kind']}:{claim['key']} exceeds capacity"
                )
    return held


def conflicts(
    claims: list[dict[str, Any]], jobs: dict[str, Any]
) -> list[dict[str, Any]]:
    normalized = normalize_claims(claims)
    held = _held_usage(jobs)
    blocked: list[dict[str, Any]] = []
    for claim in normalized:
        identity = (claim["kind"], claim["key"])
        current = held.get(identity)
        if current and current["capacity"] != claim["capacity"]:
            raise AdmissionError(
                f"resource {claim['kind']}:{claim['key']} has conflicting capacities"
            )
        used = int((current or {}).get("units") or 0)
        if used + claim["units"] > claim["capacity"]:
            blocked.append(
                {
                    "kind": claim["kind"],
                    "key": claim["key"],
                    "requested_units": claim["units"],
                    "held_units": used,
                    "capacity": claim["capacity"],
                    "holders": list((current or {}).get("holders") or []),
                }
            )
    return blocked


def migrate_legacy_active_jobs(
    jobs: dict[str, Any], claim_factory: Callable[[str], list[dict[str, Any]]]
) -> list[str]:
    """Add deterministic default claims to pre-resource-admission active jobs."""

    migrated: list[str] = []
    for run_ref, job in sorted(jobs.items()):
        if job.get("state") not in ACTIVE_STATES:
            continue
        if job.get("resource_claims") is None:
            claims = normalize_claims(claim_factory(str(job.get("ticket") or "")))
            job["resource_claims"] = claims
            job["resource_claim_schema"] = CLAIM_SCHEMA
            job["resource_claim_digest"] = claim_set_digest(claims)
            job.setdefault("history", []).append(
                {
                    "event": "resource_claims_migrated",
                    "claim_digest": job["resource_claim_digest"],
                }
            )
            migrated.append(run_ref)
    _held_usage(jobs)
    return migrated


def validate_persisted_jobs(jobs: Any) -> None:
    """Validate every modern durable claim set while allowing 1.x migration."""

    if not isinstance(jobs, dict):
        raise AdmissionError("durable supervisor jobs must be an object")
    claimed: dict[str, Any] = {}
    fields = {
        "resource_claim_schema",
        "resource_claims",
        "resource_claim_digest",
    }
    for run_ref, job in sorted(jobs.items()):
        if not isinstance(job, dict):
            raise AdmissionError(f"job {run_ref} is not an object")
        if job.get("state") not in ACTIVE_STATES:
            continue
        present = {name for name in fields if job.get(name) is not None}
        if present and present != fields:
            raise AdmissionError(f"job {run_ref} has an incomplete resource claim record")
        if present:
            claimed[run_ref] = job
    _held_usage(claimed)


def release_claims(job: dict[str, Any], *, observed_at: float) -> dict[str, Any]:
    if job.get("resource_claim_schema") != CLAIM_SCHEMA:
        raise AdmissionError("job has an unsupported resource claim schema")
    claims = normalize_claims(job.get("resource_claims"))
    digest = claim_set_digest(claims)
    if job.get("resource_claim_digest") != digest:
        raise AdmissionError("job resource claim digest changed before release")
    existing = job.get("resource_release")
    if existing:
        if existing.get("claim_digest") != digest:
            raise AdmissionError("resource release receipt does not match the claim set")
        return existing
    receipt = {"claim_digest": digest, "released_at": observed_at}
    job["resource_release"] = receipt
    job.setdefault("history", []).append(
        {"event": "resource_claims_released", **receipt}
    )
    return receipt
