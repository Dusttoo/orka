#!/usr/bin/env python3
"""Durable cross-gate, cross-round review ledger for one PR.

The orchestrator is a lossy relay with a context window that compacts, so a
ledger it "maintains" in conversation is forgotten on long tickets -- and
failed repairs lose their history. This script owns that
state on disk instead: normalized component keys, strike counts across every
gate and round, the blocking/advisory split, the scope-freeze mode for each
round, and the hard round cap that ends an unconverged loop at a human.

Findings are keyed by `<path>:<symbol>`, normalized here so that two reviewers
naming the same defect differently still land on one key and accumulate strikes.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import context_pipeline
from review_permit import (
    ReviewPermitError,
    complete as complete_review_permit,
    consume_completion,
    subject_ledger_candidates,
)
from operator_authority import (
    AuthorityError,
    activate_review_repair,
    review_repair_grant,
    restart_grant as authorized_restart_grant,
)

from runtime_state import (
    RuntimeStateError,
    canonical_config_path,
    migrate_legacy_runtime_dir,
    shared_repository_root,
    working_repository_root,
)
from version_policy import VersionPolicyError, assert_minimum_version


SCHEMA_VERSION = 1
DEFAULT_MAX_REPAIR_CYCLES = 2
DEFAULT_MAX_DESIGN_ROUNDS = 5
DEFAULT_LEDGER_DIR = ".orchestration/.review-ledger"
SEVERITIES = ("blocking", "advisory")
ROLE_GATES = {
    "code-reviewer": "code-review",
    "security-reviewer": "security-review",
}

# Round 1 sweeps the whole diff with full authority to block. Later rounds still
# sweep the whole diff, but only ledger findings and regressions in the delta may
# block -- that is what makes the blocking set shrink monotonically.
FULL = "full-authority"
FROZEN = "scope-frozen"

ACTION_REVIEW = "review"
ACTION_REDESIGN = "redesign"
ACTION_ESCALATE = "escalate-human"
ACTION_CLEAR = "gates-clear"


class LedgerError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def project_root() -> Path:
    return working_repository_root(Path.cwd())


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def config_scalar(path: Path, key: str, default: str) -> str:
    if not path.exists():
        return default
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*?)\s*(?:#.*)?$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match and match.group(1):
            return unquote(match.group(1))
    return default


# --- component keys -----------------------------------------------------------

_COMPONENT_WRAPPER = re.compile(r"^\[?\s*component\s*:\s*(.*?)\s*\]?$", re.IGNORECASE)


def normalize_key(raw: str) -> str:
    """Reduce a reviewer-supplied component key to a stable `<path>:<symbol>`.

    Reviewers run fresh every round with no memory of prior keys, so free-text
    subsystem names drift ("auth/sessionStore" then "session-refresh") and the
    same defect never accumulates a second strike. Anchoring the key to the file
    path plus the enclosing symbol makes it mechanically derivable from the diff
    instead of invented, and line numbers -- which drift on every rebase -- are
    discarded rather than treated as identity.
    """
    value = raw.strip()
    if not value:
        raise LedgerError("component key is empty")
    match = _COMPONENT_WRAPPER.match(value)
    if match:
        value = match.group(1).strip()
    value = value.strip("[]").strip()
    if not value:
        raise LedgerError(f"component key is empty after normalization: {raw!r}")

    parts = [segment.strip() for segment in value.split(":") if segment.strip()]
    if not parts:
        raise LedgerError(f"component key is empty after normalization: {raw!r}")
    # A trailing all-digits segment is a line number, not identity.
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()

    path = parts[0].lower().lstrip("./")
    path = re.sub(r"/{2,}", "/", path).strip("/")
    symbol = ""
    if len(parts) > 1:
        symbol = parts[-1].lower()
        symbol = symbol.replace("()", "")
        symbol = re.sub(r"[^a-z0-9_.\-/]+", "-", symbol).strip("-")
    if not path:
        raise LedgerError(f"component key has no path segment: {raw!r}")
    return f"{path}:{symbol}" if symbol else path


def resolve_alias(state: dict[str, Any], key: str) -> str:
    """Resolve a persisted component alias to its canonical stable key."""
    aliases = state.get("aliases", {})
    seen: set[str] = set()
    current = key
    while current in aliases:
        if current in seen:
            raise LedgerError(f"component alias cycle includes {current}")
        seen.add(current)
        current = normalize_key(str(aliases[current]))
    return current


# --- state --------------------------------------------------------------------


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _timestamp(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{label} has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"{label} timestamp must include a timezone")
    return parsed


def _migrate_legacy_repair_generations(value: dict[str, Any]) -> None:
    """Bind pre-generation review rows to their repair chronology.

    Older ledgers used `round` as a call sequence.  Treating that sequence as a
    modern logical generation can make an old round collide with a newly
    repaired head.  A repair artifact is durably recorded before reviewers may
    inspect that head, so its timestamp is an unambiguous generation boundary.
    """
    legacy_rounds = [
        entry for entry in value.get("rounds", []) if "generation" not in entry
    ]
    explicit_repairs = [
        attempt
        for attempt in value.get("repair_attempts", [])
        if not attempt.get("legacy")
    ]
    if not legacy_rounds or not explicit_repairs:
        return

    repair_times = [
        _timestamp(attempt.get("recorded_at"), label="repair attempt")
        for attempt in explicit_repairs
    ]
    if repair_times != sorted(repair_times):
        raise LedgerError("repair attempt timestamps are not monotonic")
    current_generation = int(value.get("review_generation", 1))
    mapped: list[tuple[dict[str, Any], int]] = []
    for entry in legacy_rounds:
        recorded_at = _timestamp(entry.get("recorded_at"), label="review result")
        generation = 1 + sum(boundary <= recorded_at for boundary in repair_times)
        if generation < 1 or generation > current_generation:
            raise LedgerError("legacy review result maps outside the active generation")
        mapped.append((entry, generation))

    # Do not let the chronology migration bypass the older concurrent-review
    # safety checks. Every historical result must still be backed by exactly one
    # consumed permit for the same gate, generation, and head. A legacy FAIL
    # that was demoted by the old ordering bug needs the explicit blocker-
    # restoring migration, not a generation-only rewrite.
    legacy_markers = {
        (int(entry.get("round", 0)), str(entry.get("gate") or ""))
        for entry, _ in mapped
    }
    if any(
        advisory.get("reason") == "out-of-scope-in-frozen-round"
        and (int(advisory.get("round", 0)), str(advisory.get("gate") or ""))
        in legacy_markers
        for advisory in value.get("advisories", [])
    ):
        raise LedgerError(
            "legacy demoted blocker requires blocker-restoring review migration"
        )
    migrated_heads: dict[int, str] = {}
    for generation in sorted({generation for _, generation in mapped}):
        entries = [entry for entry, item_generation in mapped if item_generation == generation]
        gates = [str(entry.get("gate") or "") for entry in entries]
        if not all(gates) or len(gates) != len(set(gates)):
            raise LedgerError(
                "legacy repair generation has duplicate or missing gate results"
            )
        generation_heads: set[str] = set()
        for entry in entries:
            gate = str(entry["gate"])
            role = next((role for role, value_gate in ROLE_GATES.items() if value_gate == gate), "")
            permits = [
                permit
                for permit in value.get("review_permits", [])
                if permit.get("role") == role
                and int(permit.get("review_generation", 1)) == generation
                and permit.get("receipt_consumed_at")
                and not permit.get("cancelled_at")
            ]
            if len(permits) != 1:
                raise LedgerError(
                    f"legacy repair generation lacks one consumed {gate} permit"
                )
            entry_head = str(entry.get("head") or "").lower()
            permit_head = str(permits[0].get("head") or "").lower()
            if not permit_head or (entry_head and entry_head != permit_head):
                raise LedgerError(
                    f"legacy {gate} result does not match its consumed permit head"
                )
            migrated_heads[id(entry)] = entry_head or permit_head
            generation_heads.add(entry_head or permit_head)
            if (
                entry.get("claimed_verdict") == "FAIL"
                and entry.get("effective_verdict") != "FAIL"
            ):
                raise LedgerError(
                    "legacy demoted FAIL requires blocker-restoring review migration"
                )
        if len(generation_heads) != 1:
            raise LedgerError(
                "legacy repair generation does not share one exact review head"
            )

    migrated = []
    for entry, generation in mapped:
        entry["legacy_result_sequence"] = int(entry.get("round", 0))
        entry["generation"] = generation
        entry["head"] = migrated_heads[id(entry)]
        migrated.append(int(entry.get("round", 0)))

    # A repair may already have been recorded by the affected runtime.  Until
    # any new reviewer consumes it, repair its gate snapshot from the now-bound
    # predecessor generation.  Never rewrite a partially reviewed attempt.
    if value.get("repair_pending_review") and value.get("repair_attempts"):
        attempt = value["repair_attempts"][-1]
        if attempt.get("reviewed_gates") or attempt.get("gate_claims"):
            raise LedgerError(
                "legacy repair generation migration requires an unreviewed pending repair"
            )
        predecessor = current_generation - 1
        required = {
            str(entry.get("gate"))
            for entry in value.get("rounds", [])
            if int(entry.get("generation", 1)) == predecessor and entry.get("gate")
        }
        required.update(
            ROLE_GATES[permit["role"]]
            for permit in value.get("review_permits", [])
            if permit.get("role") in ROLE_GATES
            and int(permit.get("review_generation", 1)) == predecessor
            and not permit.get("cancelled_at")
        )
        required.update(attempt.get("required_gates", []))
        if len(value["repair_attempts"]) > 1:
            required.update(value["repair_attempts"][-2].get("required_gates", []))
        if not required:
            raise LedgerError(
                "legacy repair generation migration cannot derive the required gates"
            )
        attempt["required_gates"] = sorted(required)

    value.setdefault("migrations", []).append(
        {
            "kind": "legacy-repair-generations-v1",
            "migrated_at": now(),
            "rounds": migrated,
        }
    )


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise LedgerError(f"no review ledger at {path}; run `open` first")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read review ledger {path}: {exc}") from exc
    if value.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(f"unsupported review ledger schema in {path}")
    _migrate_legacy_repair_generations(value)
    # v0.7 counted failed gate passes because it had no explicit repair artifact.
    # Preserve that spent budget as synthetic attempts instead of silently
    # resetting a live PR when v0.8 first writes it.
    if "repair_attempts" not in value:
        legacy_failures = [
            entry
            for entry in value.get("rounds", [])
            if entry.get("effective_verdict") == "FAIL"
        ]
        value["repair_attempts"] = [
            {
                "attempt": index,
                "recorded_at": entry.get("recorded_at", value.get("updated_at", now())),
                "completed_at": entry.get(
                    "recorded_at", value.get("updated_at", now())
                ),
                "head": "legacy-unknown",
                "findings": [],
                "open_before": entry.get("blocking", []),
                "open_after": entry.get("blocking", []),
                "closed": [],
                "required_gates": [entry.get("gate", "code-review")],
                "reviewed_gates": [entry.get("gate", "code-review")],
                "legacy": True,
            }
            for index, entry in enumerate(legacy_failures, start=1)
        ]
        value["repair_pending_review"] = False
    value.setdefault("repair_pending_review", False)
    value.setdefault("review_permits", [])
    value.setdefault("review_generation", 1)
    value.setdefault("generation_rebinds", [])
    value.setdefault(
        "work_subject",
        {
            "kind": "jira" if value.get("ticket") else "pr",
            "id": str(value.get("ticket") or value.get("pr")),
            "repository": str(shared_repository_root(project_root()).resolve()),
        },
    )
    value.setdefault(
        "design",
        {"max_rounds": DEFAULT_MAX_DESIGN_ROUNDS, "rounds": [], "escalated": False},
    )
    return value


def ledger_path(args: argparse.Namespace) -> Path:
    root = project_root()
    if args.ledger_dir:
        raise LedgerError(
            "--ledger-dir overrides are not allowed; use the canonical repository config"
        )
    try:
        cfg = canonical_config_path(root, args.config)
        relative = Path(config_scalar(cfg, "review_ledger_dir", DEFAULT_LEDGER_DIR))
        if relative.is_absolute():
            raise LedgerError("review_ledger_dir must be repository-relative")
        directory = migrate_legacy_runtime_dir(root, relative)
    except RuntimeStateError as exc:
        raise LedgerError(str(exc)) from exc
    identifier = str(args.pr).strip()
    if not identifier:
        raise LedgerError(f"invalid pr identifier: {args.pr!r}")
    explicit_kind = getattr(args, "work_kind", None)
    default_kind = "design" if getattr(args, "command", "") == "design-open" else "pr"
    requested = normalized_work_subject(
        str(explicit_kind or default_kind),
        str(getattr(args, "work_id", None) or identifier),
    )

    def canonical_path(subject: dict[str, str]) -> Path:
        slug = (
            re.sub(r"[^A-Za-z0-9_.-]", "-", subject["id"]).strip("-")[:48] or "subject"
        )
        encoded = json.dumps(
            {"pr": identifier, "work_subject": subject},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            directory
            / f"subject-{subject['kind']}-{slug}-{hashlib.sha256(encoded).hexdigest()[:20]}.json"
        )

    target = canonical_path(requested)
    legacy_slug = re.sub(r"[^A-Za-z0-9_.-]", "-", identifier).strip("-")
    legacy = directory / f"pr-{legacy_slug}.json"
    if not target.exists() and legacy.exists():
        legacy_state = load(legacy)
        legacy_subject = legacy_state.get("work_subject")
        explicit_subject = bool(
            getattr(args, "work_kind", None) or getattr(args, "work_id", None)
        )
        if explicit_subject and legacy_subject != requested:
            raise LedgerError(
                "legacy review ledger subject is ambiguous; explicit migration is required"
            )
        if not explicit_subject:
            if (
                str(legacy_state.get("pr")) != identifier
                or not isinstance(legacy_subject, dict)
                or legacy_subject.get("repository") != requested["repository"]
            ):
                raise LedgerError(
                    "legacy review ledger subject is ambiguous; explicit migration is required"
                )
            target = canonical_path(legacy_subject)
        os.replace(legacy, target)
    # Gate commands carry the PR id even when Jira owns the work subject. Locate
    # by immutable state.pr, then enforce repository and any explicitly supplied
    # subject. This also prevents a PR subject and Jira subject from colliding.
    matches = subject_ledger_candidates(
        directory, requested["repository"], identifier
    )
    explicit_subject = bool(
        getattr(args, "work_kind", None) or getattr(args, "work_id", None)
    )
    exact = []
    for candidate in matches:
        try:
            if load(candidate).get("work_subject") == requested:
                exact.append(candidate)
        except LedgerError:
            continue
    if explicit_subject:
        if len(exact) == 1:
            return exact[0]
        if matches:
            raise LedgerError(
                "review ledger PR is already bound to a different immutable work subject"
            )
    elif len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        raise LedgerError(
            "PR ledger is ambiguous; supply --work-kind and --work-id"
        )
    if target.exists():
        return target
    return target


def positive_config_int(
    args: argparse.Namespace, cli_name: str, key: str, default: int
) -> int:
    try:
        cfg = canonical_config_path(project_root(), args.config)
    except RuntimeStateError as exc:
        raise LedgerError(str(exc)) from exc
    configured = config_scalar(cfg, key, str(default))
    override = getattr(args, cli_name, None)
    try:
        value = (
            str(min(int(configured), int(override)))
            if override is not None
            else configured
        )
        result = int(value)
    except ValueError as exc:
        raise LedgerError(f"{key} must be an integer, got {value!r}") from exc
    if result < 1:
        raise LedgerError(f"{key} must be >= 1, got {result}")
    return result


def max_rounds_for(args: argparse.Namespace) -> int:
    try:
        cfg = canonical_config_path(project_root(), args.config)
    except RuntimeStateError as exc:
        raise LedgerError(str(exc)) from exc
    value = config_scalar(cfg, "max_repair_cycles", "")
    if not value:
        value = config_scalar(cfg, "max_review_rounds", str(DEFAULT_MAX_REPAIR_CYCLES))
    if getattr(args, "max_rounds", None) is not None:
        value = str(min(int(value), int(args.max_rounds)))
    try:
        rounds = int(value)
    except ValueError as exc:
        raise LedgerError(
            f"max_repair_cycles must be an integer, got {value!r}"
        ) from exc
    if rounds < 1:
        raise LedgerError(f"max_repair_cycles must be >= 1, got {rounds}")
    return rounds


def normalized_work_subject(kind: str, identifier: str) -> dict[str, str]:
    value = str(identifier).strip()
    if not value:
        raise LedgerError("work subject id must not be empty")
    if kind == "jira":
        value = value.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", value):
            raise LedgerError("Jira work subject id must be a canonical Jira key")
    elif kind == "pr":
        value = re.sub(r"\s+", " ", value)
    elif kind == "design":
        value = re.sub(r"\s+", " ", value)
    else:
        raise LedgerError(f"unsupported work subject kind: {kind}")
    return {
        "kind": kind,
        "id": value,
        "repository": str(shared_repository_root(project_root()).resolve()),
    }


def requested_work_subject(
    args: argparse.Namespace, default_kind: str
) -> dict[str, str]:
    return normalized_work_subject(
        str(getattr(args, "work_kind", None) or default_kind),
        str(getattr(args, "work_id", None) or args.pr),
    )


def bind_work_subject(state: dict[str, Any], requested: dict[str, str]) -> None:
    current = state.get("work_subject")
    if current is not None and current != requested:
        raise LedgerError(f"review ledger work subject is immutable: {current}")
    state["work_subject"] = requested


def new_state(pr: str, max_rounds: int, work_subject: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "pr": str(pr),
        "created_at": now(),
        "updated_at": now(),
        "max_rounds": max_rounds,
        "work_subject": work_subject,
        "repair_attempts": [],
        "repair_pending_review": False,
        "review_permits": [],
        "review_generation": 1,
        "generation_rebinds": [],
        "design": {
            "max_rounds": DEFAULT_MAX_DESIGN_ROUNDS,
            "rounds": [],
            "escalated": False,
        },
        "rounds": [],
        "components": {},
        "escalated": False,
    }


def open_components(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in state["components"].values() if c["status"] == "open"]


def redesign_pending(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for c in open_components(state)
        if c.get("repair_failures", 0) >= 1
        and c.get("repair_failures", 0) > c.get("redesigned_at_repair_failure", 0)
    ]


def restart_limits(state):
    subject = state.get("work_subject") or {}
    if subject.get("kind") != "jira":
        return {}
    repository = shared_repository_root(project_root()).resolve()
    if subject.get("repository") != str(repository):
        raise LedgerError("restart authority requires this repository's immutable work subject")
    try:
        grant = authorized_restart_grant(repository, subject["id"])
    except AuthorityError as exc:
        raise LedgerError(str(exc)) from exc
    return grant["allowances"] if grant else {}


def operator_review_repair_grant(state: dict[str, Any]) -> dict[str, Any]:
    """Read the live, root-owned PR repair grant without trusting ledger data."""
    # Avoid invoking host authority for ordinary ledgers. This repository-owned
    # marker is only a query hint, never authority: a forged marker still has to
    # match a live root-owned grant before it can affect a decision.
    if not state.get("operator_repair_grants"):
        return {}
    pr = str(state.get("pr") or "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", pr):
        return {}
    repository = shared_repository_root(project_root()).resolve()
    try:
        grant = review_repair_grant(repository, pr)
    except AuthorityError as exc:
        raise LedgerError(str(exc)) from exc
    return grant or {}


def _current_generation_permits(state: dict[str, Any]) -> list[dict[str, Any]]:
    generation = int(state.get("review_generation", 1))
    return [
        permit
        for permit in state.get("review_permits", [])
        if int(permit.get("review_generation", 1)) == generation
        and not permit.get("cancelled_at")
        and not permit.get("superseded_at")
    ]


def _round_is_authoritative(
    state: dict[str, Any], entry: dict[str, Any]
) -> bool:
    if entry.get("authoritative") is True:
        return True
    role = {
        "code-review": "code-reviewer",
        "security-review": "security-reviewer",
    }.get(entry.get("gate"))
    if not role or not entry.get("head"):
        return False
    return any(
        permit.get("role") == role
        and permit.get("head") == entry["head"]
        and permit.get("receipt_consumed_at")
        for permit in _current_generation_permits(state)
    )


def _current_generation_heads(state: dict[str, Any]) -> set[str]:
    generation = int(state.get("review_generation", 1))
    heads = {
        str(permit.get("head") or "").lower()
        for permit in _current_generation_permits(state)
        if permit.get("head")
    }
    heads.update(
        str(entry.get("head") or "").lower()
        for entry in state.get("rounds", [])
        if int(entry.get("generation", entry.get("round", 1))) == generation
        and entry.get("head")
        and _round_is_authoritative(state, entry)
    )
    rebinds = [
        item
        for item in state.get("generation_rebinds", [])
        if int(item.get("to_generation", 0)) == generation
    ]
    if len(rebinds) > 1:
        raise LedgerError("review generation has multiple new-head bindings")
    if rebinds:
        heads.add(str(rebinds[0].get("head") or "").lower())
        heads.discard("")
    return heads


def decide(state: dict[str, Any]) -> dict[str, Any]:
    """Derive the loop's next action.

    A repair already recorded within the authorized cycle budget must receive
    its complete review set before an exhausted budget or durable escalation
    can stop the loop. The review result, not recording the repair, determines
    whether the ledger clears or returns to human escalation.
    """
    recorded = len(state["rounds"])
    generation = int(state.get("review_generation", 1))
    # One review round is one logical generation of a PR head, not one gate
    # response. Concurrent gate completion order must not decide which reviewer
    # receives round-one blocking authority.
    next_round = generation
    restart = restart_limits(state)
    operator_grant = operator_review_repair_grant(state)
    operator_repair_ceiling = int(
        operator_grant.get("ceiling_repair_cycles") or 0
    )
    max_rounds = max(
        state["max_rounds"],
        restart.get("repair_cycles", 0),
        operator_repair_ceiling,
    )
    # New ledgers count explicit completed repairs. Old v0.7 ledgers did not
    # record them, so retain their historical failed-pass count on load.
    if "repair_attempts" in state:
        fix_cycles = len(state["repair_attempts"])
    else:
        fix_cycles = sum(
            1 for entry in state["rounds"] if entry["effective_verdict"] == "FAIL"
        )
    blocking = sorted(c["key"] for c in open_components(state))
    pending = sorted(c["key"] for c in redesign_pending(state))

    pending_review = bool(state.get("repair_pending_review"))
    current_entries = [
        entry
        for entry in state["rounds"]
        if int(entry.get("generation", entry.get("round", 1))) == generation
    ]
    gate_verdicts = {
        entry["gate"]: entry["effective_verdict"] for entry in current_entries
    }
    gate_authority = {
        entry["gate"]: _round_is_authoritative(state, entry)
        for entry in current_entries
    }
    generation_heads = _current_generation_heads(state)
    required_gates = {
        ROLE_GATES[item["role"]]
        for item in state.get("review_permits", [])
        if item.get("role") in ROLE_GATES
        and int(item.get("review_generation", 1)) == generation
        and not item.get("superseded_at")
    }
    required_gates.update(
        gate
        for item in state.get("generation_rebinds", [])
        if int(item.get("to_generation", 0)) == generation
        for gate in item.get("required_gates", [])
    )
    if pending_review and state.get("repair_attempts"):
        required_gates.update(state["repair_attempts"][-1].get("required_gates", []))
    missing_gates = sorted(required_gates - set(gate_verdicts))
    rebound_generation_pending_review = bool(
        missing_gates
        and any(
            int(item.get("to_generation", 0)) == generation
            for item in state.get("generation_rebinds", [])
        )
    )
    gates_clear = (
        not pending_review
        and bool(gate_verdicts)
        and not missing_gates
        and not blocking
        and all(verdict == "PASS" for verdict in gate_verdicts.values())
        and all(gate_authority.values())
        and len(generation_heads) == 1
    )

    cap_reached = not pending_review and fix_cycles >= max_rounds and bool(blocking)
    escalation_acknowledged = bool(restart) or fix_cycles < operator_repair_ceiling
    elif_escalated = state.get("escalated") and not escalation_acknowledged
    if gates_clear:
        action = ACTION_CLEAR
    elif pending_review or rebound_generation_pending_review:
        action = ACTION_REVIEW
    elif elif_escalated or cap_reached:
        action = ACTION_ESCALATE
    elif pending:
        action = ACTION_REDESIGN
    else:
        action = ACTION_REVIEW

    return {
        "pr": state["pr"],
        "rounds_recorded": recorded,
        "next_round": next_round,
        "max_rounds": max_rounds,
        "operator_repair_ceiling": operator_repair_ceiling,
        "fix_cycles": fix_cycles,
        "fix_cycles_remaining": max(0, max_rounds - fix_cycles),
        "next_scope_mode": FULL if generation == 1 else FROZEN,
        "uncertainty_rule": "investigate-on-doubt"
        if fix_cycles == 0
        else "advisory-on-doubt",
        "open_blocking": blocking,
        "redesign_required": pending,
        "gate_verdicts": gate_verdicts,
        "generation_head": next(iter(generation_heads), "")
        if len(generation_heads) == 1
        else "",
        "generation_head_conflict": len(generation_heads) > 1,
        "review_generation": generation,
        "required_gates": sorted(required_gates),
        "missing_gates": missing_gates,
        "rebound_generation_pending_review": rebound_generation_pending_review,
        "cap_reached": cap_reached,
        "repair_pending_review": pending_review,
        "next_action": action,
    }


# --- commands -----------------------------------------------------------------


def cmd_open(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    rounds = max_rounds_for(args)
    subject = requested_work_subject(args, "pr")
    with locked(path):
        if path.exists():
            state = load(path)
            bind_work_subject(state, subject)
            # Existing caps are immutable from worker-facing CLI syntax.
            state["max_rounds"] = min(int(state.get("max_rounds", rounds)), rounds)
            _design_state(state)["max_rounds"] = min(
                int(_design_state(state)["max_rounds"]),
                positive_config_int(
                    args,
                    "max_design_rounds",
                    "max_design_rounds",
                    DEFAULT_MAX_DESIGN_ROUNDS,
                ),
            )
            save(path, state)
        else:
            state = new_state(args.pr, rounds, subject)
            state["design"]["max_rounds"] = positive_config_int(
                args,
                "max_design_rounds",
                "max_design_rounds",
                DEFAULT_MAX_DESIGN_ROUNDS,
            )
            save(path, state)
        emit({"ledger": str(path), **decide(state)})


def cmd_migrate_concurrent_review(args: argparse.Namespace) -> None:
    """Repair the pre-generation ordering defect without weakening any gate.

    Older ledgers numbered each concurrently launched gate response as a new
    round. A failing initial response recorded second could therefore have its
    blockers demoted by the scope freeze. This migration is deliberately
    one-way and fail-closed: it only handles an unrepaired initial generation,
    proves both gate records came from consumed generation-one permits for the
    same head, and promotes the affected advisories back to blockers.
    """
    path = ledger_path(args)
    with locked(path):
        if not args.reason.strip():
            raise LedgerError("concurrent-review migration requires a non-empty audit reason")
        state = load(path)
        legacy = [entry for entry in state["rounds"] if "generation" not in entry]
        if not legacy:
            emit({"ledger": str(path), "migration": "not-needed", **decide(state)})
            return
        if state.get("repair_attempts") or int(state.get("review_generation", 1)) != 1:
            raise LedgerError(
                "automatic concurrent-review migration requires an unrepaired initial generation"
            )
        if len({entry["gate"] for entry in legacy}) != len(legacy):
            raise LedgerError(
                "automatic concurrent-review migration requires one initial result per gate"
            )

        permits_by_gate: dict[str, dict[str, Any]] = {}
        for permit in state.get("review_permits", []):
            gate = ROLE_GATES.get(str(permit.get("role", "")))
            if (
                gate
                and int(permit.get("review_generation", 1)) == 1
                and permit.get("receipt_consumed_at")
                and not permit.get("superseded_at")
            ):
                if gate in permits_by_gate:
                    raise LedgerError(
                        f"automatic concurrent-review migration found multiple consumed {gate} permits"
                    )
                permits_by_gate[gate] = permit
        missing = sorted({entry["gate"] for entry in legacy} - set(permits_by_gate))
        if missing:
            raise LedgerError(
                "automatic concurrent-review migration lacks consumed permits for: "
                + ", ".join(missing)
            )
        heads = {permits_by_gate[entry["gate"]].get("head") for entry in legacy}
        if len(heads) != 1 or not next(iter(heads), None):
            raise LedgerError(
                "automatic concurrent-review migration requires one exact shared review head"
            )

        legacy_rounds = {
            (int(entry["round"]), entry["gate"]): entry for entry in legacy
        }
        promoted_by_gate: dict[str, list[tuple[str, str]]] = {}
        finding_details_by_gate: dict[str, dict[str, dict[str, Any]]] = {}
        retained_advisories = []
        for advisory in state.get("advisories", []):
            marker = (int(advisory.get("round", 0)), advisory.get("gate"))
            entry = legacy_rounds.get(marker)
            if (
                entry
                and entry.get("claimed_verdict") == "FAIL"
                and advisory.get("reason") == "out-of-scope-in-frozen-round"
            ):
                key = resolve_alias(state, normalize_key(str(advisory["key"])))
                promoted_by_gate.setdefault(str(advisory["gate"]), []).append(
                    (key, str(advisory.get("display") or advisory["key"]))
                )
                if "finding" in advisory:
                    finding_details_by_gate.setdefault(str(advisory["gate"]), {})[
                        key
                    ] = advisory["finding"]
                continue
            retained_advisories.append(advisory)

        for entry in legacy:
            entry["legacy_result_sequence"] = int(entry["round"])
            entry["round"] = 1
            entry["generation"] = 1
            entry["head"] = str(permits_by_gate[entry["gate"]]["head"])
            entry["scope_mode"] = FULL
            promoted = sorted(key for key, _ in promoted_by_gate.get(entry["gate"], []))
            if promoted:
                entry["blocking"] = sorted(set(entry.get("blocking", [])) | set(promoted))
                entry["advisory"] = sorted(set(entry.get("advisory", [])) - set(promoted))
                entry["effective_verdict"] = "FAIL"

        state["advisories"] = retained_advisories
        promoted_keys: list[str] = []
        for gate, accepted in promoted_by_gate.items():
            apply_gate_claims(
                state,
                gate=gate,
                accepted=accepted,
                finding_details=finding_details_by_gate.get(gate, {}),
                round_no=1,
            )
            promoted_keys.extend(key for key, _ in accepted)
        migration = {
            "kind": "concurrent-review-generation-v1",
            "recorded_at": now(),
            "head": next(iter(heads)),
            "gates": sorted(entry["gate"] for entry in legacy),
            "promoted_blocking": sorted(set(promoted_keys)),
            "reason": args.reason.strip(),
        }
        state.setdefault("migrations", []).append(migration)
        save(path, state)
        emit({"ledger": str(path), "migration": migration, **decide(state)})


def _component(
    state: dict[str, Any], key: str, raw: str, round_no: int
) -> dict[str, Any]:
    component = state["components"].get(key)
    if component is None:
        component = {
            "key": key,
            "display": raw.strip(),
            "strikes": 0,
            "status": "open",
            "first_round": round_no,
            "rounds": [],
            "gates": [],
            "claims": {},
            "redesigned_at_strike": 0,
            "repair_failures": 0,
            "redesigned_at_repair_failure": 0,
        }
        state["components"][key] = component
    if not isinstance(component.get("claims"), dict):
        component["claims"] = {}
    if not component["claims"] and component.get("gates"):
        component["claims"].update(
            {
            gate: {
                "status": component.get("status", "open"),
                "last_round": component.get("last_round", round_no),
                "generation": component.get("review_generation", 1),
            }
            for gate in component.get("gates", [])
            }
        )
    return component


def apply_gate_claims(
    state: dict[str, Any],
    *,
    gate: str,
    accepted: list[tuple[str, str]],
    finding_details: dict[str, dict[str, Any]],
    round_no: int,
) -> list[str]:
    """Atomically apply one gate's claims without disturbing other owners."""
    accepted_keys = {key for key, _ in accepted}
    for key, raw in accepted:
        component = _component(state, key, raw, round_no)
        component["strikes"] += 1
        component["status"] = "open"
        component["display"] = raw.strip()
        component["last_round"] = round_no
        component["rounds"].append(round_no)
        if key in finding_details:
            component["finding"] = finding_details[key]
        if gate not in component["gates"]:
            component["gates"].append(gate)
        component["claims"][gate] = {
            "status": "open",
            "last_round": round_no,
            "generation": state.get("review_generation", 1),
        }

    resolved: list[str] = []
    for key, component in state["components"].items():
        claims = _component(state, key, component.get("display", key), round_no)[
            "claims"
        ]
        claim = claims.get(gate)
        if claim and claim.get("status") == "open" and key not in accepted_keys:
            claim.update(
                {
                    "status": "resolved",
                    "resolved_round": round_no,
                    "resolved_generation": state.get("review_generation", 1),
                }
            )
        aggregate_open = any(item.get("status") == "open" for item in claims.values())
        was_open = component.get("status") == "open"
        component["status"] = "open" if aggregate_open else "resolved"
        if was_open and not aggregate_open:
            component["resolved_round"] = round_no
            component["resolved_by_gate"] = gate
            resolved.append(key)
    return resolved


def cmd_record(args: argparse.Namespace) -> None:
    finding_details: dict[str, dict[str, Any]] = {}
    if args.result:
        if args.verdict or args.blocking or args.advisory or args.regression:
            raise LedgerError(
                "--result cannot be combined with manual verdict or finding flags"
            )
        try:
            structured = json.loads(Path(args.result).read_text(encoding="utf-8"))
            context_pipeline.validate_review_output(structured, args.gate)
        except (OSError, json.JSONDecodeError, context_pipeline.ContextError) as exc:
            raise LedgerError(f"invalid structured review result: {exc}") from exc
        args.verdict = structured["verdict"]
        args.blocking = [
            item["component"]
            for item in structured["findings"]
            if item["disposition"] == "blocking"
        ]
        args.advisory = [
            item["component"]
            for item in structured["findings"]
            if item["disposition"] == "advisory"
        ]
        args.regression = [
            item["component"] for item in structured["findings"] if item["regression"]
        ]
        finding_details = {
            normalize_key(item["component"]): item for item in structured["findings"]
        }
    elif not args.verdict:
        raise LedgerError("record requires either --result or --verdict")
    elif args.verdict == "PASS":
        raise LedgerError(
            "review PASS requires a structured --result and completion receipt"
        )
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        if args.result:
            role = {
                "code-review": "code-reviewer",
                "security-review": "security-reviewer",
            }.get(args.gate)
            if not role or not args.phase_permit or not args.head:
                raise LedgerError(
                    "structured review record requires --phase-permit and exact --head"
                )
            if not consume_completion(
                state,
                token=args.phase_permit,
                role=role,
                head=args.head,
                result=structured,
                timestamp=now(),
            ):
                raise LedgerError(
                    "review result lacks a matching single-use provider completion receipt"
                )
        plan = decide(state)
        if plan["next_action"] == ACTION_ESCALATE:
            raise LedgerError(
                f"PR {state['pr']} spent its {plan['max_rounds']} fix cycles with "
                f"{len(plan['open_blocking'])} blocking component(s) still open. Hand it to a "
                f"human (`handoff {state['pr']}`), or request a root-issued ticket restart allowance."
            )
        if args.verdict == "PASS" and args.blocking:
            raise LedgerError(
                f"verdict PASS contradicts {len(args.blocking)} blocking finding(s): "
                f"{', '.join(args.blocking)}"
            )
        generation = int(state.get("review_generation", 1))
        round_no = generation
        scope = FULL if generation == 1 else FROZEN
        # The security gate never loses blocking authority to the scope freeze: a
        # data leak found late is not a process nit.
        exempt = args.gate == "security-review"
        finding_details = {
            resolve_alias(state, key): value for key, value in finding_details.items()
        }
        regressions = {
            resolve_alias(state, normalize_key(k)) for k in args.regression
        }

        accepted: list[tuple[str, str]] = []
        demoted: list[tuple[str, str]] = []
        accepted_seen: set[str] = set()
        for raw in args.blocking:
            key = resolve_alias(state, normalize_key(raw))
            known = key in state["components"]
            if scope == FULL or known or key in regressions or exempt:
                if key not in accepted_seen:
                    accepted.append((key, raw))
                    accepted_seen.add(key)
            else:
                demoted.append((key, raw))

        accepted_keys = {key for key, _ in accepted}
        pending_attempt = (
            state["repair_attempts"][-1]
            if state.get("repair_pending_review") and state.get("repair_attempts")
            else None
        )
        if pending_attempt is not None:
            staged = pending_attempt.setdefault("gate_claims", {})
            if args.gate in staged:
                raise LedgerError(
                    f"gate {args.gate} already recorded for review generation "
                    f"{state.get('review_generation', 1)}"
                )
            staged[args.gate] = {
                "round": round_no,
                "accepted": [
                    {
                        "key": key,
                        "display": raw,
                        **(
                            {"finding": finding_details[key]}
                            if key in finding_details
                            else {}
                        ),
                    }
                    for key, raw in accepted
                ],
            }
            resolved = []
        else:
            resolved = apply_gate_claims(
                state,
                gate=args.gate,
                accepted=accepted,
                finding_details=finding_details,
                round_no=round_no,
            )

        advisories = [
            {
                "key": resolve_alias(state, normalize_key(raw)),
                "display": raw.strip(),
                "reason": "reported-advisory",
                **(
                    {"finding": finding_details[resolve_alias(state, normalize_key(raw))]}
                    if resolve_alias(state, normalize_key(raw)) in finding_details
                    else {}
                ),
            }
            for raw in args.advisory
        ] + [
            {
                "key": key,
                "display": raw.strip(),
                "reason": "out-of-scope-in-frozen-round",
                **({"finding": finding_details[key]} if key in finding_details else {}),
            }
            for key, raw in demoted
        ]
        state.setdefault("advisories", []).extend(
            {**item, "round": round_no, "gate": args.gate} for item in advisories
        )

        effective = "FAIL" if accepted else "PASS"

        state["rounds"].append(
            {
                "round": round_no,
                "generation": generation,
                "gate": args.gate,
                "head": args.head.lower() if args.head else "",
                "scope_mode": scope,
                "claimed_verdict": args.verdict,
                "effective_verdict": effective,
                "recorded_at": now(),
                "blocking": sorted(accepted_keys),
                "advisory": sorted({item["key"] for item in advisories}),
                "resolved": sorted(resolved),
                "authoritative": bool(args.result),
            }
        )
        if state.get("repair_pending_review") and state.get("repair_attempts"):
            attempt = state["repair_attempts"][-1]
            if not args.head:
                raise LedgerError("recording a repaired-head review requires --head")
            if args.head.lower() != attempt["head"]:
                raise LedgerError(
                    f"review head {args.head.lower()} does not match repaired head {attempt['head']}"
                )
            attempt.setdefault("reviewed_gates", [])
            if args.gate not in attempt["reviewed_gates"]:
                attempt["reviewed_gates"].append(args.gate)
        save(path, state)

        result = {
            "ledger": str(path),
            "round": round_no,
            "gate": args.gate,
            "scope_mode": scope,
            "claimed_verdict": args.verdict,
            "effective_verdict": effective,
            "accepted_blocking": sorted(accepted_keys),
            "demoted_to_advisory": sorted(key for key, _ in demoted),
            "resolved_this_round": sorted(resolved),
            **decide(state),
        }
        emit(result)


def _load_repair_report(path: str) -> dict[str, Any]:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read repair report: {exc}") from exc
    if (
        set(report) != {"schema_version", "head", "findings"}
        or report.get("schema_version") != 1
    ):
        raise LedgerError(
            "repair report requires exactly schema_version=1, head, and findings"
        )
    if not isinstance(report["head"], str) or not re.fullmatch(
        r"[0-9a-fA-F]{7,64}", report["head"]
    ):
        raise LedgerError(
            "repair report head must be a 7-64 character hexadecimal commit id"
        )
    if not isinstance(report["findings"], list) or not report["findings"]:
        raise LedgerError("repair report findings must be a non-empty array")
    required = {"component", "status", "root_cause", "change", "verification"}
    for item in report["findings"]:
        if not isinstance(item, dict) or set(item) != required:
            raise LedgerError(
                f"each repair finding requires exactly: {', '.join(sorted(required))}"
            )
        if item["status"] not in {"closed", "unresolved"}:
            raise LedgerError("repair finding status must be closed or unresolved")
        for key in ("component", "root_cause", "change", "verification"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise LedgerError(f"repair finding {key} must be non-empty text")
    return report


def cmd_repair_brief(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    plan = decide(state)
    if not plan["open_blocking"]:
        raise LedgerError("cannot build a repair brief without open blocking findings")
    lines = [
        f"REPAIR ATTEMPT {plan['fix_cycles'] + 1} OF {plan['max_rounds']} FOR PR {state['pr']}",
        "",
        "Treat every component below as a stable finding ID. Before editing, map each ID",
        "to its root cause, planned change, affected boundaries, verification, and objective",
        "closure condition. After editing, write one repair-report JSON object and record it",
        "with review-ledger.py record-repair before any reviewer is launched.",
        "",
        "OPEN BLOCKING FINDINGS:",
    ]
    for component in sorted(open_components(state), key=lambda item: item["key"]):
        lines.append(f"- `{component['key']}`")
        finding = component.get("finding")
        if finding:
            lines.append(f"  {finding['title']}: {finding['explanation']}")
        lines.append(
            "  Closure must be demonstrated by a named regression test or equivalent evidence."
        )
    lines += [
        "",
        "The report schema is:",
        '{"schema_version":1,"head":"<commit>","findings":[{"component":"<path>:<symbol>",',
        '"status":"closed|unresolved","root_cause":"...","change":"...","verification":"..."}]}',
        "Every open ID must appear exactly once. Reviewer re-execution, not the implementer's",
        "self-assessment, determines whether a finding is actually closed.",
    ]
    print("\n".join(lines))


def cmd_record_repair(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    report = _load_repair_report(args.report)
    with locked(path):
        state = load(path)
        state.setdefault("review_generation", 1)
        if any("generation" not in entry for entry in state.get("rounds", [])):
            raise LedgerError(
                "legacy review results require migrate-concurrent-review before repair"
            )
        plan = decide(state)
        if state.get("repair_pending_review"):
            raise LedgerError(
                "the previous repair is still waiting for its complete review set"
            )
        if (
            plan["next_action"] == ACTION_ESCALATE
            or plan["fix_cycles"] >= plan["max_rounds"]
        ):
            raise LedgerError(
                "the repair budget is spent; render handoff instead of starting another repair"
            )
        open_keys = set(plan["open_blocking"])
        report_keys = [normalize_key(item["component"]) for item in report["findings"]]
        if len(report_keys) != len(set(report_keys)):
            raise LedgerError("repair report contains duplicate component IDs")
        if set(report_keys) != open_keys:
            missing = sorted(open_keys - set(report_keys))
            extra = sorted(set(report_keys) - open_keys)
            raise LedgerError(
                f"repair report must cover the exact open set; missing={missing}, extra={extra}"
            )
        review_plan = decide(state)
        gate_verdicts = review_plan["gate_verdicts"]
        required_gates = set(gate_verdicts) | set(review_plan["required_gates"])
        if state.get("repair_attempts"):
            required_gates.update(
                state["repair_attempts"][-1].get("required_gates", [])
            )
        attempt = {
            "attempt": len(state.setdefault("repair_attempts", [])) + 1,
            "recorded_at": now(),
            "head": report["head"].lower(),
            "findings": [
                {**item, "component": normalize_key(item["component"])}
                for item in report["findings"]
            ],
            "open_before": sorted(open_keys),
            "required_gates": sorted(required_gates) or ["code-review"],
            "reviewed_gates": [],
        }
        state["repair_attempts"].append(attempt)
        state["review_generation"] += 1
        superseded_at = now()
        for permit in state.get("review_permits", []):
            if (
                permit.get("review_generation", 1) < state["review_generation"]
                and not permit.get("receipt_consumed_at")
            ):
                permit["superseded_at"] = superseded_at
                permit["superseded_by_generation"] = state["review_generation"]
        state["repair_pending_review"] = True
        save(path, state)
        emit(
            {
                "repair_recorded": attempt["attempt"],
                "head": attempt["head"],
                **decide(state),
            }
        )


def cmd_complete_repair_review(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        if not state.get("repair_pending_review") or not state.get("repair_attempts"):
            raise LedgerError("no repaired head is awaiting review completion")
        attempt = state["repair_attempts"][-1]
        missing = sorted(
            set(attempt["required_gates"]) - set(attempt["reviewed_gates"])
        )
        if missing:
            raise LedgerError(
                f"repair review is incomplete; missing gates: {', '.join(missing)}"
            )
        for gate in attempt["required_gates"]:
            staged = attempt.get("gate_claims", {}).get(gate)
            if not staged:
                raise LedgerError(f"repair review has no staged claims for gate: {gate}")
            accepted = [
                (str(item["key"]), str(item["display"]))
                for item in staged.get("accepted", [])
            ]
            details = {
                str(item["key"]): item["finding"]
                for item in staged.get("accepted", [])
                if "finding" in item
            }
            apply_gate_claims(
                state,
                gate=gate,
                accepted=accepted,
                finding_details=details,
                round_no=int(staged["round"]),
            )
        attempt["claims_finalized_at"] = now()
        remaining = {item["key"] for item in open_components(state)}
        attempt["open_after"] = sorted(remaining)
        attempt["closed"] = sorted(set(attempt["open_before"]) - remaining)
        attempt["completed_at"] = now()
        for key in remaining.intersection(attempt["open_before"]):
            state["components"][key]["repair_failures"] = (
                state["components"][key].get("repair_failures", 0) + 1
            )
        state["repair_pending_review"] = False
        save(path, state)
        emit({"repair_review_completed": attempt["attempt"], **decide(state)})


def cmd_metrics(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    attempts = state.get("repair_attempts", [])
    total_initial = len(attempts[0]["open_before"]) if attempts else 0
    first_closed = len(attempts[0].get("closed", [])) if attempts else 0
    all_closed = (
        len(set().union(*(set(item.get("closed", [])) for item in attempts)))
        if attempts
        else 0
    )

    def elapsed(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        return round(
            (
                datetime.fromisoformat(end) - datetime.fromisoformat(start)
            ).total_seconds(),
            3,
        )

    repair_durations = [
        elapsed(item.get("recorded_at"), item.get("completed_at")) for item in attempts
    ]
    design_rounds = state.get("design", {}).get("rounds", [])
    emit(
        {
            "pr": state["pr"],
            "review_passes": len(state["rounds"]),
            "repair_attempts": len(attempts),
            "initial_blocking_findings": total_initial,
            "first_repair_closure_rate": (first_closed / total_initial)
            if total_initial
            else None,
            "cumulative_repair_closure_rate": (all_closed / total_initial)
            if total_initial
            else None,
            "no_op_repairs": sum(1 for item in attempts if not item.get("closed")),
            "repair_review_seconds": repair_durations,
            "design_rounds": len(design_rounds),
            "design_elapsed_seconds": elapsed(
                design_rounds[0].get("recorded_at") if design_rounds else None,
                design_rounds[-1].get("recorded_at") if design_rounds else None,
            ),
            "review_elapsed_seconds": elapsed(
                state["rounds"][0].get("recorded_at") if state["rounds"] else None,
                state["rounds"][-1].get("recorded_at") if state["rounds"] else None,
            ),
            "new_blocking_after_round_one": sum(
                1
                for entry in state["rounds"]
                if entry["scope_mode"] == FROZEN
                for key in entry["blocking"]
                if state["components"].get(key, {}).get("first_round") == entry["round"]
            ),
            "open_blocking": decide(state)["open_blocking"],
        }
    )


def _design_state(
    state: dict[str, Any], max_rounds: int | None = None
) -> dict[str, Any]:
    design = state.setdefault(
        "design",
        {
            "max_rounds": max_rounds or DEFAULT_MAX_DESIGN_ROUNDS,
            "rounds": [],
            "escalated": False,
        },
    )
    if max_rounds is not None:
        design["max_rounds"] = max_rounds
    return design


def _design_plan(state: dict[str, Any]) -> dict[str, Any]:
    design = _design_state(state)
    rounds = design["rounds"]
    restart = restart_limits(state)
    maximum = max(design["max_rounds"], restart.get("design_rounds", 0))
    if rounds and rounds[-1]["verdict"] == "PASS":
        action = "implement"
    elif (design.get("escalated") and not restart) or len(rounds) >= maximum:
        action = ACTION_ESCALATE
    else:
        action = "redesign"
    return {
        "target": state["pr"],
        "design_rounds": len(rounds),
        "max_design_rounds": maximum,
        "design_rounds_remaining": max(0, maximum - len(rounds)),
        "next_action": action,
    }


def cmd_design_open(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    rounds = max_rounds_for(args)
    design_rounds = positive_config_int(
        args, "max_design_rounds", "max_design_rounds", DEFAULT_MAX_DESIGN_ROUNDS
    )
    subject = requested_work_subject(args, "design")
    with locked(path):
        existed = path.exists()
        state = load(path) if existed else new_state(args.pr, rounds, subject)
        bind_work_subject(state, subject)
        design = _design_state(state)
        design["max_rounds"] = min(
            int(design.get("max_rounds", design_rounds)), design_rounds
        )
        save(path, state)
        emit({"ledger": str(path), **_design_plan(state)})


def cmd_design_record(args: argparse.Namespace) -> None:
    artifact: dict[str, Any] | None = None
    if args.result:
        if args.verdict or args.evidence:
            raise LedgerError("--result cannot be combined with manual design fields")
        try:
            artifact = json.loads(Path(args.result).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"invalid design result: {exc}") from exc
        required = {
            "schema_version",
            "gate",
            "verdict",
            "source_sha",
            "artifact",
            "artifact_sha256",
            "checks",
            "phase_permit",
        }
        if set(artifact) != required or artifact.get("schema_version") != 1:
            raise LedgerError(
                "design result requires exactly schema_version=1, gate, verdict, source_sha, artifact, checks"
            )
        if artifact.get("gate") != "design-review" or artifact.get("verdict") not in {
            "PASS",
            "FAIL",
        }:
            raise LedgerError("design result gate/verdict is invalid")
        if not re.fullmatch(
            r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})",
            str(artifact.get("source_sha") or ""),
        ):
            raise LedgerError("design result source_sha must be a full commit id")
        if (
            not str(artifact.get("artifact") or "").strip()
            or not isinstance(artifact.get("checks"), list)
            or not artifact["checks"]
        ):
            raise LedgerError(
                "design result requires a named artifact and non-empty checks"
            )
        if any(
            not isinstance(item, dict) or item.get("status") not in {"pass", "fail"}
            for item in artifact["checks"]
        ):
            raise LedgerError("every design check requires status pass or fail")
        if artifact["verdict"] == "PASS" and any(
            item["status"] != "pass" for item in artifact["checks"]
        ):
            raise LedgerError("design PASS contradicts a failed check")
        try:
            actual_head = (
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=project_root(),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                .stdout.strip()
                .lower()
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise LedgerError(
                "cannot verify design result against repository HEAD"
            ) from exc
        if actual_head != str(artifact["source_sha"]).lower():
            raise LedgerError(
                f"design result source {artifact['source_sha']} does not match current HEAD {actual_head}"
            )
        artifact_path = (project_root() / str(artifact["artifact"])).resolve()
        root = project_root()
        if artifact_path != root and root not in artifact_path.parents:
            raise LedgerError("design artifact escapes the repository")
        if not artifact_path.is_file():
            raise LedgerError("design artifact must be an existing repository file")
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if artifact.get("artifact_sha256") != digest:
            raise LedgerError(
                "design artifact digest does not match the reviewed artifact"
            )
        args.verdict = artifact["verdict"]
        args.evidence = artifact["artifact"]
    elif args.verdict == "PASS":
        raise LedgerError(
            "design PASS requires a machine-readable --result bound to source_sha"
        )
    elif not args.verdict or not args.evidence:
        raise LedgerError(
            "design FAIL requires --verdict and --evidence, or use --result"
        )
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        if artifact and not consume_completion(
            state,
            token=str(artifact["phase_permit"]),
            role="design-reviewer",
            head=str(artifact["source_sha"]),
            result=artifact,
            timestamp=now(),
        ):
            raise LedgerError(
                "design result lacks a matching single-use reviewer completion receipt"
            )
        design = _design_state(state)
        plan = _design_plan(state)
        if plan["next_action"] == ACTION_ESCALATE:
            raise LedgerError(
                "the design-round budget is spent; hand off instead of running another round"
            )
        if plan["next_action"] == "implement":
            raise LedgerError("the design gate already passed")
        design["rounds"].append(
            {
                "round": len(design["rounds"]) + 1,
                "verdict": args.verdict,
                "evidence": args.evidence,
                **({"result": artifact} if artifact else {}),
                "recorded_at": now(),
            }
        )
        if args.verdict == "FAIL" and len(design["rounds"]) >= design["max_rounds"]:
            design["escalated"] = True
        save(path, state)
        emit(_design_plan(state))


def cmd_design_handoff(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    design = _design_state(state)
    lines = [
        f"# Design gate stopped for {state['pr']}",
        "",
        f"Rounds used: {len(design['rounds'])} of {design['max_rounds']}.",
        "No production implementation is authorized while the last design verdict is FAIL.",
        "",
        "## Round history",
    ]
    for item in design["rounds"]:
        lines.append(
            f"- Round {item['round']}: {item['verdict']} -- {item['evidence']}"
        )
    print("\n".join(lines))


def cmd_status(args: argparse.Namespace) -> None:
    state = load(ledger_path(args))
    components = {
        key: {
            "strikes": component["strikes"],
            "status": component["status"],
            "gates": component["gates"],
            "rounds": component["rounds"],
            "display": component["display"],
            "claims": component.get("claims", {}),
        }
        for key, component in sorted(state["components"].items())
    }
    emit(
        {
            **decide(state),
            "work_subject": state["work_subject"],
            "escalated": bool(state.get("escalated")),
            "operator_repair_grants": state.get("operator_repair_grants", []),
            "components": components,
        }
    )


def cmd_brief(args: argparse.Namespace) -> None:
    """Emit the round-aware review contract to paste into the reviewer's brief."""
    state = load(ledger_path(args))
    plan = decide(state)
    lines = [
        f"REVIEW PASS {plan['next_round']} on PR {state['pr']} "
        f"(scope mode: {plan['next_scope_mode']})",
        f"Fix cycles used: {plan['fix_cycles']} of at most {plan['max_rounds']}. "
        f"When that budget is spent with findings still open, the loop stops for a "
        f"human instead of running another round.",
        "",
    ]
    if plan["next_scope_mode"] == FULL:
        lines += [
            "This is round 1. Sweep the entire diff with full blocking authority.",
            "Every defect class in your brief may block. Be exhaustive now -- findings",
            "you do not raise this round lose blocking authority in later rounds.",
        ]
    else:
        lines += [
            "Round 2+ scope freeze. Still sweep the ENTIRE diff -- a fix can break",
            "something elsewhere -- but only these may block:",
            "  1. Open ledger components listed below (the findings already agreed on).",
            "  2. A regression in the delta since the previous round. Report it with",
            "     the `--regression` flag so it keeps blocking authority.",
            "  3. Any security or data-loss finding, at any time.",
            "Anything newly noticed on code untouched since the last round is ADVISORY:",
            "report it for the PR body, but it does not FAIL this gate.",
        ]
    doubt = (
        "Investigate uncertainty before the verdict. Block only with a concrete "
        "failing input or precondition, production path, wrong outcome and impact, "
        "plus a reproduction or exact falsifying assertion. If that evidence "
        "remains incomplete, file it as ADVISORY and name what would settle it."
        if plan["uncertainty_rule"] == "investigate-on-doubt"
        else (
            f"This is fix cycle {plan['fix_cycles'] + 1}. A false FAIL no longer costs one loop -- it costs\n"
            "the next one too. When unsure whether something is a real defect, file it as\n"
            "ADVISORY and name the exact evidence that would settle it."
        )
    )
    lines += ["", doubt, ""]

    open_list = open_components(state)
    if open_list:
        lines.append("OPEN LEDGER COMPONENTS (reuse these exact keys):")
        for component in sorted(open_list, key=lambda c: c["key"]):
            mark = (
                "  [REDESIGN REQUIRED]" if component in redesign_pending(state) else ""
            )
            lines.append(
                f"  - `{component['key']}` strikes={component['strikes']}"
                f" gates={','.join(component['gates'])}{mark}"
            )
    else:
        lines.append("OPEN LEDGER COMPONENTS: none.")
    lines += [
        "",
        "Set every finding's JSON `component` field to the bare `<path>:<symbol>` key --",
        "the file path plus the enclosing symbol. Do not include `[component: ...]` or",
        "any other wrapper; never use whitespace, a line number, or a free-text subsystem",
        "name. Put prose test names in `title`; use the test file and a stable test symbol",
        "for `component`.",
        "If your finding is the same defect as an open component above, reuse its key",
        "verbatim so the strike lands on it.",
    ]
    print("\n".join(lines))


def cmd_handoff(args: argparse.Namespace) -> None:
    """Render the human escalation report when the loop did not converge."""
    state = load(ledger_path(args))
    plan = decide(state)
    lines = [
        f"# Review loop stopped for PR {state['pr']}",
        "",
        f"Fix cycles used: {plan['fix_cycles']} of {plan['max_rounds']} "
        f"across {plan['rounds_recorded']} review pass(es). "
        f"Next action: {plan['next_action']}.",
        "",
        "This PR was NOT merged and NOT abandoned. The loop hit its configured round",
        "cap with blocking findings still open, so it stopped for a human decision",
        "rather than looping further.",
        "",
        "## Still blocking",
    ]
    open_list = sorted(open_components(state), key=lambda c: -c["strikes"])
    if open_list:
        for component in open_list:
            lines.append(
                f"- `{component['key']}` -- {component['strikes']} strike(s), "
                f"rounds {component['rounds']}, gates {', '.join(component['gates'])}"
            )
            if component.get("finding"):
                finding = component["finding"]
                lines.append(f"  {finding['title']}: {finding['explanation']}")
    else:
        lines.append("- none")
    lines += ["", "## Round history"]
    for entry in state["rounds"]:
        lines.append(
            f"- Round {entry['round']} ({entry['gate']}, {entry['scope_mode']}): "
            f"{entry['effective_verdict']}"
            + (
                f" -- resolved {', '.join(entry['resolved'])}"
                if entry["resolved"]
                else ""
            )
        )
    attempts = state.get("repair_attempts", [])
    if attempts:
        lines += ["", "## Repair history"]
        for attempt in attempts:
            lines.append(
                f"- Attempt {attempt['attempt']} at `{attempt['head']}`: "
                f"closed {', '.join(attempt.get('closed', [])) or 'none'}; "
                f"still open {', '.join(attempt.get('open_after', attempt['open_before'])) or 'none'}"
            )
            for item in attempt["findings"]:
                lines.append(
                    f"  - `{item['component']}`: root cause={item['root_cause']}; "
                    f"change={item['change']}; verification={item['verification']}"
                )
    advisories = state.get("advisories", [])
    if advisories:
        lines += ["", "## Advisory (non-blocking, for follow-up)"]
        for item in advisories:
            lines.append(f"- `{item['key']}` ({item['reason']})")
            if item.get("finding"):
                finding = item["finding"]
                lines.append(f"  {finding['title']}: {finding['explanation']}")
    lines += [
        "",
        "## Options",
        "- Request a root-issued ticket restart allowance; reopening cannot raise stored caps.",
        "- Accept the advisories as follow-up tickets and merge if the blocking set is",
        "  actually empty.",
        "- Return the ticket to scoping: repeated strikes on one component usually mean",
        "  the acceptance criteria, not the code, are underspecified.",
    ]
    print("\n".join(lines))


def cmd_resolve(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        key = normalize_key(args.key)
        component = state["components"].get(key)
        if component is None:
            raise LedgerError(f"no such component on the ledger: {key}")
        component["status"] = "resolved"
        component["resolved_round"] = len(state["rounds"])
        component["resolved_by_gate"] = "manual"
        save(path, state)
        emit({"resolved": key, **decide(state)})


def cmd_redesign(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        key = normalize_key(args.key)
        component = state["components"].get(key)
        if component is None:
            raise LedgerError(f"no such component on the ledger: {key}")
        if args.verdict != "PASS":
            raise LedgerError(
                f"design gate returned {args.verdict} for {key}; "
                "redesign again before authorizing any implementation"
            )
        component["redesigned_at_strike"] = component["strikes"]
        component["redesigned_at_repair_failure"] = component.get("repair_failures", 0)
        component["last_redesign_round"] = len(state["rounds"])
        save(path, state)
        emit({"redesign_cleared": key, **decide(state)})


def cmd_alias(args: argparse.Namespace) -> None:
    """Merge a duplicate key into the canonical one so strikes accumulate."""
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        source = normalize_key(args.source)
        target = resolve_alias(state, normalize_key(args.target))
        if source == target:
            raise LedgerError("alias source and target normalize to the same key")
        if source not in state["components"]:
            raise LedgerError(f"no such component on the ledger: {source}")
        # Materialize legacy gate ownership before either component is removed
        # or combined. Old ledgers recorded `gates` without per-gate claims.
        _component(
            state,
            source,
            state["components"][source].get("display", args.source),
            int(state["components"][source].get("last_round", 1)),
        )
        merged = state["components"].pop(source)
        canonical = _component(state, target, args.target, merged["first_round"])
        canonical["strikes"] += merged["strikes"]
        canonical["rounds"] = sorted(set(canonical["rounds"] + merged["rounds"]))
        canonical["gates"] = sorted(set(canonical["gates"] + merged["gates"]))
        canonical_claims = canonical.setdefault("claims", {})
        merged_claims = merged.get("claims", {})
        for gate in canonical["gates"]:
            left = canonical_claims.get(gate)
            right = merged_claims.get(gate)
            if left is None and right is not None:
                canonical_claims[gate] = right
            elif left is not None and right is not None:
                canonical_claims[gate] = {
                    **left,
                    **right,
                    "status": "open"
                    if "open" in {left.get("status"), right.get("status")}
                    else "resolved",
                    "generation": max(
                        int(left.get("generation", 1)),
                        int(right.get("generation", 1)),
                    ),
                    "last_round": max(
                        int(left.get("last_round", 0)),
                        int(right.get("last_round", 0)),
                    ),
                }
        canonical["first_round"] = min(canonical["first_round"], merged["first_round"])
        if merged["status"] == "open":
            canonical["status"] = "open"
        state.setdefault("aliases", {})[source] = target
        for alias, destination in list(state["aliases"].items()):
            if alias != source and normalize_key(str(destination)) == source:
                state["aliases"][alias] = target
        save(path, state)
        emit(
            {
                "aliased": {source: target},
                "strikes": canonical["strikes"],
                **decide(state),
            }
        )


def cmd_escalate(args: argparse.Namespace) -> None:
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        state["escalated"] = True
        state["escalation_reason"] = args.reason
        save(path, state)
        emit({"escalated": True, "reason": args.reason, **decide(state)})


def cmd_authorize_repair(args: argparse.Namespace) -> None:
    """Consume root authority for a bounded post-escalation repair continuation."""
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        plan = decide(state)
        if not state.get("escalated") and not plan["cap_reached"]:
            raise LedgerError("review ledger has not reached a repair escalation")
        if state.get("repair_pending_review"):
            raise LedgerError("the current repaired head still requires review completion")
        if not plan["open_blocking"]:
            raise LedgerError("review ledger has no blocking findings to repair")
        try:
            grant = activate_review_repair(
                shared_repository_root(project_root()).resolve(),
                str(state["pr"]),
                operator_capability(args),
            )
        except AuthorityError as exc:
            raise LedgerError(str(exc)) from exc
        ceiling = int(grant["ceiling_repair_cycles"])
        if ceiling <= plan["fix_cycles"]:
            raise LedgerError(
                "review repair ceiling must exceed the completed repair-cycle count"
            )
        state.setdefault("operator_repair_grants", []).append(
            {
                **grant,
                "authorized_at": now(),
                "fix_cycles_at_authorization": plan["fix_cycles"],
            }
        )
        save(path, state)
        emit(
            {
                "repair_authorized": True,
                "grant_id": grant["grant_id"],
                "reason": grant["reason"],
                **decide(state),
            }
        )


def operator_capability(args: argparse.Namespace) -> str:
    if getattr(args, "operator_capability_stdin", False):
        value = sys.stdin.readline().strip()
        if not value:
            raise LedgerError("operator capability stdin was empty")
        return value
    return str(getattr(args, "operator_capability", ""))


def exact_repository_head(requested: str) -> str:
    try:
        actual = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_root(),
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .lower()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LedgerError("cannot bind review generation to repository HEAD") from exc
    if requested.lower() != actual:
        raise LedgerError("review generation head must exactly match the full repository HEAD")
    return actual


def cmd_rebind_generation(args: argparse.Namespace) -> None:
    """Start a new review generation for a non-repair commit on the same PR."""
    actual_head = exact_repository_head(args.head)
    reason = args.reason.strip()
    if not reason:
        raise LedgerError("review generation rebind reason must not be empty")
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        plan = decide(state)
        generation = int(state.get("review_generation", 1))
        current_entries = [
            entry
            for entry in state.get("rounds", [])
            if int(entry.get("generation", entry.get("round", 1))) == generation
        ]
        if state.get("repair_pending_review"):
            raise LedgerError("a repaired head is still awaiting its complete review set")
        if plan["open_blocking"]:
            raise LedgerError(
                "blocking findings require record-repair; a new-head generation cannot bypass them"
            )
        if plan["next_action"] == ACTION_ESCALATE:
            raise LedgerError("an escalated review ledger requires operator recovery")
        if not current_entries:
            raise LedgerError("the current review generation has no completed gate results")
        if plan["missing_gates"]:
            raise LedgerError(
                "the current review generation is incomplete; missing gates: "
                + ", ".join(plan["missing_gates"])
            )
        if not all(_round_is_authoritative(state, entry) for entry in current_entries):
            raise LedgerError("the current review generation lacks authoritative gate evidence")
        outstanding = [
            permit
            for permit in _current_generation_permits(state)
            if not permit.get("receipt_consumed_at")
        ]
        if outstanding:
            raise LedgerError("the current review generation has an outstanding phase permit")
        generation_heads = _current_generation_heads(state)
        if len(generation_heads) != 1:
            raise LedgerError("the current review generation lacks one exact head binding")
        prior_head = next(iter(generation_heads))
        if actual_head == prior_head:
            raise LedgerError("the requested head is already bound to the current generation")
        required_gates = sorted(
            set(plan["required_gates"])
            or {str(entry["gate"]) for entry in current_entries if entry.get("gate")}
        )
        if not required_gates:
            raise LedgerError("cannot derive the required gate set for the new generation")
        next_generation = generation + 1
        event = {
            "from_generation": generation,
            "to_generation": next_generation,
            "from_head": prior_head,
            "head": actual_head,
            "reason": reason,
            "required_gates": required_gates,
            "recorded_at": now(),
        }
        state.setdefault("generation_rebinds", []).append(event)
        state["review_generation"] = next_generation
        save(path, state)
        emit({"generation_rebound": event, **decide(state)})


def cmd_permit_review(args: argparse.Namespace) -> None:
    """Issue one phase capability when durable ledger state allows review."""
    try:
        assert_minimum_version(
            Path(__file__).resolve().parent.parent,
            canonical_config_path(project_root()),
            allow_missing_config=True,
        )
    except (RuntimeStateError, VersionPolicyError) as exc:
        raise LedgerError(f"review gate runtime is incompatible: {exc}") from exc
    actual_head = exact_repository_head(args.head)
    path = ledger_path(args)
    with locked(path):
        state = load(path)
        subject = state.get("work_subject")
        if not isinstance(subject, dict):
            raise LedgerError("review ledger has no immutable work subject")
        if args.role == "design-reviewer":
            if _design_plan(state)["next_action"] != "redesign":
                raise LedgerError(
                    "design ledger phase does not permit another reviewer"
                )
        elif decide(state)["next_action"] != ACTION_REVIEW:
            raise LedgerError("review ledger phase does not permit another reviewer")
        generation_heads = _current_generation_heads(state)
        if generation_heads and actual_head not in generation_heads:
            raise LedgerError(
                "the current review generation is already bound to another exact head"
            )
        gate = ROLE_GATES.get(args.role)
        if gate and any(
            entry.get("gate") == gate
            and int(entry.get("generation", entry.get("round", 1)))
            == int(state.get("review_generation", 1))
            for entry in state.get("rounds", [])
        ):
            raise LedgerError(
                "the current gate already recorded a result for this review generation"
            )
        active = [
            item
            for item in state.get("review_permits", [])
            if item.get("role") == args.role
            and item.get("head") == actual_head
            and item.get("review_generation", 1) == state.get("review_generation", 1)
            and not item.get("cancelled_at")
            and not item.get("superseded_at")
            and not item.get("receipt_consumed_at")
        ]
        reused = False
        if active:
            if len(active) != 1:
                raise LedgerError(
                    "the current gate has multiple outstanding phase permits"
                )
            existing = active[0]
            if existing.get("completion_receipt"):
                raise LedgerError(
                    "the current gate has a completed review awaiting ledger recording"
                )
            if existing.get("started_at"):
                raise LedgerError(
                    "the current gate has a started review requiring reconciliation"
                )
            # Issuance is idempotent until provider or desktop execution starts.
            # This lets a caller correct locally invalid structured output without
            # fabricating a second review or stranding the generation.
            token = str(existing["token"])
            reused = True
        else:
            token = "phase_" + os.urandom(24).hex()
            state.setdefault("review_permits", []).append(
                {
                    "token": token,
                    "work_subject": subject,
                    "role": args.role,
                    "head": actual_head,
                    "review_generation": state["review_generation"],
                    "issued_at": now(),
                    "round_count": len(state.get("rounds", [])),
                    "repair_count": len(state.get("repair_attempts", [])),
                    "design_round_count": len(
                        (state.get("design") or {}).get("rounds", [])
                    ),
                    "started_at": "",
                    "completion_receipt": "",
                    "receipt_consumed_at": "",
                }
            )
            save(path, state)
    emit(
        {
            "review_phase_permit": token,
            "work_subject": subject,
            "role": args.role,
            "head": actual_head,
            "review_phase_permit_reused": reused,
        }
    )


def cmd_complete_review(args: argparse.Namespace) -> None:
    """Atomically attest a native desktop review only after its result exists."""
    try:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
        gate = {
            "code-reviewer": "code-review",
            "security-reviewer": "security-review",
        }.get(args.role)
        if gate:
            context_pipeline.validate_review_output(result, gate)
    except (OSError, json.JSONDecodeError, context_pipeline.ContextError) as exc:
        raise LedgerError(
            "invalid completed review result: "
            f"{exc}; correct the result and retry with the same phase permit"
        ) from exc
    try:
        actual_head = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_root(),
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .lower()
        )
        root = shared_repository_root(project_root())
        receipt = complete_review_permit(
            shared_root=root,
            ledger_dir=str(ledger_path(args).parent.relative_to(root)),
            pr=args.pr,
            token=args.phase_permit,
            role=args.role,
            head=actual_head,
            result=result,
            timestamp=now(),
            desktop=True,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        ValueError,
        ReviewPermitError,
    ) as exc:
        raise LedgerError(str(exc)) from exc
    emit({"completion_receipt": receipt, "head": actual_head, "role": args.role})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config",
        help="repo orchestration config (default: .orchestration/config.yaml)",
    )
    result.add_argument("--ledger-dir", help="ledger directory override")
    commands = result.add_subparsers(dest="command", required=True)

    open_parser = commands.add_parser(
        "open", help="create or report the ledger for a PR"
    )
    open_parser.add_argument("pr")
    open_parser.add_argument(
        "--max-rounds", help="tighten (never raise) the configured repair cap"
    )
    open_parser.add_argument(
        "--max-design-rounds", help="tighten (never raise) the configured design cap"
    )
    open_parser.add_argument("--work-kind", choices=("jira", "pr", "design"))
    open_parser.add_argument("--work-id")
    open_parser.set_defaults(func=cmd_open)

    record_parser = commands.add_parser(
        "record", help="record one completed gate round"
    )
    record_parser.add_argument("pr")
    record_parser.add_argument("--gate", required=True)
    record_parser.add_argument(
        "--result", help="validated structured reviewer JSON file"
    )
    record_parser.add_argument("--verdict", choices=("PASS", "FAIL"))
    record_parser.add_argument(
        "--blocking",
        action="append",
        default=[],
        metavar="COMPONENT",
        help="a blocking finding's component key (repeatable)",
    )
    record_parser.add_argument(
        "--advisory",
        action="append",
        default=[],
        metavar="COMPONENT",
        help="a non-blocking finding's component key (repeatable)",
    )
    record_parser.add_argument(
        "--regression",
        action="append",
        default=[],
        metavar="COMPONENT",
        help="a new key that is a regression in the delta, so it keeps blocking authority",
    )
    record_parser.add_argument(
        "--head", help="exact reviewed commit; required after record-repair"
    )
    record_parser.add_argument(
        "--phase-permit", help="single-use permit with a completed review receipt"
    )
    record_parser.set_defaults(func=cmd_record)

    migrate_parser = commands.add_parser(
        "migrate-concurrent-review",
        help="restore initial blockers hidden by legacy concurrent gate ordering",
    )
    migrate_parser.add_argument("pr")
    migrate_parser.add_argument(
        "--reason", required=True, help="auditable explanation for the migration"
    )
    migrate_parser.set_defaults(func=cmd_migrate_concurrent_review)

    for name, func, helptext in (
        ("status", cmd_status, "emit the ledger state and the loop's next action"),
        ("brief", cmd_brief, "emit the round-aware contract for the next reviewer"),
        ("handoff", cmd_handoff, "render the human escalation report"),
        ("repair-brief", cmd_repair_brief, "emit one deduplicated repair contract"),
        ("metrics", cmd_metrics, "emit repair effectiveness metrics"),
        (
            "complete-repair-review",
            cmd_complete_repair_review,
            "close a repaired head after every required gate records",
        ),
        (
            "design-handoff",
            cmd_design_handoff,
            "render the design-round escalation report",
        ),
    ):
        command = commands.add_parser(name, help=helptext)
        command.add_argument("pr")
        command.set_defaults(func=func)

    repair_parser = commands.add_parser(
        "record-repair", help="record one complete repair report before re-review"
    )
    repair_parser.add_argument("pr")
    repair_parser.add_argument("--report", required=True)
    repair_parser.set_defaults(func=cmd_record_repair)

    rebind_parser = commands.add_parser(
        "rebind-generation",
        help="start a preserved new review generation for a non-repair PR head",
    )
    rebind_parser.add_argument("pr")
    rebind_parser.add_argument("--head", required=True)
    rebind_parser.add_argument("--reason", required=True)
    rebind_parser.set_defaults(func=cmd_rebind_generation)

    design_open = commands.add_parser(
        "design-open", help="create or report the pre-code design ledger"
    )
    design_open.add_argument("pr", help="ticket or change identifier")
    design_open.add_argument("--max-rounds")
    design_open.add_argument("--max-design-rounds")
    design_open.add_argument("--work-kind", choices=("jira", "pr", "design"))
    design_open.add_argument("--work-id")
    design_open.set_defaults(func=cmd_design_open)

    design_record = commands.add_parser(
        "design-record", help="record one pre-code design verdict"
    )
    design_record.add_argument("pr", help="ticket or change identifier")
    design_record.add_argument(
        "--result", help="machine-readable design evidence bound to source SHA"
    )
    design_record.add_argument("--verdict", choices=("PASS", "FAIL"))
    design_record.add_argument("--evidence")
    design_record.set_defaults(func=cmd_design_record)
    permit = commands.add_parser(
        "permit-review",
        help="issue a single-use permit for the ledger's current review phase",
    )
    permit.add_argument("pr")
    permit.add_argument(
        "--role",
        required=True,
        choices=("design-reviewer", "code-reviewer", "security-reviewer"),
    )
    permit.add_argument("--head", required=True)
    permit.set_defaults(func=cmd_permit_review)
    complete = commands.add_parser(
        "complete-review", help="complete a native review permit after output exists"
    )
    complete.add_argument("pr")
    complete.add_argument(
        "--role",
        required=True,
        choices=("design-reviewer", "code-reviewer", "security-reviewer"),
    )
    complete.add_argument("--phase-permit", required=True)
    complete.add_argument("--result", required=True)
    complete.set_defaults(func=cmd_complete_review)

    resolve_parser = commands.add_parser("resolve", help="manually close a component")
    resolve_parser.add_argument("pr")
    resolve_parser.add_argument("--key", required=True)
    resolve_parser.set_defaults(func=cmd_resolve)

    redesign_parser = commands.add_parser(
        "redesign", help="record a design-gate verdict for a component"
    )
    redesign_parser.add_argument("pr")
    redesign_parser.add_argument("--key", required=True)
    redesign_parser.add_argument("--verdict", required=True, choices=("PASS", "FAIL"))
    redesign_parser.set_defaults(func=cmd_redesign)

    alias_parser = commands.add_parser(
        "alias", help="merge a duplicate component key into the canonical one"
    )
    alias_parser.add_argument("pr")
    alias_parser.add_argument("--from", dest="source", required=True)
    alias_parser.add_argument("--to", dest="target", required=True)
    alias_parser.set_defaults(func=cmd_alias)

    escalate_parser = commands.add_parser(
        "escalate", help="stop the loop and hand the PR to a human"
    )
    escalate_parser.add_argument("pr")
    escalate_parser.add_argument("--reason", required=True)
    escalate_parser.set_defaults(func=cmd_escalate)

    authorize_repair_parser = commands.add_parser(
        "authorize-repair",
        help="consume root authority for bounded repair after human escalation",
    )
    authorize_repair_parser.add_argument("pr")
    repair_authority = authorize_repair_parser.add_mutually_exclusive_group(
        required=True
    )
    repair_authority.add_argument("--operator-capability", help=argparse.SUPPRESS)
    repair_authority.add_argument(
        "--operator-capability-stdin", action="store_true"
    )
    authorize_repair_parser.set_defaults(func=cmd_authorize_repair)

    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except (LedgerError, context_pipeline.ContextError) as exc:
        print(f"review-ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
