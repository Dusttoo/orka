#!/usr/bin/env python3
"""Pure recovery eligibility evaluation for Orka's durable supervisor."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class RecoveryEvidenceError(RuntimeError):
    pass


SCHEMA = "orka.recovery-eligibility/v1"
VERDICTS = ("eligible", "waiting", "operator_action")
PROFILES = ("isolated", "cooperative")
WORK_KINDS = ("attempt", "preserved_pr")
PRESERVED_FIELDS = (
    "attempts",
    "spend",
    "progress",
    "branch",
    "worktree",
    "pr",
    "review_findings",
    "review_generation",
)
REASONS = {
    "execution_live": ("waiting", "the prior execution unit is still live"),
    "execution_unknown": (
        "operator_action",
        "the prior execution unit cannot be proven live or absent",
    ),
    "execution_identity_mismatch": (
        "operator_action",
        "execution evidence is not bound to the exact prior invocation",
    ),
    "terminal_receipt_missing": (
        "operator_action",
        "the prior execution has no authenticated terminal receipt",
    ),
    "descendants_live": (
        "waiting",
        "a descendant of the prior execution remains live",
    ),
    "descendants_unknown": (
        "operator_action",
        "descendant absence cannot be proven",
    ),
    "cooperative_not_authorized": (
        "operator_action",
        "cooperative recovery was not authorized at launch and recovery",
    ),
    "cooperative_cleanup_incomplete": (
        "operator_action",
        "cooperative gateway and process-group cleanup is incomplete",
    ),
    "provider_pending": (
        "waiting",
        "provider work or a usage reservation has not settled",
    ),
    "provider_ambiguous": (
        "operator_action",
        "provider acknowledgement or financial settlement is ambiguous",
    ),
    "binding_incomplete": (
        "operator_action",
        "repository, ticket, attempt, and work identity are not fully bound",
    ),
    "worktree_active": ("waiting", "the preserved worktree is still active"),
    "worktree_dirty": (
        "operator_action",
        "the preserved worktree contains uncommitted changes",
    ),
    "worktree_unknown": (
        "operator_action",
        "worktree ownership or quiescence cannot be proven",
    ),
    "worktree_missing": (
        "operator_action",
        "preserved PR recovery requires a clean bound worktree",
    ),
    "revision_mismatch": (
        "operator_action",
        "branch, PR, head, or tree evidence no longer agrees",
    ),
    "history_incomplete": (
        "operator_action",
        "recovery would not preserve every required history field",
    ),
}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_bool(value: dict[str, Any], key: str, section: str) -> bool:
    observed = value.get(key)
    if not isinstance(observed, bool):
        raise RecoveryEvidenceError(f"{section}.{key} must be boolean")
    return observed


def normalize_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise RecoveryEvidenceError(f"recovery evidence must use {SCHEMA}")
    profile = value.get("profile")
    if profile not in PROFILES:
        raise RecoveryEvidenceError("recovery profile is unsupported")
    execution = value.get("execution")
    provider = value.get("provider")
    work = value.get("work")
    history = value.get("history")
    for name, section in (
        ("execution", execution),
        ("provider", provider),
        ("work", work),
        ("history", history),
    ):
        if not isinstance(section, dict):
            raise RecoveryEvidenceError(f"{name} evidence must be an object")

    status = execution.get("status")
    descendants = execution.get("descendants")
    provider_state = provider.get("state")
    worktree = work.get("worktree")
    work_kind = work.get("kind")
    if status not in {"live", "absent", "unknown"}:
        raise RecoveryEvidenceError("execution.status is invalid")
    if descendants not in {"live", "absent", "unknown"}:
        raise RecoveryEvidenceError("execution.descendants is invalid")
    if provider_state not in {"settled", "pending", "ambiguous"}:
        raise RecoveryEvidenceError("provider.state is invalid")
    if worktree not in {"clean", "dirty", "active", "unknown", "not_applicable"}:
        raise RecoveryEvidenceError("work.worktree is invalid")
    if work_kind not in WORK_KINDS:
        raise RecoveryEvidenceError("work.kind is invalid")
    preserved = history.get("preserved_fields")
    if not isinstance(preserved, list) or any(
        not isinstance(field, str) for field in preserved
    ):
        raise RecoveryEvidenceError("history.preserved_fields must be strings")

    return {
        "schema": SCHEMA,
        "profile": profile,
        "execution": {
            "status": status,
            "identity_bound": _require_bool(execution, "identity_bound", "execution"),
            "terminal_receipt": _require_bool(
                execution, "terminal_receipt", "execution"
            ),
            "descendants": descendants,
            "cooperative_authorized": _require_bool(
                execution, "cooperative_authorized", "execution"
            ),
            "cleanup_complete": _require_bool(
                execution, "cleanup_complete", "execution"
            ),
        },
        "provider": {"state": provider_state},
        "work": {
            "kind": work_kind,
            "binding_complete": _require_bool(work, "binding_complete", "work"),
            "worktree": worktree,
            "revision_match": _require_bool(work, "revision_match", "work"),
        },
        "history": {"preserved_fields": sorted(set(preserved))},
    }


def evaluate_recovery(value: Any) -> dict[str, Any]:
    """Return a stable verdict without reading or mutating external state."""

    evidence = normalize_evidence(value)
    execution = evidence["execution"]
    provider = evidence["provider"]
    work = evidence["work"]
    history = evidence["history"]
    reason_codes: list[str] = []

    if execution["status"] == "live":
        reason_codes.append("execution_live")
    elif execution["status"] == "unknown":
        reason_codes.append("execution_unknown")
    if not execution["identity_bound"]:
        reason_codes.append("execution_identity_mismatch")
    if not execution["terminal_receipt"]:
        reason_codes.append("terminal_receipt_missing")
    if execution["descendants"] == "live":
        reason_codes.append("descendants_live")
    elif execution["descendants"] == "unknown":
        reason_codes.append("descendants_unknown")
    if evidence["profile"] == "cooperative":
        if not execution["cooperative_authorized"]:
            reason_codes.append("cooperative_not_authorized")
        if not execution["cleanup_complete"]:
            reason_codes.append("cooperative_cleanup_incomplete")

    if provider["state"] == "pending":
        reason_codes.append("provider_pending")
    elif provider["state"] == "ambiguous":
        reason_codes.append("provider_ambiguous")
    if not work["binding_complete"]:
        reason_codes.append("binding_incomplete")
    if work["worktree"] == "active":
        reason_codes.append("worktree_active")
    elif work["worktree"] == "dirty":
        reason_codes.append("worktree_dirty")
    elif work["worktree"] == "unknown":
        reason_codes.append("worktree_unknown")
    elif work["kind"] == "preserved_pr" and work["worktree"] != "clean":
        reason_codes.append("worktree_missing")
    if not work["revision_match"]:
        reason_codes.append("revision_mismatch")
    if not set(PRESERVED_FIELDS).issubset(history["preserved_fields"]):
        reason_codes.append("history_incomplete")

    verdict = "eligible"
    if any(REASONS[code][0] == "operator_action" for code in reason_codes):
        verdict = "operator_action"
    elif reason_codes:
        verdict = "waiting"
    result = {
        "schema": SCHEMA,
        "profile": evidence["profile"],
        "verdict": verdict,
        "eligible": verdict == "eligible",
        "reason_codes": reason_codes,
        "reasons": [REASONS[code][1] for code in reason_codes],
        "preservation_invariants": list(PRESERVED_FIELDS),
        "evidence_digest": canonical_digest(evidence),
    }
    return result
