#!/usr/bin/env python3
"""Bridge controller decisions to the separately owned host authority."""

from __future__ import annotations

import json
import os
import re
import time
import stat
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_HELPER = Path("/usr/local/libexec/orchestration-recovery-authority")


class AuthorityError(RuntimeError):
    pass


def _scope(kind: str, repository: Path, ticket: str, attempt: int | None = None) -> str:
    value: dict[str, Any] = {
        "kind": kind,
        "repository": str(repository.resolve()),
        "ticket": ticket,
    }
    if attempt is not None:
        value["attempt"] = attempt
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _review_repair_scope(repository: Path, pr: str) -> str:
    return json.dumps(
        {
            "kind": "review-repair",
            "repository": str(repository.resolve()),
            "pr": str(pr),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _helper() -> tuple[Path, bool]:
    test_helper = os.environ.get("ORCHESTRATION_TEST_AUTHORITY_HELPER")
    if os.environ.get("ORCHESTRATION_TEST_MODE") == "1" and test_helper:
        helper = Path(test_helper).resolve()
        if not helper.is_file():
            raise AuthorityError("test authority helper is missing")
        return helper, True

    helper = DEFAULT_HELPER
    try:
        info = helper.lstat()
    except FileNotFoundError:
        raise AuthorityError("host operator authority is not installed") from None
    except OSError as exc:
        raise AuthorityError(f"cannot inspect host operator authority: {exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
    ):
        raise AuthorityError(
            "host operator authority must be a root-owned, non-writable regular file"
        )
    return helper, False


def _call(
    command: str,
    scope: str,
    *,
    token: str = "",
    no_authority_ok: bool = False,
) -> str | None:
    try:
        helper, test_mode = _helper()
    except AuthorityError as exc:
        if no_authority_ok and "not installed" in str(exc):
            return None
        raise
    argv = [str(helper), command, "--scope", scope]
    if not test_mode:
        argv = ["sudo", "-n", *argv]
    try:
        result = subprocess.run(
            argv,
            input=(token + "\n") if token else None,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorityError(f"host operator authority failed: {exc}") from exc
    if result.returncode == 3 and command in {
        "budget-ceiling",
        "relaunch-ceiling",
        "restart-grant",
        "review-repair-grant",
    }:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or "request denied"
        raise AuthorityError(f"host operator authority denied {command}: {detail}")
    return result.stdout.strip()


def budget_ceiling(repository: Path, ticket: str) -> Decimal | None:
    raw = _call(
        "budget-ceiling",
        _scope("budget", repository, ticket),
        no_authority_ok=True,
    )
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise AuthorityError("host authority returned an invalid budget ceiling") from exc
    if not value.is_finite() or value <= 0:
        raise AuthorityError("host authority returned a non-positive budget ceiling")
    return value


def activate_budget(repository: Path, ticket: str, token: str) -> Decimal:
    if not token.strip():
        raise AuthorityError("budget capability must not be empty")
    raw = _call(
        "activate-budget",
        _scope("budget", repository, ticket),
        token=token.strip(),
    )
    try:
        value = Decimal(raw or "")
    except InvalidOperation as exc:
        raise AuthorityError("host authority returned an invalid budget ceiling") from exc
    if not value.is_finite() or value <= 0:
        raise AuthorityError("host authority returned a non-positive budget ceiling")
    return value


def relaunch_ceiling(repository: Path, ticket: str) -> int | None:
    raw = _call(
        "relaunch-ceiling",
        _scope("relaunch", repository, ticket),
        no_authority_ok=True,
    )
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise AuthorityError("host authority returned an invalid attempt ceiling") from exc
    if value <= 0:
        raise AuthorityError("host authority returned a non-positive attempt ceiling")
    return value


def activate_relaunch(repository: Path, ticket: str, token: str) -> int:
    if not token.strip():
        raise AuthorityError("relaunch capability must not be empty")
    raw = _call(
        "activate-relaunch",
        _scope("relaunch", repository, ticket),
        token=token.strip(),
    )
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise AuthorityError("host authority returned an invalid attempt ceiling") from exc
    if value <= 0:
        raise AuthorityError("host authority returned a non-positive attempt ceiling")
    return value


def consume_recovery(
    repository: Path, ticket: str, attempt: int, token: str
) -> None:
    if not token.strip():
        raise AuthorityError("recovery capability must not be empty")
    _call(
        "consume-recovery",
        _scope("recovery", repository, ticket, attempt),
        token=token.strip(),
    )


def _validated_review_repair(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "")
        expected = {
            "grant_id",
            "ceiling_repair_cycles",
            "reason",
            "issued_at",
            "expires_at",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid review repair grant")
        ceiling = value["ceiling_repair_cycles"]
        if type(ceiling) is not int or not 0 < ceiling <= 10000:
            raise ValueError("invalid review repair ceiling")
        if (
            not value["grant_id"]
            or not value["reason"]
            or float(value["expires_at"]) <= time.time()
        ):
            raise ValueError("invalid review repair grant")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise AuthorityError("host authority returned an invalid review repair grant") from exc
    return value


def activate_review_repair(repository: Path, pr: str, token: str) -> dict[str, Any]:
    if not token.strip():
        raise AuthorityError("review repair capability must not be empty")
    return _validated_review_repair(
        _call(
            "activate-review-repair",
            _review_repair_scope(repository, pr),
            token=token.strip(),
        )
    )


def review_repair_grant(repository: Path, pr: str) -> dict[str, Any] | None:
    raw = _call(
        "review-repair-grant",
        _review_repair_scope(repository, pr),
        no_authority_ok=True,
    )
    return _validated_review_repair(raw) if raw is not None else None


def restart_grant(repository: Path, ticket: str, token: str = "") -> dict[str, Any] | None:
    """Only a live host-owned grant can relax restart ceilings."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", ticket):
        return None
    raw = _call("activate-restart" if token else "restart-grant",
                _scope("restart", repository, ticket), token=token, no_authority_ok=not token)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        counts = {"attempts", "model_runs", "review_runs", "design_rounds", "code_rounds", "security_rounds", "repair_cycles"}
        dollars = {"ticket_usd", "design_usd", "implementation_usd", "code_review_usd", "security_review_usd", "progress_baseline_usd"}
        limits = value["allowances"]
        if not (set(limits) == counts | dollars):
            raise ValueError("invalid restart allowance")
        if not (all(type(limits[k]) is int and 0 < limits[k] <= 10000 for k in counts)):
            raise ValueError("invalid restart allowance")
        if not (all(Decimal(str(limits[k])).is_finite() and Decimal(str(limits[k])) >= 0 for k in dollars)):
            raise ValueError("invalid restart allowance")
        if not (all(Decimal(str(limits[k])) > 0 for k in dollars - {"progress_baseline_usd"})):
            raise ValueError("invalid restart allowance")
        if not (Decimal(str(limits["progress_baseline_usd"])) < Decimal(str(limits["ticket_usd"]))):
            raise ValueError("invalid restart allowance")
        if not (value["grant_id"] and value["reason"] and float(value["expires_at"]) > time.time()):
            raise ValueError("invalid restart allowance")
    except (ValueError, KeyError, TypeError, AssertionError, InvalidOperation) as exc:
        raise AuthorityError("host authority returned an invalid restart grant") from exc
    return value
