#!/usr/bin/env python3
"""Root-owned, file-backed authority for recovery, budget, and relaunch capabilities.

It also holds standing per-repository budget policies that raise Orka's compiled
hard caps. Only root sets or clears a policy; the runtime may only read one.

Runtime commands are intended to be exposed through a narrow sudoers rule.
Issuance and revocation commands require a real root invocation and are never
included in that rule.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_STATE = Path("/var/lib/orka-authority")


def test_mode() -> bool:
    # sudo sets the real uid to root. A production/root invocation must never
    # honor caller-controlled paths, even on a host with unusual env_keep rules.
    return os.getuid() != 0 and os.environ.get("ORCHESTRATION_AUTHORITY_TEST_MODE") == "1"


def state_root() -> Path:
    if test_mode():
        override = os.environ.get("ORCHESTRATION_AUTHORITY_STATE_DIR")
        if override:
            # Keep the final path component unresolved so ensure_layout can
            # reject a symlink instead of silently following it.
            return Path(os.path.abspath(override))
    return DEFAULT_STATE


def fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def canonical_scope(raw: str, expected_kind: str) -> str:
    if not raw or len(raw) > 4096 or "\n" in raw or "\r" in raw:
        raise ValueError("invalid authority scope")
    value = json.loads(raw)
    if expected_kind == "review-repair":
        expected = {"kind", "repository", "pr"}
    elif expected_kind == "budget-policy":
        expected = {"kind", "repository"}
    else:
        expected = {"kind", "repository", "ticket"}
        if expected_kind == "recovery":
            expected.add("attempt")
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("authority scope has unexpected fields")
    if value.get("kind") != expected_kind:
        raise ValueError("authority scope kind mismatch")
    repository = str(value.get("repository") or "")
    if expected_kind == "budget-policy":
        if not repository.startswith("/"):
            raise ValueError("budget policy scope is incomplete")
    elif expected_kind == "review-repair":
        if not repository.startswith("/") or not re.fullmatch(
            r"[1-9][0-9]{0,19}", str(value.get("pr") or "")
        ):
            raise ValueError("review repair scope is incomplete")
    else:
        ticket = str(value.get("ticket") or "")
        if (
            not repository.startswith("/")
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", ticket)
            or len(ticket) > 64
        ):
            raise ValueError("authority scope is incomplete")
    if expected_kind == "recovery":
        attempt = value.get("attempt")
        if not isinstance(attempt, int) or attempt < 0:
            raise ValueError("recovery attempt must be a non-negative integer")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if not hmac.compare_digest(canonical, raw):
        raise ValueError("authority scope must use canonical JSON")
    return canonical


def build_scope(kind: str, repository: str, ticket: str, attempt: int | None) -> str:
    root = Path(repository).resolve()
    if not root.is_dir():
        raise ValueError("repository does not exist")
    value: dict[str, Any] = {
        "kind": kind,
        "repository": str(root),
        "ticket": ticket.strip().upper(),
    }
    if kind == "recovery":
        if attempt is None or attempt < 0:
            raise ValueError("recovery attempt is required")
        value["attempt"] = attempt
    return canonical_scope(
        json.dumps(value, sort_keys=True, separators=(",", ":")), kind
    )


def build_review_repair_scope(repository: str, pr: str) -> str:
    root = Path(repository).resolve()
    if not root.is_dir():
        raise ValueError("repository does not exist")
    value = {
        "kind": "review-repair",
        "repository": str(root),
        "pr": str(pr).strip(),
    }
    return canonical_scope(
        json.dumps(value, sort_keys=True, separators=(",", ":")), "review-repair"
    )


def build_budget_policy_scope(repository: str) -> str:
    root = Path(repository).resolve()
    if not root.is_dir():
        raise ValueError("repository does not exist")
    value = {"kind": "budget-policy", "repository": str(root)}
    return canonical_scope(
        json.dumps(value, sort_keys=True, separators=(",", ":")), "budget-policy"
    )


# Standing policies may raise only Orka's compiled hard caps, never output-token
# or tool bounds. Dollars are decimal strings so no float rounds a ceiling.
POLICY_DOLLAR_KEYS = {
    "max_usd_per_run",
    "max_usd_per_ticket",
    "max_usd_per_sprint",
    "pause_usd_per_ticket",
    "max_usd_per_design_phase",
    "max_usd_per_implementation_phase",
    "max_usd_per_code_review_phase",
    "max_usd_per_security_review_phase",
    "max_usd_without_progress",
}
POLICY_COUNT_KEYS = {"max_model_runs_per_ticket", "max_reviewer_runs_per_ticket"}
POLICY_MAX_USD = Decimal("100000")
POLICY_MAX_COUNT = 1000


def budget_policy_caps(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("budget policy must be a non-empty JSON object of caps")
    unknown = set(value) - POLICY_DOLLAR_KEYS - POLICY_COUNT_KEYS
    if unknown:
        raise ValueError(f"budget policy has unsupported keys: {', '.join(sorted(unknown))}")
    caps: dict[str, Any] = {}
    for key, raw in value.items():
        if key in POLICY_COUNT_KEYS:
            if (
                not isinstance(raw, int)
                or isinstance(raw, bool)
                or not 0 < raw <= POLICY_MAX_COUNT
            ):
                raise ValueError(f"{key} must be an integer from 1 through {POLICY_MAX_COUNT}")
            caps[key] = raw
            continue
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be a decimal string")
        try:
            amount = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"{key} must be a decimal string") from exc
        if not amount.is_finite() or not 0 < amount <= POLICY_MAX_USD:
            raise ValueError(f"{key} must be greater than 0 and at most {POLICY_MAX_USD}")
        caps[key] = str(amount)
    return caps


def validate_directory(path: Path, *, private: bool = True) -> None:
    info = path.lstat()
    expected_uid = os.getuid() if test_mode() else 0
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != expected_uid
        or (private and info.st_mode & 0o077)
    ):
        raise PermissionError(f"authority path is not a private owned directory: {path}")


def ensure_layout(root: Path) -> None:
    if root.is_symlink():
        raise PermissionError("authority state root must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("pending", "active", "consumed", "policies"):
        path = root / name
        if path.is_symlink():
            raise PermissionError(f"authority path must not be a symlink: {path}")
        path.mkdir(mode=0o700, exist_ok=True)
    for path in (
        root,
        root / "pending",
        root / "active",
        root / "consumed",
        root / "policies",
    ):
        validate_directory(path, private=False)
        os.chmod(path, 0o700)
        validate_directory(path)


def require_layout(root: Path) -> None:
    for path in (root, root / "pending", root / "active", root / "consumed"):
        validate_directory(path)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".authority-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_record(path: Path) -> dict[str, Any]:
    info = path.lstat()
    expected_uid = os.getuid() if test_mode() else 0
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != expected_uid
        or info.st_mode & 0o077
    ):
        raise PermissionError("capability record is not a private owned regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid capability record")
    return value


def require_real_root() -> None:
    if os.getuid() != 0 and not test_mode():
        raise PermissionError("capabilities may only be issued or revoked by root")


def read_token() -> str:
    token = sys.stdin.readline().strip()
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise ValueError("invalid capability token")
    return token


def live(record: dict[str, Any]) -> bool:
    return float(record.get("expires_at", 0)) > time.time()


def record_ceiling(record: dict[str, Any]) -> Decimal:
    try:
        value = Decimal(str(record["ceiling_usd"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("budget capability has an invalid ceiling") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("budget capability has an invalid ceiling")
    return value


def record_attempt_ceiling(record: dict[str, Any]) -> int:
    value = record.get("ceiling_attempts")
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("relaunch capability has an invalid attempt ceiling")
    return value


def locked(root: Path):
    class Lock:
        def __enter__(self):
            self.handle = (root / ".lock").open("a+")
            os.chmod(root / ".lock", 0o600)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *_args):
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    return Lock()


RESTART_COUNTS = {"attempts", "model_runs", "review_runs", "design_rounds", "code_rounds", "security_rounds", "repair_cycles"}
RESTART_DOLLARS = {"ticket_usd", "design_usd", "implementation_usd", "code_review_usd", "security_review_usd", "progress_baseline_usd"}


def restart_allowances(value):
    if not isinstance(value, dict) or set(value) != RESTART_COUNTS | RESTART_DOLLARS:
        raise ValueError("restart requires exactly the documented count and dollar ceilings")
    for key in RESTART_COUNTS:
        if type(value[key]) is not int or not 0 < value[key] <= 10000:
            raise ValueError(f"invalid restart count: {key}")
    result = dict(value)
    for key in RESTART_DOLLARS:
        amount = Decimal(str(value[key]))
        if not amount.is_finite() or amount < 0 or (amount == 0 and key != "progress_baseline_usd"):
            raise ValueError(f"invalid restart amount: {key}")
        result[key] = str(amount)
    if Decimal(result["progress_baseline_usd"]) >= Decimal(result["ticket_usd"]):
        raise ValueError("restart baseline must be below the ticket ceiling")
    return result


def restart_grant(args, activate=False):
    scope = canonical_scope(args.scope, "restart")
    root = state_root()
    require_layout(root)
    target = root / "active" / (hashlib.sha256(scope.encode()).hexdigest() + ".json")
    token = read_token() if activate else None
    with locked(root):
        source = root / "pending" / f"{token}.json" if activate else target
        if not source.is_file():
            return fail("restart capability missing or consumed") if activate else 3
        record = load_record(source)
        if record.get("kind") != "restart" or not live(record) or not hmac.compare_digest(record.get("scope", ""), scope):
            return fail("restart capability invalid, expired or scope mismatch") if activate else 3
        restart_allowances(record["allowances"])
        if activate:
            atomic_json(target, record)
            os.replace(source, root / "consumed" / f"{token}.json")
    print(json.dumps({key: record[key] for key in ("grant_id", "allowances", "reason", "expires_at")}))
    return 0


def revoke_restart(args):
    require_real_root()
    scope = build_scope("restart", args.repository, args.ticket, None)
    root = state_root()
    ensure_layout(root)
    with locked(root):
        (root / "active" / (hashlib.sha256(scope.encode()).hexdigest() + ".json")).unlink(missing_ok=True)
    return 0


def issue(args: argparse.Namespace, kind: str) -> int:
    require_real_root()
    if not 0 < args.expires_hours <= 168:
        raise ValueError("capability expiry must be greater than zero and at most 168 hours")
    scope = build_scope(kind, args.repository, args.ticket, getattr(args, "attempt", None))
    ceiling = None
    if kind == "budget":
        try:
            ceiling = Decimal(args.ceiling_usd)
        except InvalidOperation as exc:
            raise ValueError("ceiling must be a decimal number") from exc
        if not ceiling.is_finite() or ceiling <= 0:
            raise ValueError("ceiling must be positive")
    attempt_ceiling = None
    if kind == "relaunch":
        attempt_ceiling = args.ceiling_attempts
        if attempt_ceiling <= 0:
            raise ValueError("attempt ceiling must be positive")
    allowances = None
    if kind == "restart":
        allowances = restart_allowances(json.loads(Path(args.allowances).read_text()))
        if not args.reason.strip() or len(args.reason) > 2000:
            raise ValueError("restart requires a bounded operator reason")
    token = secrets.token_hex(32)
    root = state_root()
    ensure_layout(root)
    record = {
        "kind": kind,
        "scope": scope,
        "issued_at": time.time(),
        "expires_at": time.time() + args.expires_hours * 3600,
    }
    if allowances is not None:
        record.update(allowances=allowances, reason=args.reason.strip(), grant_id=secrets.token_hex(16))
    if ceiling is not None:
        record["ceiling_usd"] = str(ceiling)
    if attempt_ceiling is not None:
        record["ceiling_attempts"] = attempt_ceiling
    with locked(root):
        atomic_json(root / "pending" / f"{token}.json", record)
    print(token)
    return 0


def consume_recovery(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "recovery")
    token = read_token()
    root = state_root()
    require_layout(root)
    source = root / "pending" / f"{token}.json"
    with locked(root):
        if not source.is_file():
            return fail("capability is missing or already consumed")
        record = load_record(source)
        if record.get("kind") != "recovery" or not live(record):
            return fail("recovery capability is invalid or expired")
        if not hmac.compare_digest(str(record.get("scope") or ""), scope):
            return fail("recovery capability scope mismatch")
        os.replace(source, root / "consumed" / f"{token}.json")
    return 0


def issue_review_repair(args: argparse.Namespace) -> int:
    require_real_root()
    if not 0 < args.expires_hours <= 168:
        raise ValueError("capability expiry must be greater than zero and at most 168 hours")
    if not 0 < args.ceiling_repair_cycles <= 10000:
        raise ValueError("review repair ceiling must be between 1 and 10000")
    if not args.reason.strip() or len(args.reason) > 2000:
        raise ValueError("review repair authorization requires a bounded operator reason")
    scope = build_review_repair_scope(args.repository, args.pr)
    token = secrets.token_hex(32)
    root = state_root()
    ensure_layout(root)
    record = {
        "kind": "review-repair",
        "scope": scope,
        "ceiling_repair_cycles": args.ceiling_repair_cycles,
        "reason": args.reason.strip(),
        "grant_id": secrets.token_hex(16),
        "issued_at": time.time(),
        "expires_at": time.time() + args.expires_hours * 3600,
    }
    with locked(root):
        atomic_json(root / "pending" / f"{token}.json", record)
    print(token)
    return 0


def review_repair_grant(args: argparse.Namespace, activate: bool = False) -> int:
    scope = canonical_scope(args.scope, "review-repair")
    token = read_token() if activate else None
    root = state_root()
    require_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        source = root / "pending" / f"{token}.json" if activate else target
        if not source.is_file():
            return fail("review repair capability missing or consumed") if activate else 3
        record = load_record(source)
        if record.get("kind") != "review-repair" or not live(record):
            return fail("review repair capability is invalid or expired") if activate else 3
        if not hmac.compare_digest(str(record.get("scope") or ""), scope):
            return fail("review repair capability scope mismatch") if activate else 3
        ceiling = record.get("ceiling_repair_cycles")
        if type(ceiling) is not int or not 0 < ceiling <= 10000:
            return fail("review repair capability has an invalid ceiling") if activate else 3
        if activate:
            if target.is_file():
                current = load_record(target)
                current_ceiling = current.get("ceiling_repair_cycles")
                if (
                    current.get("kind") == "review-repair"
                    and live(current)
                    and type(current_ceiling) is int
                    and current_ceiling > ceiling
                ):
                    record = current
            atomic_json(target, record)
            os.replace(source, root / "consumed" / f"{token}.json")
    print(
        json.dumps(
            {
                key: record[key]
                for key in (
                    "grant_id",
                    "ceiling_repair_cycles",
                    "reason",
                    "issued_at",
                    "expires_at",
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def revoke_review_repair(args: argparse.Namespace) -> int:
    require_real_root()
    scope = build_review_repair_scope(args.repository, args.pr)
    root = state_root()
    ensure_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    with locked(root):
        (root / "active" / f"{key}.json").unlink(missing_ok=True)
    return 0


def activate_budget(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "budget")
    token = read_token()
    root = state_root()
    require_layout(root)
    source = root / "pending" / f"{token}.json"
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        if not source.is_file():
            return fail("capability is missing or already consumed")
        record = load_record(source)
        if record.get("kind") != "budget" or not live(record):
            return fail("budget capability is invalid or expired")
        if not hmac.compare_digest(str(record.get("scope") or ""), scope):
            return fail("budget capability scope mismatch")
        if target.is_file():
            current = load_record(target)
            if live(current) and record_ceiling(current) > record_ceiling(record):
                record = current
        record_ceiling(record)
        atomic_json(target, record)
        os.replace(source, root / "consumed" / f"{token}.json")
    print(record["ceiling_usd"])
    return 0


def budget_ceiling(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "budget")
    root = state_root()
    require_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        if not target.is_file():
            return 3
        record = load_record(target)
        if (
            record.get("kind") != "budget"
            or not live(record)
            or not hmac.compare_digest(str(record.get("scope") or ""), scope)
        ):
            return 3
        record_ceiling(record)
    print(record["ceiling_usd"])
    return 0


def activate_relaunch(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "relaunch")
    token = read_token()
    root = state_root()
    require_layout(root)
    source = root / "pending" / f"{token}.json"
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        if not source.is_file():
            return fail("capability is missing or already consumed")
        record = load_record(source)
        if record.get("kind") != "relaunch" or not live(record):
            return fail("relaunch capability is invalid or expired")
        if not hmac.compare_digest(str(record.get("scope") or ""), scope):
            return fail("relaunch capability scope mismatch")
        if target.is_file():
            current = load_record(target)
            if (
                live(current)
                and record_attempt_ceiling(current) > record_attempt_ceiling(record)
            ):
                record = current
        record_attempt_ceiling(record)
        atomic_json(target, record)
        os.replace(source, root / "consumed" / f"{token}.json")
    print(record["ceiling_attempts"])
    return 0


def relaunch_ceiling(args: argparse.Namespace) -> int:
    scope = canonical_scope(args.scope, "relaunch")
    root = state_root()
    require_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    target = root / "active" / f"{key}.json"
    with locked(root):
        if not target.is_file():
            return 3
        record = load_record(target)
        if (
            record.get("kind") != "relaunch"
            or not live(record)
            or not hmac.compare_digest(str(record.get("scope") or ""), scope)
        ):
            return 3
        record_attempt_ceiling(record)
    print(record["ceiling_attempts"])
    return 0


def policy_path(root: Path, scope: str) -> Path:
    return root / "policies" / f"{hashlib.sha256(scope.encode('utf-8')).hexdigest()}.json"


def set_budget_policy(args: argparse.Namespace) -> int:
    require_real_root()
    scope = build_budget_policy_scope(args.repository)
    reason = args.reason.strip()
    if not reason or len(reason) > 2000:
        raise ValueError("budget policy requires a bounded operator reason")
    caps = budget_policy_caps(json.loads(Path(args.policy).read_text(encoding="utf-8")))
    root = state_root()
    ensure_layout(root)
    record = {
        "kind": "budget-policy",
        "scope": scope,
        "caps": caps,
        "reason": reason,
        "issued_at": time.time(),
        "policy_id": secrets.token_hex(16),
    }
    with locked(root):
        atomic_json(policy_path(root, scope), record)
    print(record["policy_id"])
    return 0


def clear_budget_policy(args: argparse.Namespace) -> int:
    require_real_root()
    scope = build_budget_policy_scope(args.repository)
    root = state_root()
    ensure_layout(root)
    with locked(root):
        policy_path(root, scope).unlink(missing_ok=True)
    return 0


def read_budget_policy(scope: str) -> dict[str, Any] | None:
    root = state_root()
    # A state root that was never created, or an install that predates
    # policies, holds no policy; any existing layout must still be private.
    if not root.exists() and not root.is_symlink():
        return None
    require_layout(root)
    if not (root / "policies").exists():
        return None
    validate_directory(root / "policies")
    target = policy_path(root, scope)
    with locked(root):
        if not target.is_file():
            return None
        record = load_record(target)
    if record.get("kind") != "budget-policy" or not hmac.compare_digest(
        str(record.get("scope") or ""), scope
    ):
        return None
    return {
        "policy_id": str(record.get("policy_id") or ""),
        "caps": budget_policy_caps(record.get("caps")),
        "reason": str(record.get("reason") or ""),
        "issued_at": record.get("issued_at"),
    }


def budget_policy(args: argparse.Namespace) -> int:
    policy = read_budget_policy(canonical_scope(args.scope, "budget-policy"))
    if policy is None:
        return 3
    print(json.dumps(policy, sort_keys=True, separators=(",", ":")))
    return 0


def show_budget_policy(args: argparse.Namespace) -> int:
    require_real_root()
    policy = read_budget_policy(build_budget_policy_scope(args.repository))
    if policy is None:
        return fail("no budget policy is set for this repository", 3)
    print(json.dumps(policy, indent=2, sort_keys=True))
    return 0


def revoke_budget(args: argparse.Namespace) -> int:
    require_real_root()
    scope = build_scope("budget", args.repository, args.ticket, None)
    root = state_root()
    ensure_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    with locked(root):
        (root / "active" / f"{key}.json").unlink(missing_ok=True)
    return 0


def revoke_relaunch(args: argparse.Namespace) -> int:
    require_real_root()
    scope = build_scope("relaunch", args.repository, args.ticket, None)
    root = state_root()
    ensure_layout(root)
    key = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    with locked(root):
        (root / "active" / f"{key}.json").unlink(missing_ok=True)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in (
        "consume-recovery",
        "activate-review-repair",
        "review-repair-grant",
        "activate-budget",
        "budget-ceiling",
        "activate-relaunch",
        "relaunch-ceiling",
        "activate-restart",
        "restart-grant",
        "budget-policy",
    ):
        command = commands.add_parser(name)
        command.add_argument("--scope", required=True)
    set_policy = commands.add_parser("set-budget-policy")
    set_policy.add_argument("--repository", required=True)
    set_policy.add_argument(
        "--policy", required=True, help="JSON object of raised hard caps"
    )
    set_policy.add_argument("--reason", required=True)
    for name in ("clear-budget-policy", "show-budget-policy"):
        command = commands.add_parser(name)
        command.add_argument("--repository", required=True)
    recovery = commands.add_parser("issue-recovery")
    recovery.add_argument("--repository", required=True)
    recovery.add_argument("--ticket", required=True)
    recovery.add_argument("--attempt", required=True, type=int)
    recovery.add_argument("--expires-hours", type=float, default=24)
    review_repair = commands.add_parser("issue-review-repair")
    review_repair.add_argument("--repository", required=True)
    review_repair.add_argument("--pr", required=True)
    review_repair.add_argument("--ceiling-repair-cycles", required=True, type=int)
    review_repair.add_argument("--reason", required=True)
    review_repair.add_argument("--expires-hours", type=float, default=24)
    revoke_review_repair_parser = commands.add_parser("revoke-review-repair")
    revoke_review_repair_parser.add_argument("--repository", required=True)
    revoke_review_repair_parser.add_argument("--pr", required=True)
    budget = commands.add_parser("issue-budget")
    budget.add_argument("--repository", required=True)
    budget.add_argument("--ticket", required=True)
    budget.add_argument("--ceiling-usd", required=True)
    budget.add_argument("--expires-hours", type=float, default=24)
    relaunch = commands.add_parser("issue-relaunch")
    relaunch.add_argument("--repository", required=True)
    relaunch.add_argument("--ticket", required=True)
    relaunch.add_argument("--ceiling-attempts", required=True, type=int)
    relaunch.add_argument("--expires-hours", type=float, default=24)
    revoke = commands.add_parser("revoke-budget")
    revoke.add_argument("--repository", required=True)
    revoke.add_argument("--ticket", required=True)
    revoke_relaunch_parser = commands.add_parser("revoke-relaunch")
    revoke_relaunch_parser.add_argument("--repository", required=True)
    revoke_relaunch_parser.add_argument("--ticket", required=True)
    restart = commands.add_parser("issue-restart")
    restart.add_argument("--repository", required=True)
    restart.add_argument("--ticket", required=True)
    restart.add_argument("--allowances", required=True, help="JSON file with absolute ceilings and progress baseline")
    restart.add_argument("--reason", required=True)
    restart.add_argument("--expires-hours", type=float, default=24)
    revoke_restart_parser = commands.add_parser("revoke-restart")
    revoke_restart_parser.add_argument("--repository", required=True)
    revoke_restart_parser.add_argument("--ticket", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "set-budget-policy":
            return set_budget_policy(args)
        if args.command == "clear-budget-policy":
            return clear_budget_policy(args)
        if args.command == "show-budget-policy":
            return show_budget_policy(args)
        if args.command == "budget-policy":
            return budget_policy(args)
        if args.command == "issue-restart":
            return issue(args, "restart")
        if args.command == "activate-restart":
            return restart_grant(args, activate=True)
        if args.command == "restart-grant":
            return restart_grant(args)
        if args.command == "revoke-restart":
            return revoke_restart(args)
        if args.command == "issue-recovery":
            return issue(args, "recovery")
        if args.command == "issue-review-repair":
            return issue_review_repair(args)
        if args.command == "issue-budget":
            return issue(args, "budget")
        if args.command == "issue-relaunch":
            return issue(args, "relaunch")
        if args.command == "consume-recovery":
            return consume_recovery(args)
        if args.command == "activate-review-repair":
            return review_repair_grant(args, activate=True)
        if args.command == "review-repair-grant":
            return review_repair_grant(args)
        if args.command == "revoke-review-repair":
            return revoke_review_repair(args)
        if args.command == "activate-budget":
            return activate_budget(args)
        if args.command == "budget-ceiling":
            return budget_ceiling(args)
        if args.command == "activate-relaunch":
            return activate_relaunch(args)
        if args.command == "relaunch-ceiling":
            return relaunch_ceiling(args)
        if args.command == "revoke-budget":
            return revoke_budget(args)
        if args.command == "revoke-relaunch":
            return revoke_relaunch(args)
    except (OSError, ValueError, PermissionError, InvalidOperation, json.JSONDecodeError) as exc:
        return fail(str(exc))
    return fail("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
