#!/usr/bin/env python3
"""Single-use phase permits backed by the durable review ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class ReviewPermitError(RuntimeError):
    pass


def safe_pr(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-")
    if not result:
        raise ReviewPermitError("review permit requires a valid PR or design-ledger id")
    return result


def subject_ledger_candidates(directory: Path, repository: str, pr: str) -> list[Path]:
    """Find ledgers by their immutable PR binding, never by a subject slug."""
    candidates: list[Path] = []
    for candidate in directory.glob("subject-*.json") if directory.exists() else []:
        try:
            state = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        subject = state.get("work_subject")
        if (
            str(state.get("pr")) == str(pr)
            and isinstance(subject, dict)
            and subject.get("repository") == repository
            and isinstance(subject.get("kind"), str)
            and isinstance(subject.get("id"), str)
        ):
            candidates.append(candidate)
    return candidates


def ledger_path(shared_root: Path, ledger_dir: str, pr: str) -> Path:
    relative = Path(ledger_dir)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReviewPermitError(
            "review ledger directory must stay in the shared repository"
        )
    directory = shared_root / relative
    repository = str(shared_root.resolve())
    legacy = directory / f"pr-{safe_pr(pr)}.json"
    candidates = subject_ledger_candidates(directory, repository, pr)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ReviewPermitError("review subject is ambiguous")
    if legacy.exists():
        try:
            state = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewPermitError(f"cannot read legacy review ledger: {exc}") from exc
        subject = state.get("work_subject")
        if not (
            str(state.get("pr")) == str(pr)
            and isinstance(subject, dict)
            and subject.get("repository") == repository
        ):
            raise ReviewPermitError(
                "legacy review ledger subject is ambiguous; migrate it explicitly"
            )
    return legacy


def consume(
    *,
    shared_root: Path,
    ledger_dir: str,
    pr: str,
    token: str,
    role: str,
    head: str,
    timestamp: str,
) -> str:
    path = ledger_path(shared_root, ledger_dir, pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if not path.is_file():
        raise ReviewPermitError("review phase permit ledger does not exist")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        permit = next(
            (
                item
                for item in state.get("review_permits", [])
                if item.get("token") == token
            ),
            None,
        )
        if (
            not permit
            or permit.get("started_at")
            or permit.get("completion_receipt")
            or permit.get("cancelled_at")
            or permit.get("superseded_at")
            or permit.get("receipt_consumed_at")
        ):
            raise ReviewPermitError("review phase permit is missing or already started")
        expected = {
            "work_subject": state.get("work_subject"),
            "role": role,
            "head": head.lower(),
        }
        if any(permit.get(key) != value for key, value in expected.items()):
            raise ReviewPermitError(
                "review phase permit does not match work subject, role, and exact head"
            )
        if permit.get("review_generation", 1) != state.get("review_generation", 1):
            raise ReviewPermitError("review phase changed after this permit was issued")
        permit["started_at"] = timestamp
        _save(path, state)
        return canonical_digest({key: permit.get(key) for key in (
            "work_subject", "role", "head", "review_generation", "design_round_count")})


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _save(path: Path, state: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def complete(
    *,
    shared_root: Path,
    ledger_dir: str,
    pr: str,
    token: str,
    role: str,
    head: str,
    result: Any,
    timestamp: str,
    desktop: bool = False,
) -> str:
    """Create a digest-bound completion receipt after successful review output."""
    return complete_with_status(
        shared_root=shared_root,
        ledger_dir=ledger_dir,
        pr=pr,
        token=token,
        role=role,
        head=head,
        result=result,
        timestamp=timestamp,
        desktop=desktop,
    )[0]


def complete_with_status(
    *,
    shared_root: Path,
    ledger_dir: str,
    pr: str,
    token: str,
    role: str,
    head: str,
    result: Any,
    timestamp: str,
    desktop: bool = False,
) -> tuple[str, bool]:
    """Complete a permit, returning its receipt and whether it already existed.

    API reviewers must have started their permit first. A desktop reviewer uses
    this controller-owned atomic transition to start and complete the same
    single permit after its structured result exists. The API runner completes
    its own permit, so a repeated completion for the same role, head, and
    identical result digest returns that unconsumed receipt instead of failing.
    """
    path = ledger_path(shared_root, ledger_dir, pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    digest = canonical_digest(result)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        permits = state.get("review_permits", [])
        permit = next((item for item in permits if item.get("token") == token), None)
        if not permit:
            raise ReviewPermitError("review phase permit is missing")
        if permit.get("cancelled_at") or permit.get("superseded_at"):
            raise ReviewPermitError(
                "review phase permit was cancelled or superseded and cannot complete"
            )
        if permit.get("receipt_consumed_at"):
            raise ReviewPermitError(
                "review phase permit completion was already recorded in the ledger"
            )
        expected = {
            "work_subject": state.get("work_subject"),
            "role": role,
            "head": head.lower(),
        }
        if any(permit.get(key) != value for key, value in expected.items()):
            raise ReviewPermitError(
                "review phase permit does not match work subject, role, and exact head"
            )
        if permit.get("review_generation", 1) != state.get("review_generation", 1):
            raise ReviewPermitError("review phase changed after this permit was issued")
        if permit.get("completion_receipt"):
            if permit.get("result_sha256") != digest:
                raise ReviewPermitError(
                    "review phase permit already completed with a different result "
                    "digest; record the result the reviewer originally completed"
                )
            return str(permit["completion_receipt"]), True
        if not permit.get("started_at"):
            if not desktop:
                raise ReviewPermitError(
                    "API review permit was not started by the provider runner"
                )
            permit["started_at"] = timestamp
            permit["execution"] = "desktop"
        receipt = "receipt_" + os.urandom(24).hex()
        permit.update(
            {
                "completed_at": timestamp,
                "completion_receipt": receipt,
                "result_sha256": digest,
                "receipt_consumed_at": "",
            }
        )
        _save(path, state)
    return receipt, False


def cancel_started(
    *,
    shared_root: Path,
    ledger_dir: str,
    pr: str,
    token: str,
    role: str,
    head: str,
    timestamp: str,
) -> None:
    """Release a started permit after a known nonexecuted or terminal failed run.

    The runner owns outcome classification. Uncertain or live provider work
    must never use this transition; no PASS receipt is created by cancellation.
    """
    path = ledger_path(shared_root, ledger_dir, pr)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        permit = next(
            (
                item
                for item in state.get("review_permits", [])
                if item.get("token") == token
            ),
            None,
        )
        expected = {
            "work_subject": state.get("work_subject"),
            "role": role,
            "head": head.lower(),
        }
        if (
            not permit
            or any(permit.get(key) != value for key, value in expected.items())
            or not permit.get("started_at")
            or permit.get("completion_receipt")
            or permit.get("cancelled_at")
            or permit.get("superseded_at")
        ):
            raise ReviewPermitError(
                "only a started, incomplete matching permit can be cancelled"
            )
        permit["cancelled_at"] = timestamp
        permit["receipt_consumed_at"] = timestamp
        _save(path, state)


def consume_completion(
    state: dict[str, Any],
    *,
    token: str,
    role: str,
    head: str,
    result: Any,
    timestamp: str,
) -> bool:
    digest = canonical_digest(result)
    permit = next(
        (
            item
            for item in state.get("review_permits", [])
            if item.get("token") == token
            and item.get("role") == role
            and item.get("head") == head.lower()
        ),
        None,
    )
    if (
        not permit
        or not permit.get("completion_receipt")
        or permit.get("receipt_consumed_at")
        or permit.get("cancelled_at")
        or permit.get("superseded_at")
        or permit.get("review_generation", 1) != state.get("review_generation", 1)
        or permit.get("result_sha256") != digest
    ):
        return False
    permit["receipt_consumed_at"] = timestamp
    return True


def consumed_permit(state: dict[str, Any], token: str, *, role: str, head: str) -> bool:
    return any(
        item.get("token") == token
        and item.get("role") == role
        and item.get("head") == head.lower()
        and item.get("completion_receipt")
        and not item.get("cancelled_at")
        and not item.get("superseded_at")
        for item in state.get("review_permits", [])
    )
