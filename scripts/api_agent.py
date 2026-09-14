#!/usr/bin/env python3
"""Run one constrained orchestration agent through a configured API provider.

The runner owns provider submission, client tool loops, durable request markers,
worst-case budget reservations, and actual usage accounting. HTTP providers use
the standard library; Bedrock uses boto3 and the ambient AWS credential chain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import context_pipeline
from attempt_capability import (
    AttemptCapabilityError,
    validate as validate_attempt_capability,
)
from operator_authority import (
    AuthorityError,
    budget_ceiling as authorized_budget_ceiling,
    restart_grant as authorized_restart_grant,
)
from review_permit import (
    ReviewPermitError,
    cancel_started as cancel_review_permit,
    complete as complete_review_permit,
    consume as consume_review_permit,
)
from runtime_state import (
    RuntimeStateError,
    canonical_config_path,
    migrate_legacy_runtime_dir,
    shared_repository_root as resolve_shared_repository_root,
)


MILLION = Decimal("1000000")
TOOL_NAMES = {
    "read_file",
    "search",
    "git_diff",
    "git_status",
    "run_check",
    "apply_patch",
}
READ_TOOLS = {"read_file", "search", "git_diff", "git_status", "run_check"}
SCOPING_TOOLS = {"read_file", "search", "git_status"}
OPENAI_CACHE_REQUEST_FIELDS = {
    "prompt_cache_key",
    "prompt_cache_options",
    "prompt_cache_breakpoint",
    "prompt_cache_retention",
}


def strip_openai_cache_request_fields(value: Any) -> None:
    """Remove optional cache controls rejected by some OpenAI-compatible routes."""
    if isinstance(value, dict):
        for key in tuple(value):
            if key in OPENAI_CACHE_REQUEST_FIELDS:
                value.pop(key, None)
            else:
                strip_openai_cache_request_fields(value[key])
    elif isinstance(value, list):
        for item in value:
            strip_openai_cache_request_fields(item)


ROLE_TOOL_CEILINGS = {
    "ticket-scoper": SCOPING_TOOLS,
    "design-reviewer": READ_TOOLS,
    "code-reviewer": READ_TOOLS,
    "security-reviewer": READ_TOOLS,
    "implementer": TOOL_NAMES,
    "sprint-worker": TOOL_NAMES,
}
POST_IMPLEMENTATION_REVIEWER_ROLES = {"code-reviewer", "security-reviewer"}
PHASE_BUDGETS = {
    "design": ("max_usd_per_design_phase", Decimal("5")),
    "implementation": ("max_usd_per_implementation_phase", Decimal("12")),
    "code_review": ("max_usd_per_code_review_phase", Decimal("5")),
    "security_review": ("max_usd_per_security_review_phase", Decimal("5")),
}


def spending_phase(role: str | None) -> str:
    # Unknown/legacy roles consume implementation capacity rather than escaping
    # phase accounting. Phase selection is never accepted from model output.
    return {
        "ticket-scoper": "design",
        "design-reviewer": "design",
        "code-reviewer": "code_review",
        "security-reviewer": "security_review",
    }.get(role, "implementation")


# Admission pressure is recomputed; only ticket-local dollar incidents latch.
TRANSIENT_PAUSE_REASONS = {
    "max_model_runs_per_ticket",
    "max_reviewer_runs_per_ticket",
    "max_usd_per_sprint",
}
DEFAULT_PROVIDER_READ_TIMEOUT_SECONDS = 900
DEFAULT_BUDGETS = {
    "max_usd_per_run": Decimal("10.00"),
    "max_usd_per_ticket": Decimal("30.00"),
    "max_usd_per_sprint": Decimal("300.00"),
    "warn_usd_per_ticket": Decimal("10.00"),
    "pause_usd_per_ticket": Decimal("20.00"),
    "max_model_runs_per_ticket": 12,
    "max_reviewer_runs_per_ticket": 6,
    "max_output_tokens_per_turn": 4096,
    "max_tool_rounds": 8,
    "max_tool_output_chars": 12000,
    "tool_timeout_seconds": 300,
    "provider_read_timeout_seconds": DEFAULT_PROVIDER_READ_TIMEOUT_SECONDS,
    "max_pre_ack_retries": 2,
    "max_rate_limit_retries": 8,
    "max_rate_limit_wait_seconds": 600,
    "retry_backoff_seconds": 2,
    "retry_max_backoff_seconds": 60,
}
DEFAULT_BUDGETS.update({key: maximum for key, maximum in PHASE_BUDGETS.values()})
NON_OVERRIDABLE_MAXIMA = {
    "max_usd_per_run": Decimal("10.00"),
    "max_usd_per_ticket": Decimal("30.00"),
    "max_usd_per_sprint": Decimal("300.00"),
    "pause_usd_per_ticket": Decimal("20.00"),
    "max_model_runs_per_ticket": 12,
    "max_reviewer_runs_per_ticket": 6,
}
NON_OVERRIDABLE_MAXIMA.update({key: maximum for key, maximum in PHASE_BUDGETS.values()})
CREDENTIAL_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_ADM_API_KEY",
    "AZURE_ADM_BASE_URL",
}


class AgentError(RuntimeError):
    pass


class BudgetError(AgentError):
    pass


class ProviderAdmissionError(AgentError):
    """Shared admission refused before sending this request to the provider."""


class ProviderHTTPError(AgentError):
    def __init__(
        self, status: int, body: str, retry_after_seconds: float | None = None
    ):
        super().__init__(f"provider returned HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body
        self.retry_after_seconds = retry_after_seconds


def load_orchestration_env(config_path: Path) -> list[str]:
    """Load provider credentials beside config without evaluating shell syntax.

    Container- or host-supplied variables always win. Only the provider keys the
    runner consumes are accepted, keeping a repository file from changing PATH
    or unrelated process behavior.
    """
    env_path = config_path.resolve().parent / ".env"
    if not env_path.is_file():
        return []
    parsed: dict[str, str] = {}
    for line_no, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise AgentError(f"invalid {env_path} line {line_no}; expected KEY=value")
        key, value = match.groups()
        if key not in CREDENTIAL_ENV_KEYS:
            continue
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise AgentError(
                    f"invalid quoted value in {env_path} line {line_no}"
                ) from exc
            if not isinstance(value, str):
                raise AgentError(f"invalid quoted value in {env_path} line {line_no}")
        elif value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise AgentError(
                    f"unterminated quoted value in {env_path} line {line_no}"
                )
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value or "\n" in value or "\r" in value:
            raise AgentError(f"invalid control character in {env_path} line {line_no}")
        parsed[key] = value
    loaded = []
    for key, value in parsed.items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class ProviderAmbiguous(AgentError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def decimal_value(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AgentError(f"{label} must be a decimal number") from exc
    if result < 0:
        raise AgentError(f"{label} must not be negative")
    return result


def int_value(value: Any, label: str, minimum: int = 1) -> int:
    try:
        result = int(str(value))
    except ValueError as exc:
        raise AgentError(f"{label} must be an integer") from exc
    if result < minimum:
        raise AgentError(f"{label} must be at least {minimum}")
    return result


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def runtime_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise AgentError(f"runtime path escapes repository: {relative}")
    return candidate


def shared_repository_root(root: Path) -> Path:
    """Resolve the root that a repository shares with all of its git worktrees.

    Budget ceilings are only real if every concurrent lane counts against one
    ledger. Agent worktrees each carry their own checkout, so a worktree-relative
    ledger silently multiplies `max_usd_per_ticket` and `max_usd_per_sprint` by
    the number of running lanes. `git rev-parse --git-common-dir` names the
    directory shared by the main checkout and all of its worktrees; its parent is
    the canonical root. Fall back to the given root when git cannot answer, so a
    non-repository directory still gets a working local ledger.
    """
    return resolve_shared_repository_root(root)


def roll_conversation_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return messages carrying exactly one cache breakpoint at the conversation end.

    An Anthropic request allows four breakpoints and already spends two on the
    stable system prefix and the tool definitions. Without a third, every tool
    round re-sends the whole accumulated transcript at full input price. Moving a
    single breakpoint forward each round caches that transcript instead, and
    moving rather than adding keeps the request inside the four-breakpoint cap.

    The response content lists handed in by the transport are never mutated; the
    stripped and marked blocks are copies.
    """
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            blocks = [
                {key: value for key, value in block.items() if key != "cache_control"}
                if isinstance(block, dict) and "cache_control" in block
                else block
                for block in content
            ]
            message = dict(message, content=blocks)
        result.append(message)
    for message in reversed(result):
        content = message.get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1] = dict(content[-1], cache_control={"type": "ephemeral"})
            break
    return result


def roll_bedrock_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Move one Bedrock cache point to the end of the reusable transcript."""
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            blocks = [
                block
                for block in content
                if not (isinstance(block, dict) and "cachePoint" in block)
            ]
            message = dict(message, content=blocks)
        result.append(message)
    for message in reversed(result):
        content = message.get("content")
        if isinstance(content, list) and content:
            message["content"] = list(content) + [
                {"cachePoint": {"type": "default", "ttl": "1h"}}
            ]
            break
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AgentError(f"configuration file not found: {path}")
    engine = Path(__file__).with_name("orchestration-engine.py")
    spec = importlib.util.spec_from_file_location("orchestration_policy_loader", engine)
    if spec is None or spec.loader is None:
        raise AgentError("could not load orchestration configuration parser")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        value = module.load_simple_yaml(path)
    except Exception as exc:
        raise AgentError(f"could not parse configuration: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentError("configuration root must be a map")
    return value


@dataclass(frozen=True)
class Pricing:
    input_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    output_per_mtok: Decimal
    long_context_threshold_tokens: int
    long_context_input_multiplier: Decimal
    long_context_output_multiplier: Decimal

    @classmethod
    def from_config(cls, config: dict[str, Any], model: str) -> "Pricing":
        llm = config.get("llm")
        pricing = llm.get("pricing") if isinstance(llm, dict) else None
        entry = pricing.get(model) if isinstance(pricing, dict) else None
        if not isinstance(entry, dict):
            raise AgentError(
                f"llm.pricing.{model} is required for API execution so USD limits can be enforced"
            )
        required = (
            "input_per_mtok",
            "cache_write_per_mtok",
            "cache_read_per_mtok",
            "output_per_mtok",
        )
        missing = [key for key in required if key not in entry]
        if missing:
            raise AgentError(f"llm.pricing.{model} is missing: {', '.join(missing)}")
        rates = [
            decimal_value(entry[key], f"llm.pricing.{model}.{key}") for key in required
        ]
        threshold = int_value(
            entry.get("long_context_threshold_tokens", 10**12),
            f"llm.pricing.{model}.long_context_threshold_tokens",
        )
        input_multiplier = decimal_value(
            entry.get("long_context_input_multiplier", 1),
            f"llm.pricing.{model}.long_context_input_multiplier",
        )
        output_multiplier = decimal_value(
            entry.get("long_context_output_multiplier", 1),
            f"llm.pricing.{model}.long_context_output_multiplier",
        )
        if input_multiplier < 1 or output_multiplier < 1:
            raise AgentError("long-context pricing multipliers must be at least 1")
        return cls(*rates, threshold, input_multiplier, output_multiplier)

    def actual_cost(self, usage: dict[str, int]) -> Decimal:
        input_total = (
            usage["input_tokens"]
            + usage["cache_write_tokens"]
            + usage["cache_read_tokens"]
        )
        input_multiplier = (
            self.long_context_input_multiplier
            if input_total > self.long_context_threshold_tokens
            else Decimal("1")
        )
        output_multiplier = (
            self.long_context_output_multiplier
            if input_total > self.long_context_threshold_tokens
            else Decimal("1")
        )
        total = (
            input_multiplier
            * (
                Decimal(usage["input_tokens"]) * self.input_per_mtok
                + Decimal(usage["cache_write_tokens"]) * self.cache_write_per_mtok
                + Decimal(usage["cache_read_tokens"]) * self.cache_read_per_mtok
            )
            + output_multiplier * Decimal(usage["output_tokens"]) * self.output_per_mtok
        )
        return total / MILLION

    def worst_case(self, input_tokens: int, output_tokens: int) -> Decimal:
        input_rate = max(self.input_per_mtok, self.cache_write_per_mtok)
        input_multiplier = (
            self.long_context_input_multiplier
            if input_tokens > self.long_context_threshold_tokens
            else Decimal("1")
        )
        output_multiplier = (
            self.long_context_output_multiplier
            if input_tokens > self.long_context_threshold_tokens
            else Decimal("1")
        )
        return (
            input_multiplier * Decimal(input_tokens) * input_rate
            + output_multiplier * Decimal(output_tokens) * self.output_per_mtok
        ) / MILLION


def budgets_from_config(config: dict[str, Any]) -> dict[str, Any]:
    result = dict(DEFAULT_BUDGETS)
    llm = config.get("llm")
    raw = llm.get("budgets") if isinstance(llm, dict) else None
    if raw is not None and not isinstance(raw, dict):
        raise AgentError("llm.budgets must be a map")
    raw = raw or {}
    for key in (
        "max_usd_per_run",
        "max_usd_per_ticket",
        "max_usd_per_sprint",
        "warn_usd_per_ticket",
        "pause_usd_per_ticket",
        *(key for key, _ in PHASE_BUDGETS.values()),
    ):
        if key in raw:
            result[key] = decimal_value(raw[key], f"llm.budgets.{key}")
    for key in (
        "max_output_tokens_per_turn",
        "max_tool_rounds",
        "max_tool_output_chars",
        "tool_timeout_seconds",
        "provider_read_timeout_seconds",
        "max_pre_ack_retries",
        "max_rate_limit_retries",
        "max_rate_limit_wait_seconds",
        "retry_backoff_seconds",
        "retry_max_backoff_seconds",
        "max_model_runs_per_ticket",
        "max_reviewer_runs_per_ticket",
    ):
        if key in raw:
            minimum = (
                0
                if key
                in {
                    "max_pre_ack_retries",
                    "max_rate_limit_retries",
                    "retry_backoff_seconds",
                    "max_model_runs_per_ticket",
                    "max_reviewer_runs_per_ticket",
                }
                else 1
            )
            result[key] = int_value(raw[key], f"llm.budgets.{key}", minimum=minimum)
    for key, _ in PHASE_BUDGETS.values():
        if result[key] <= 0:
            raise AgentError(f"llm.budgets.{key} must be greater than zero")
    # Repository configuration may tighten incident breakers, never relax them.
    # Ticket-specific host grants are applied separately at admission; editing
    # the worktree cannot raise these defaults or shared/per-run ceilings.
    for key, maximum in NON_OVERRIDABLE_MAXIMA.items():
        configured = result[key]
        result[key] = maximum if not configured else min(configured, maximum)
    hard = result["max_usd_per_ticket"]
    if "pause_usd_per_ticket" not in raw:
        result["pause_usd_per_ticket"] = min(Decimal("20"), hard * Decimal("0.75"))
    if "warn_usd_per_ticket" not in raw:
        result["warn_usd_per_ticket"] = min(
            Decimal("10"), result["pause_usd_per_ticket"] * Decimal("0.666666")
        )
    if result["max_usd_per_run"] <= 0:
        raise AgentError("llm.budgets.max_usd_per_run must be greater than zero")
    warning = result["warn_usd_per_ticket"]
    pause = result["pause_usd_per_ticket"]
    if warning and pause and warning >= pause:
        raise AgentError("warn_usd_per_ticket must be lower than pause_usd_per_ticket")
    if pause and hard and pause >= hard:
        raise AgentError("pause_usd_per_ticket must be lower than max_usd_per_ticket")
    return result


def normalize_ticket_scope(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", normalized):
        raise AgentError("ticket must be a canonical Jira key such as PROJ-123")
    return normalized


def normalize_sprint_scope(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", normalized):
        raise AgentError(
            "sprint must be a canonical id containing only letters, digits, dot, underscore, or hyphen"
        )
    return normalized


def self_checks(config: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in config.get("self_check", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("run") or "").strip()
        if name and command:
            result[name] = command
    for item in config.get("verification", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("run") or "").strip()
        if name and command:
            result[f"verification:{name}"] = command
    return result


class UsageLedger:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = runtime_path(root, ".orchestration/.llm-usage")
        self.path = self.directory / "usage.jsonl"
        self.lock_path = self.directory / ".lock"

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def snapshot(self) -> list[dict[str, Any]]:
        """Read a complete ledger between appends, including concurrent workers."""
        if not self.path.is_file():
            return []
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            return self._events()

    @staticmethod
    def counted_runs(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Runs with accepted usage or unresolved reservations, never pure rejections.

        A release closes a request, not its whole run: an earlier accepted tool
        turn must continue to count even if the final request was rejected.
        """
        _, pending = UsageLedger._totals(events)
        runs: dict[str, dict[str, Any]] = {}
        for event in [
            *pending.values(),
            *(e for e in events if e.get("kind") == "usage"),
        ]:
            if event.get("run_id"):
                runs[str(event.get("logical_review_id") or event["run_id"])] = event
        return runs

    @staticmethod
    def _totals(
        events: list[dict[str, Any]],
    ) -> tuple[Decimal, dict[str, dict[str, Any]]]:
        spent = Decimal("0")
        open_reservations: dict[str, dict[str, Any]] = {}
        for event in events:
            kind = event.get("kind")
            if kind == "reservation":
                open_reservations[str(event["reservation_id"])] = event
            elif kind == "usage":
                spent += decimal_value(event.get("cost_usd", 0), "ledger cost")
                open_reservations.pop(str(event.get("reservation_id")), None)
            elif kind == "release":
                open_reservations.pop(str(event.get("reservation_id")), None)
        return spent, open_reservations

    @staticmethod
    def _matches(event: dict[str, Any], field: str, value: str | None) -> bool:
        return value is not None and str(event.get(field) or "") == value

    @staticmethod
    def phase_totals(
        events: list[dict[str, Any]], ticket: str
    ) -> dict[str, dict[str, Decimal]]:
        totals = {
            phase: {"spent_usd": Decimal("0"), "reserved_usd": Decimal("0")}
            for phase in PHASE_BUDGETS
        }
        _, pending = UsageLedger._totals(events)
        reservations = {
            e.get("reservation_id"): e for e in events if e.get("kind") == "reservation"
        }
        for event in events:
            if event.get("kind") == "usage" and UsageLedger._matches(
                event, "ticket", ticket
            ):
                role = reservations.get(event.get("reservation_id"), {}).get(
                    "role"
                ) or event.get("role")
                totals[spending_phase(role)]["spent_usd"] += decimal_value(
                    event.get("cost_usd", 0), "phase cost"
                )
        for event in pending.values():
            if UsageLedger._matches(event, "ticket", ticket):
                totals[spending_phase(event.get("role"))]["reserved_usd"] += (
                    decimal_value(
                        event.get("projected_cost_usd", 0), "phase reservation"
                    )
                )
        return totals

    @staticmethod
    def phase_limits(events, ticket, limits):
        result = {
            phase: min(decimal_value(limits.get(key, maximum), key), maximum)
            for phase, (key, maximum) in PHASE_BUDGETS.items()
        }
        transfer = next(
            (
                e
                for e in events
                if e.get("kind") == "design_budget_transferred"
                and e.get("ticket") == ticket
            ),
            None,
        )
        if transfer:
            # Reconfiguration may tighten envelopes; never manufacture capacity.
            amount = min(
                decimal_value(transfer["amount_usd"], "transfer"),
                max(
                    Decimal("0"),
                    result["design"]
                    - decimal_value(transfer["design_spent_usd"], "spent"),
                ),
            )
            result["design"] -= amount
            result["implementation"] += amount
        return result

    def transfer_design_budget(self, ticket, limits, receipt):
        """Close unused design capacity after a controller-verified PASS."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            events = self._events()
            if any(
                e.get("kind") == "design_budget_transferred"
                and e.get("ticket") == ticket
                for e in events
            ):
                return False
            totals = self.phase_totals(events, ticket)["design"]
            if totals["reserved_usd"]:
                return False  # Uncertain provider work keeps its full reservation.
            maximum = self.phase_limits(events, ticket, limits)["design"]
            amount = max(Decimal("0"), maximum - totals["spent_usd"])
            if amount <= 0:
                return False
            self._append_locked(
                dict(
                    kind="design_budget_transferred",
                    timestamp=utc_now(),
                    ticket=ticket,
                    amount_usd=str(amount),
                    design_spent_usd=str(totals["spent_usd"]),
                    receipt=receipt,
                )
            )
            return True

    def reserve(
        self,
        *,
        projected: Decimal,
        limits: dict[str, Any],
        run_id: str,
        ticket: str | None,
        sprint: str | None,
        provider: str,
        model: str,
        role: str | None = None,
        logical_review_id: str | None = None,
    ) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            events = self._events()
            authority_ceiling: Decimal | None = None
            try:
                restart = (
                    authorized_restart_grant(self.root, ticket) if ticket else None
                )
            except AuthorityError as exc:
                raise BudgetError(str(exc)) from exc
            allowances = restart["allowances"] if restart else {}
            if ticket:
                try:
                    authority_ceiling = authorized_budget_ceiling(self.root, ticket)
                    if restart:
                        authority_ceiling = max(
                            authority_ceiling or Decimal("0"),
                            Decimal(allowances["ticket_usd"]),
                        )
                except AuthorityError as exc:
                    raise BudgetError(str(exc)) from exc
            if ticket:
                cost_pause_indexes = [
                    index
                    for index, event in enumerate(events)
                    if event.get("kind") == "ticket_budget_pause"
                    and self._matches(event, "ticket", ticket)
                    and str(event.get("reason") or "") not in TRANSIENT_PAUSE_REASONS
                ]
                last_cost_pause = max(cost_pause_indexes, default=-1)
                last_reset = max(
                    (
                        index
                        for index, event in enumerate(events)
                        if event.get("kind") == "ticket_budget_reset"
                        and self._matches(event, "ticket", ticket)
                    ),
                    default=-1,
                )
                # Run-count incidents are recomputed below from the current,
                # phase-aware counters. They must not leave a stale dollar
                # pause behind after a reviewed plugin upgrade changes the
                # classification. Genuine cost pauses remain capability-gated.
                if last_cost_pause > last_reset and authority_ceiling is None:
                    raise BudgetError(
                        f"ticket_budget_pause is active for {ticket}; operator reset required"
                    )
                # Preserve the independent total execution-attempt breaker:
                # zero-cost failures must not enable an unbounded retry loop.
                run_ids = {
                    str(event.get("run_id"))
                    for event in events
                    if event.get("kind") == "reservation"
                    and self._matches(event, "ticket", ticket)
                    and event.get("run_id")
                    and event.get("role") != "design-reviewer"
                }
                is_new_run = run_id not in run_ids
                max_runs = max(
                    limits["max_model_runs_per_ticket"], allowances.get("model_runs", 0)
                )
                if (
                    role != "design-reviewer"
                    and is_new_run
                    and max_runs
                    and len(run_ids) >= max_runs
                ):
                    self._append_locked(
                        {
                            "kind": "ticket_budget_pause",
                            "timestamp": utc_now(),
                            "ticket": ticket,
                            "run_id": run_id,
                            "reason": "max_model_runs_per_ticket",
                        }
                    )
                    raise BudgetError(
                        f"max_model_runs_per_ticket={max_runs} reached for {ticket}; human action required"
                    )
                reviewer_run_ids = {
                    run
                    for run, event in self.counted_runs(events).items()
                    if self._matches(event, "ticket", ticket)
                    and event.get("role") in POST_IMPLEMENTATION_REVIEWER_ROLES
                }
                max_reviewers = max(
                    limits["max_reviewer_runs_per_ticket"],
                    allowances.get("review_runs", 0),
                )
                if (
                    (logical_review_id or run_id) not in reviewer_run_ids
                    and role in POST_IMPLEMENTATION_REVIEWER_ROLES
                    and max_reviewers
                    and len(reviewer_run_ids) >= max_reviewers
                ):
                    self._append_locked(
                        {
                            "kind": "ticket_budget_pause",
                            "timestamp": utc_now(),
                            "ticket": ticket,
                            "run_id": run_id,
                            "reason": "max_reviewer_runs_per_ticket",
                        }
                    )
                    raise BudgetError(
                        f"max_reviewer_runs_per_ticket={max_reviewers} post-implementation "
                        f"review runs reached for {ticket}; "
                        "human action required"
                    )
            # Operational retries share a review round, but cannot loop forever.
            if logical_review_id:
                attempts = {
                    e.get("run_id")
                    for e in events
                    if e.get("kind") == "reservation"
                    and e.get("logical_review_id") == logical_review_id
                }
                if run_id not in attempts and len(attempts) >= 3:
                    raise BudgetError("review operational retry ceiling reached")
                phase_rounds = {
                    identity
                    for identity, event in self.counted_runs(events).items()
                    if self._matches(event, "ticket", ticket)
                    and event.get("role") == role
                }
                if (
                    role in POST_IMPLEMENTATION_REVIEWER_ROLES
                    and logical_review_id not in phase_rounds
                    and len(phase_rounds)
                    >= max(
                        3,
                        allowances.get(
                            "security_rounds"
                            if role == "security-reviewer"
                            else "code_rounds",
                            0,
                        ),
                    )
                ):
                    raise BudgetError(f"{role} logical review round ceiling reached")
            scopes = [
                ("run_id", run_id, "max_usd_per_run"),
                ("ticket", ticket, "max_usd_per_ticket"),
                ("sprint", sprint, "max_usd_per_sprint"),
            ]
            for field, value, limit_key in scopes:
                limit = limits[limit_key]
                if field == "ticket" and authority_ceiling is not None:
                    limit = max(limit, authority_ceiling)
                if not value or limit <= 0:
                    continue
                used = sum(
                    (
                        decimal_value(event.get("cost_usd", 0), "ledger cost")
                        for event in events
                        if event.get("kind") == "usage"
                        and self._matches(event, field, value)
                    ),
                    Decimal("0"),
                )
                _, open_items = self._totals(events)
                reserved = sum(
                    (
                        decimal_value(
                            event.get("projected_cost_usd", 0), "ledger reservation"
                        )
                        for event in open_items.values()
                        if self._matches(event, field, value)
                    ),
                    Decimal("0"),
                )
                if used + reserved + projected > limit:
                    if ticket and field == "ticket":
                        self._append_locked(
                            {
                                "kind": "ticket_budget_pause",
                                "timestamp": utc_now(),
                                "ticket": ticket,
                                "run_id": run_id,
                                "reason": limit_key,
                                "projected_total_usd": str(used + reserved + projected),
                            }
                        )
                    raise BudgetError(
                        f"{limit_key} would be exceeded: spent ${used:.6f}, reserved "
                        f"${reserved:.6f}, next request up to ${projected:.6f}, limit ${limit:.6f}"
                    )
            if ticket:
                phase = spending_phase(role)
                phase_key, default_limit = PHASE_BUDGETS[phase]
                limit = max(
                    self.phase_limits(events, ticket, limits)[phase],
                    Decimal(allowances.get(phase + "_usd", "0")),
                )
                totals = self.phase_totals(events, ticket)[phase]
                if totals["spent_usd"] + totals["reserved_usd"] + projected > limit:
                    # This is request admission pressure, not a sticky ticket
                    # pause: release/reconciliation may restore capacity and
                    # another phase can still proceed within its own envelope.
                    raise BudgetError(
                        f"{phase_key} would be exceeded for {ticket}: spent "
                        f"${totals['spent_usd']:.6f}, reserved ${totals['reserved_usd']:.6f}, "
                        f"next request up to ${projected:.6f}, limit ${limit:.6f}"
                    )
                used = sum(
                    (
                        decimal_value(event.get("cost_usd", 0), "ledger cost")
                        for event in events
                        if event.get("kind") == "usage"
                        and self._matches(event, "ticket", ticket)
                    ),
                    Decimal("0"),
                )
                _, open_items = self._totals(events)
                reserved = sum(
                    (
                        decimal_value(
                            event.get("projected_cost_usd", 0), "ledger reservation"
                        )
                        for event in open_items.values()
                        if self._matches(event, "ticket", ticket)
                    ),
                    Decimal("0"),
                )
                projected_total = used + reserved + projected
                pause = limits["pause_usd_per_ticket"]
                if authority_ceiling is not None:
                    pause = max(pause, authority_ceiling)
                if pause and projected_total > pause:
                    if not any(
                        event.get("kind") == "ticket_budget_pause"
                        and self._matches(event, "ticket", ticket)
                        and event.get("run_id") == run_id
                        for event in events
                    ):
                        self._append_locked(
                            {
                                "kind": "ticket_budget_pause",
                                "timestamp": utc_now(),
                                "ticket": ticket,
                                "run_id": run_id,
                                "projected_total_usd": str(projected_total),
                                "pause_usd": str(pause),
                            }
                        )
                    raise BudgetError(
                        f"pause_usd_per_ticket requires operator action for {ticket}: projected total "
                        f"${projected_total:.6f}, pause ${pause:.6f}; operator policy change required"
                    )
                warning = limits["warn_usd_per_ticket"]
                if (
                    warning
                    and projected_total > warning
                    and not any(
                        event.get("kind") == "ticket_budget_warning"
                        and self._matches(event, "ticket", ticket)
                        for event in events
                    )
                ):
                    self._append_locked(
                        {
                            "kind": "ticket_budget_warning",
                            "timestamp": utc_now(),
                            "ticket": ticket,
                            "projected_total_usd": str(projected_total),
                            "warning_usd": str(warning),
                        }
                    )
            reservation_id = "resv_" + uuid.uuid4().hex
            event = {
                "kind": "reservation",
                "logical_review_id": logical_review_id,
                "timestamp": utc_now(),
                "reservation_id": reservation_id,
                "run_id": run_id,
                "role": role,
                "phase": spending_phase(role),
                "ticket": ticket,
                "sprint": sprint,
                "provider": provider,
                "model": model,
                "projected_cost_usd": str(projected),
            }
            self._append_locked(event)
            return reservation_id

    def _append_locked(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            os.chmod(self.path, 0o600)
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, event: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._append_locked(event)

    def settle(
        self,
        reservation_id: str,
        *,
        run_id: str,
        ticket: str | None,
        sprint: str | None,
        provider: str,
        model: str,
        response_id: str,
        usage: dict[str, int],
        cost: Decimal,
        role: str | None = None,
        latency_ms: int | None = None,
        rate_limit_wait_seconds: float | None = None,
        tool_round: int | None = None,
    ) -> None:
        # Performance fields are optional: a reconciled request is settled from
        # provider records long after the fact and has no measurable latency.
        performance = {
            "latency_ms": latency_ms,
            "rate_limit_wait_seconds": rate_limit_wait_seconds,
            "tool_round": tool_round,
        }
        event = {
            "kind": "usage",
            "timestamp": utc_now(),
            "reservation_id": reservation_id,
            "run_id": run_id,
            "role": role,
            "phase": spending_phase(role),
            "ticket": ticket,
            "sprint": sprint,
            "provider": provider,
            "model": model,
            "response_id": response_id,
            **usage,
            **{key: value for key, value in performance.items() if value is not None},
            "cost_usd": str(cost),
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            events = self._events()
            reserved = next(
                (
                    e
                    for e in events
                    if e.get("kind") == "reservation"
                    and e.get("reservation_id") == reservation_id
                ),
                {},
            )
            if reserved.get("role"):
                event["role"] = reserved["role"]
                event["phase"] = spending_phase(reserved["role"])
            if reserved.get("logical_review_id"):
                event["logical_review_id"] = reserved["logical_review_id"]
            existing = next(
                (
                    item
                    for item in events
                    if item.get("kind") == "usage"
                    and item.get("reservation_id") == reservation_id
                ),
                None,
            )
            if existing:
                if existing.get("response_id") == response_id:
                    return
                raise AgentError(f"reservation {reservation_id} was already settled")
            _, open_items = self._totals(events)
            if reservation_id not in open_items:
                raise AgentError(f"reservation {reservation_id} is not open")
            self._append_locked(event)

    def release(self, reservation_id: str, run_id: str, reason: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            events = self._events()
            if any(
                item.get("kind") == "usage"
                and item.get("reservation_id") == reservation_id
                for item in events
            ):
                raise AgentError(
                    f"reservation {reservation_id} already has recorded usage"
                )
            _, open_items = self._totals(events)
            if reservation_id not in open_items:
                return
            self._append_locked(
                {
                    "kind": "release",
                    "timestamp": utc_now(),
                    "reservation_id": reservation_id,
                    "run_id": run_id,
                    "reason": reason,
                }
            )

    def summary(self) -> dict[str, Any]:
        events = self._events()
        spent, open_items = self._totals(events)
        token_fields = (
            "input_tokens",
            "cache_write_tokens",
            "cache_read_tokens",
            "output_tokens",
        )
        tokens = {
            field: sum(
                int(event.get(field, 0))
                for event in events
                if event.get("kind") == "usage"
            )
            for field in token_fields
        }
        return {
            "cost_usd": str(spent),
            **tokens,
            "open_reservations": list(open_items.values()),
            "ledger": str(self.path),
        }


def anthropic_context_beta(payload):
    """Enable only context editing that cannot introduce extra generation turns."""
    if "context_management" not in payload:
        return None
    context = payload["context_management"]
    if (
        not isinstance(context, dict)
        or set(context) != {"edits"}
        or not isinstance(context["edits"], list)
        or any(
            not isinstance(edit, dict)
            or edit.get("type")
            not in {"clear_tool_uses_20250919", "clear_thinking_20251015"}
            for edit in context["edits"]
        )
    ):
        raise AgentError(
            "native gateway supports bounded context editing only; server compaction is not metered"
        )
    return "context-management-2025-06-27"


class HttpTransport:
    def __init__(
        self,
        timeout: int = DEFAULT_PROVIDER_READ_TIMEOUT_SECONDS,
        bedrock_client: Any | None = None,
    ):
        self.timeout = timeout
        self._bedrock_client = bedrock_client

    def _get_bedrock_client(self) -> Any:
        if self._bedrock_client is not None:
            return self._bedrock_client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise AgentError(
                "Bedrock API execution requires boto3; install requirements-bedrock.txt "
                "in the controller's Python environment"
            ) from exc
        # The runner owns safe retries. Keeping one SDK attempt prevents a read
        # timeout from being invisibly resubmitted after Bedrock may have begun
        # model work, while adaptive mode still supplies client-side rate control.
        config = Config(
            retries={"total_max_attempts": 1, "mode": "adaptive"},
            connect_timeout=min(10, self.timeout),
            read_timeout=self.timeout,
            max_pool_connections=20,
            user_agent_appid="orka/1.0.0",
        )
        session = boto3.Session()
        self._bedrock_client = session.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or session.region_name,
            config=config,
        )
        return self._bedrock_client

    def _bedrock_request(
        self,
        path: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        try:
            from botocore.exceptions import (
                BotoCoreError,
                ClientError,
                ConnectTimeoutError,
                EndpointConnectionError,
                ReadTimeoutError,
            )
        except ImportError as exc:
            raise AgentError(
                "Bedrock API execution requires boto3; install requirements-bedrock.txt "
                "in the controller's Python environment"
            ) from exc
        client = self._get_bedrock_client()
        try:
            if path == "count_tokens":
                result = client.count_tokens(**payload)
            elif path == "converse":
                body = dict(payload)
                if idempotency_key:
                    metadata = dict(body.get("requestMetadata") or {})
                    metadata["orchestrationReservation"] = idempotency_key
                    body["requestMetadata"] = metadata
                result = client.converse(**body)
            else:
                raise AgentError(f"unsupported Bedrock runtime operation: {path}")
        except ClientError as exc:
            error = exc.response.get("Error") or {}
            code = str(error.get("Code") or "")
            status = int(
                (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
                or 500
            )
            message = str(error.get("Message") or exc)
            if code == "ThrottlingException":
                raise ProviderHTTPError(429, message) from exc
            if code in {"ServiceUnavailableException", "ModelNotReadyException"}:
                raise ProviderHTTPError(529, message) from exc
            if code in {"AccessDeniedException", "ResourceNotFoundException"}:
                raise ProviderHTTPError(
                    403 if code == "AccessDeniedException" else 404, message
                ) from exc
            if code == "ValidationException":
                raise ProviderHTTPError(400, message) from exc
            # A server/model timeout or internal error can occur after work has
            # started. Preserve the reservation for explicit reconciliation.
            raise ProviderAmbiguous(
                f"Bedrock submission outcome is unknown ({code or status}): {message}"
            ) from exc
        except (
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
            BotoCoreError,
        ) as exc:
            raise ProviderAmbiguous(
                f"Bedrock submission outcome is unknown: {exc}"
            ) from exc
        if not isinstance(result, dict):
            raise ProviderAmbiguous("Bedrock returned an invalid response object")
        if path == "count_tokens":
            return {"input_tokens": int(result.get("inputTokens") or 0)}
        response = dict(result)
        metadata = response.get("ResponseMetadata") or {}
        response["id"] = str(metadata.get("RequestId") or "")
        return response

    def request(
        self,
        provider: str,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if provider == "bedrock":
            return self._bedrock_request(path, payload, idempotency_key)
        if provider == "bedrock_mantle":
            try:
                import boto3
                from botocore.auth import SigV4Auth
                from botocore.awsrequest import AWSRequest
            except ImportError as exc:
                raise AgentError(
                    "Bedrock Mantle API execution requires boto3; install "
                    "requirements-bedrock.txt in the controller's Python environment"
                ) from exc
            session = boto3.Session()
            region = (
                os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION")
                or session.region_name
            )
            if not region:
                raise AgentError(
                    "AWS_REGION is required for Bedrock Mantle API execution"
                )
            base = os.environ.get(
                "BEDROCK_MANTLE_BASE_URL",
                f"https://bedrock-mantle.{region}.api.aws/v1",
            )
            headers: dict[str, str] = {}
            key = "aws-sigv4"
        elif provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
            base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
            if not base.rstrip("/").endswith("/v1"):
                base = base.rstrip("/") + "/v1"
            headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
            if beta := anthropic_context_beta(payload):
                headers["anthropic-beta"] = beta
        elif provider == "azure_adm":
            key = os.environ.get("AZURE_ADM_API_KEY")
            base = os.environ.get("AZURE_ADM_BASE_URL", "")
            headers = {"api-key": key or ""}
            if not base:
                raise AgentError(
                    "AZURE_ADM_BASE_URL is required for azure_adm API execution"
                )
        else:
            key = os.environ.get("OPENAI_API_KEY")
            base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            headers = {"Authorization": f"Bearer {key or ''}"}
        if not key:
            raise AgentError(
                f"{provider.upper()}_API_KEY is required for {provider} API execution"
            )
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = "orka-api-agent/1.0.0"
        if idempotency_key:
            if provider == "azure_adm":
                headers["x-ms-client-request-id"] = idempotency_key
            else:
                headers["Idempotency-Key"] = idempotency_key
        url = base.rstrip("/") + "/" + path.lstrip("/")
        request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if provider == "bedrock_mantle":
            credentials = session.get_credentials()
            if credentials is None:
                raise AgentError(
                    "AWS credentials are required for Bedrock Mantle API execution"
                )
            aws_request = AWSRequest(
                method="POST",
                url=url,
                data=request_body,
                headers=headers,
            )
            SigV4Auth(
                credentials.get_frozen_credentials(), "bedrock-mantle", region
            ).add_auth(aws_request)
            headers = dict(aws_request.prepare().headers.items())
        request = urllib.request.Request(
            url,
            data=request_body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retry_after = None
            raw_ms = exc.headers.get("retry-after-ms") if exc.headers else None
            raw_seconds = exc.headers.get("Retry-After") if exc.headers else None
            try:
                if raw_ms is not None:
                    retry_after = max(0.0, float(raw_ms) / 1000.0)
                elif raw_seconds is not None:
                    retry_after = max(0.0, float(raw_seconds))
            except ValueError:
                retry_after = None
            raise ProviderHTTPError(exc.code, body, retry_after) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderAmbiguous(
                f"provider submission outcome is unknown: {exc}"
            ) from exc
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderAmbiguous(
                "provider returned a non-JSON success response"
            ) from exc
        if not isinstance(value, dict):
            raise ProviderAmbiguous("provider returned an invalid response object")
        return value


class ToolExecutor:
    def __init__(
        self,
        root: Path,
        checks: dict[str, str],
        max_output_chars: int,
        timeout: int,
    ):
        self.root = root.resolve()
        self.checks = checks
        self.max_output_chars = max_output_chars
        self.timeout = timeout

    def _path(self, raw: str) -> Path:
        if not raw or "\x00" in raw:
            raise AgentError("path must be non-empty")
        candidate = (self.root / raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise AgentError(f"path escapes repository: {raw}")
        return candidate

    def _relative(self, raw: str) -> str:
        return str(self._path(raw).relative_to(self.root))

    def _truncate(self, value: str) -> str:
        if len(value) <= self.max_output_chars:
            return value
        half = max(1, (self.max_output_chars - 80) // 2)
        return value[:half] + "\n... tool output truncated ...\n" + value[-half:]

    def _run(self, command: list[str], *, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                input=stdin,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"tool timed out after {self.timeout}s") from exc
        output = self._truncate(result.stdout or "")
        return f"exit={result.returncode}\n{output}".rstrip()

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            path = self._path(str(arguments.get("path") or ""))
            if not path.is_file():
                raise AgentError(f"file not found: {arguments.get('path')}")
            start = int_value(arguments.get("start_line", 1), "start_line")
            end = int_value(arguments.get("end_line", start + 399), "end_line")
            if end < start or end - start > 999:
                raise AgentError("read_file may return at most 1000 lines")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            selected = "\n".join(
                f"{index}: {lines[index - 1]}"
                for index in range(start, min(end, len(lines)) + 1)
            )
            return self._truncate(selected)
        if name == "search":
            query = str(arguments.get("query") or "")
            if not query or len(query) > 500:
                raise AgentError("search query must contain 1-500 characters")
            raw_paths = arguments.get("paths") or ["."]
            if not isinstance(raw_paths, list) or len(raw_paths) > 20:
                raise AgentError(
                    "search paths must be a list of at most 20 repository paths"
                )
            paths = [
                self._relative(str(path)) if str(path) != "." else "."
                for path in raw_paths
            ]
            return self._run(
                [
                    "rg",
                    "-n",
                    "--fixed-strings",
                    "--max-count",
                    "100",
                    "--",
                    query,
                    *paths,
                ]
            )
        if name == "git_diff":
            base = str(arguments.get("base") or "HEAD")
            if base.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_./~^:+-]+", base):
                raise AgentError("invalid git base revision")
            raw_paths = arguments.get("paths") or []
            if not isinstance(raw_paths, list) or len(raw_paths) > 20:
                raise AgentError(
                    "diff paths must be a list of at most 20 repository paths"
                )
            paths = [self._relative(str(path)) for path in raw_paths]
            return self._run(
                ["git", "diff", "--no-ext-diff", "--unified=80", base, "--", *paths]
            )
        if name == "git_status":
            return self._run(["git", "status", "--short", "--branch"])
        if name == "run_check":
            check = str(arguments.get("name") or "")
            if check not in self.checks:
                raise AgentError(
                    f"unknown check {check!r}; allowed: {', '.join(sorted(self.checks))}"
                )
            return self._run(["bash", "-c", self.checks[check]])
        if name == "apply_patch":
            patch = str(arguments.get("patch") or "")
            if not patch or len(patch) > 500_000:
                raise AgentError("patch must contain 1-500000 characters")
            if "GIT binary patch" in patch or "Binary files " in patch:
                raise AgentError("binary patches are not allowed")
            for raw in re.findall(
                r"^(?:---|\+\+\+)\s+([^\t\n]+)", patch, flags=re.MULTILINE
            ):
                if raw == "/dev/null":
                    continue
                candidate = raw[2:] if raw.startswith(("a/", "b/")) else raw
                self._relative(candidate)
            checked = self._run(
                ["git", "apply", "--check", "--recount", "-"], stdin=patch
            )
            if not checked.startswith("exit=0"):
                return checked
            return self._run(["git", "apply", "--recount", "-"], stdin=patch)
        raise AgentError(f"tool is not implemented: {name}")


TOOL_SPECS = {
    "read_file": (
        "Read a bounded line range from one repository file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    "search": (
        "Search for an exact string in a bounded set of repository paths.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "git_diff": (
        "Read a raw unified Git diff, optionally restricted to repository paths.",
        {
            "type": "object",
            "properties": {
                "base": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "additionalProperties": False,
        },
    ),
    "git_status": (
        "Read the concise Git branch and worktree status.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    "run_check": (
        "Run one repository-configured self-check or verification by its exact name.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    "apply_patch": (
        "Apply a text-only standard unified Git patch inside the repository after Git validation. "
        "The patch must use diff --git / --- a/path / +++ b/path / @@ hunk syntax accepted by "
        "git apply; do not use wrapper markers such as '*** Begin Patch' or '*** Update File'.",
        {
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    ),
}


def tools_for_role(
    role: str,
    configured: list[str] | None,
    provider: str,
    model: str = "",
) -> list[dict[str, Any]]:
    ceiling = ROLE_TOOL_CEILINGS.get(role)
    if ceiling is None:
        raise AgentError(f"no API tool policy is defined for role: {role}")
    selected = set(configured or ceiling)
    unknown = selected - TOOL_NAMES
    forbidden = selected - ceiling
    if unknown:
        raise AgentError(
            f"unknown allowed_tools for {role}: {', '.join(sorted(unknown))}"
        )
    if forbidden:
        raise AgentError(f"role {role} may not receive: {', '.join(sorted(forbidden))}")
    result = []
    for name in sorted(selected):
        description, schema = TOOL_SPECS[name]
        if provider == "anthropic":
            # Anthropic strict tool use accepts a narrower JSON Schema subset
            # than these bounded tool specs use. The executor independently
            # enforces every path, range, collection, and size constraint.
            result.append(
                {"name": name, "description": description, "input_schema": schema}
            )
        elif provider == "bedrock":
            result.append(
                {
                    "toolSpec": {
                        "name": name,
                        "description": description,
                        "inputSchema": {"json": schema},
                        "strict": True,
                    }
                }
            )
        elif provider in {"azure_adm", "bedrock_mantle"}:
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": schema,
                    },
                }
            )
        else:
            result.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": schema,
                    # Several tools intentionally have optional bounds/path filters.
                    # OpenAI strict schemas require every property to be required;
                    # runtime validation below remains the security boundary.
                    "strict": False,
                }
            )
    if provider == "anthropic" and result:
        result[-1]["cache_control"] = {"type": "ephemeral"}
    if (
        provider == "bedrock"
        and context_pipeline.bedrock_model_family(model) == "anthropic"
        and result
    ):
        result.append({"cachePoint": {"type": "default", "ttl": "1h"}})
    return result


def normalize_usage(provider: str, response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage") or {}
    if provider == "anthropic":
        return {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_tokens": int(
                (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            ),
        }
    if provider == "bedrock":
        return {
            "input_tokens": int(usage.get("inputTokens") or 0),
            "cache_write_tokens": int(usage.get("cacheWriteInputTokens") or 0),
            "cache_read_tokens": int(usage.get("cacheReadInputTokens") or 0),
            "output_tokens": int(usage.get("outputTokens") or 0),
            "reasoning_tokens": 0,
        }
    if provider in {"azure_adm", "bedrock_mantle"}:
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        total = int(usage.get("prompt_tokens") or 0)
        cached = int(prompt_details.get("cached_tokens") or 0)
        visible_output = int(usage.get("completion_tokens") or 0)
        billed_output = max(
            visible_output,
            int(usage.get("total_tokens") or 0) - total,
        )
        return {
            "input_tokens": max(0, total - cached),
            "cache_write_tokens": 0,
            "cache_read_tokens": cached,
            "output_tokens": billed_output,
            "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
        }
    details = usage.get("input_tokens_details") or {}
    total = int(usage.get("input_tokens") or 0)
    cached = int(details.get("cached_tokens") or 0)
    cache_write = int(details.get("cache_write_tokens") or 0)
    return {
        "input_tokens": max(0, total - cached - cache_write),
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cached,
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(
            (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        ),
    }


def response_text(provider: str, response: dict[str, Any]) -> str:
    if provider == "anthropic":
        return "\n".join(
            str(block.get("text") or "")
            for block in response.get("content", [])
            if block.get("type") == "text"
        ).strip()
    if provider == "bedrock":
        message = (response.get("output") or {}).get("message") or {}
        return "\n".join(
            str(block.get("text") or "")
            for block in message.get("content", [])
            if "text" in block
        ).strip()
    if provider in {"azure_adm", "bedrock_mantle"}:
        choices = response.get("choices") or []
        if not choices:
            return ""
        return str((choices[0].get("message") or {}).get("content") or "").strip()
    if response.get("output_text"):
        return str(response["output_text"]).strip()
    chunks = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                chunks.append(str(block.get("text") or ""))
    return "\n".join(chunks).strip()


def review_text(provider: str, text: str) -> str:
    """Remove only a complete provider reasoning wrapper before JSON validation."""
    if provider != "bedrock_mantle":
        return text
    match = re.fullmatch(r"\s*<think>.*?</think>\s*(\{.*\})\s*", text, re.DOTALL)
    return match.group(1) if match else text


def tool_calls(provider: str, response: dict[str, Any]) -> list[dict[str, Any]]:
    if provider == "bedrock":
        message = (response.get("output") or {}).get("message") or {}
        calls = []
        for block in message.get("content", []):
            tool = block.get("toolUse") if isinstance(block, dict) else None
            if not isinstance(tool, dict):
                continue
            calls.append(
                {
                    "type": "tool_use",
                    "id": str(tool.get("toolUseId") or ""),
                    "name": str(tool.get("name") or ""),
                    "input": tool.get("input") or {},
                }
            )
        return calls
    if provider in {"azure_adm", "bedrock_mantle"}:
        choices = response.get("choices") or []
        if not choices:
            return []
        calls = (choices[0].get("message") or {}).get("tool_calls") or []
        return [
            {
                "type": "function_call",
                "id": str(call.get("id") or ""),
                "call_id": str(call.get("id") or ""),
                "name": str((call.get("function") or {}).get("name") or ""),
                "arguments": str((call.get("function") or {}).get("arguments") or "{}"),
            }
            for call in calls
        ]
    source = (
        response.get("content", [])
        if provider == "anthropic"
        else response.get("output", [])
    )
    expected = "tool_use" if provider == "anthropic" else "function_call"
    return [item for item in source if item.get("type") == expected]


class ApiAgent:
    def __init__(
        self,
        *,
        root: Path,
        config_path: Path,
        role: str,
        ticket: str | None,
        sprint: str | None,
        run_id: str,
        transport: HttpTransport | Any | None,
        review_authorization: str | None = None,
        review_pr: str | None = None,
        attempt_capability: str | None = None,
        worker_ref: str | None = None,
    ):
        self.root = root.resolve()
        try:
            self.config_path = canonical_config_path(self.root, config_path)
        except RuntimeStateError as exc:
            raise AgentError(str(exc)) from exc
        load_orchestration_env(self.config_path)
        self.config = load_yaml(self.config_path)
        self.route = context_pipeline.llm_route_from_config(self.config_path, role)
        if self.route["execution"] != "api":
            raise AgentError(
                f"role {self.route['role']} resolves to desktop, not API execution"
            )
        self.provider = self.route["provider"]
        self.model = self.route["model"]
        self.role = self.route["role"]
        self.ticket = normalize_ticket_scope(ticket)
        self.sprint = normalize_sprint_scope(sprint)
        self.run_id = run_id
        self.budgets = budgets_from_config(self.config)
        from provider_health import ProviderTransport

        self.transport = ProviderTransport(
            self.root,
            transport
            if transport is not None
            else HttpTransport(timeout=self.budgets["provider_read_timeout_seconds"]),
        )
        self.review_authorization = review_authorization
        self.review_pr = review_pr
        self.attempt_capability = attempt_capability
        self.worker_ref = worker_ref or run_id
        self.pricing = Pricing.from_config(self.config, self.model)
        # Tool execution stays sandboxed to this worktree; spend accounting and
        # run state are repository-wide so concurrent lanes share one ceiling.
        self.shared_root = shared_repository_root(self.root)
        try:
            migrate_legacy_runtime_dir(self.root, ".orchestration/.llm-usage")
            migrate_legacy_runtime_dir(self.root, ".orchestration/.llm-runs")
        except RuntimeStateError as exc:
            raise AgentError(str(exc)) from exc
        self.ledger = UsageLedger(self.shared_root)
        if self.role in {"implementer", "sprint-worker"}:
            if not self.ticket or not self.sprint or not self.attempt_capability:
                raise AgentError(
                    "orchestration worker requires ticket, sprint, and a controller-issued attempt capability"
                )
            try:
                validate_attempt_capability(
                    state_dir=runtime_path(
                        self.shared_root,
                        str(
                            self.config.get("sprint_checkpoint_dir")
                            or ".orchestration/.sprint-state"
                        ),
                    ),
                    token=self.attempt_capability,
                    repository=str(self.shared_root),
                    sprint=self.sprint,
                    ticket=self.ticket,
                    role=self.role,
                    run_id=self.run_id,
                    worker=self.worker_ref,
                    route=self.route,
                )
            except AttemptCapabilityError as exc:
                raise AgentError(str(exc)) from exc
        state_directory = runtime_path(self.shared_root, ".orchestration/.llm-runs")
        self.state_path = state_directory / f"{run_id}.json"
        if self.state_path.exists():
            raise AgentError(
                f"run id already exists: {run_id}; reconcile or choose a new id instead of overwriting it"
            )
        self.tool_executor = ToolExecutor(
            self.root,
            self_checks(self.config),
            self.budgets["max_tool_output_chars"],
            self.budgets["tool_timeout_seconds"],
        )
        self.tools = tools_for_role(
            self.role,
            self.route.get("allowed_tools") or None,
            self.provider,
            self.model,
        )
        self.state: dict[str, Any] = {
            "run_id": run_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "ticket": ticket,
            "sprint": sprint,
            "status": "created",
            "created_at": utc_now(),
            "response_ids": [],
            "reservations": [],
            "tool_rounds": 0,
            "cost_usd": "0",
        }
        atomic_json(self.state_path, self.state)

    def _save(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_path, self.state)

    def _count(self, body: dict[str, Any]) -> int:
        if self.provider in {"azure_adm", "bedrock_mantle"}:
            # Chat Completions routes do not expose a separate token-counting
            # endpoint. One UTF-8 byte per token is a deliberately conservative
            # upper bound for pre-submit budget reservation.
            count_body = dict(body)
            count_body.pop("max_completion_tokens", None)
            count_body.pop("max_tokens", None)
            return max(
                1,
                len(
                    json.dumps(
                        count_body, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
                ),
            )
        count_body = dict(body)
        if self.provider == "bedrock":
            count_body.pop("modelId", None)
            count_body.pop("inferenceConfig", None)
            count_body.pop("requestMetadata", None)
            # Current Claude 5 and GPT-5.6 model cards both mark CountTokens
            # unsupported on bedrock-runtime. One UTF-8 byte per token is a
            # deliberately conservative pre-submit budget estimate; settlement
            # still uses the provider's exact response usage.
            return max(
                1,
                len(
                    json.dumps(
                        count_body,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ),
            )
        if self.provider == "anthropic":
            count_body.pop("max_tokens", None)
            endpoint = "messages/count_tokens"
        else:
            count_body.pop("max_output_tokens", None)
            endpoint = "responses/input_tokens"
        attempts = self.budgets["max_pre_ack_retries"] + 1
        for attempt in range(attempts):
            try:
                result = self.transport.request(self.provider, endpoint, count_body)
                break
            except (ProviderAmbiguous, ProviderHTTPError) as exc:
                retryable = isinstance(exc, ProviderAmbiguous) or exc.status in {
                    429,
                    500,
                    502,
                    503,
                    504,
                    529,
                }
                if not retryable or attempt + 1 >= attempts:
                    raise
                time.sleep(self.budgets["retry_backoff_seconds"] * (attempt + 1))
        count = int(result.get("input_tokens") or 0)
        if count <= 0:
            raise AgentError("provider token counter returned no input token count")
        return count

    def _submit(self, body: dict[str, Any]) -> dict[str, Any]:
        input_tokens = self._count(body)
        output_cap = (
            int(body.get("max_tokens") or 0)
            if self.provider == "anthropic"
            else int((body.get("inferenceConfig") or {}).get("maxTokens") or 0)
            if self.provider == "bedrock"
            else int(body.get("max_completion_tokens") or 0)
            if self.provider == "azure_adm"
            else int(body.get("max_tokens") or 0)
            if self.provider == "bedrock_mantle"
            else int(body.get("max_output_tokens") or 0)
        )
        projected = self.pricing.worst_case(input_tokens, output_cap)
        reservation = self.ledger.reserve(
            projected=projected,
            limits=self.budgets,
            logical_review_id=getattr(self, "logical_review_id", None),
            run_id=self.run_id,
            ticket=self.ticket,
            sprint=self.sprint,
            provider=self.provider,
            model=self.model,
            role=self.role,
        )
        self.state["reservations"].append(reservation)
        self._save(
            status="pending_submission",
            pending_reservation=reservation,
            pending_request=body,
            projected_cost_usd=str(projected),
        )
        endpoint = {
            "anthropic": "messages",
            "azure_adm": "chat/completions",
            "bedrock": "converse",
            "bedrock_mantle": "chat/completions",
        }.get(self.provider, "responses")
        response = None
        latency_ms = None
        rate_limit_retries = 0
        overload_retries = 0
        rate_limit_wait = 0.0
        try:
            while True:
                try:
                    # Measure only the accepted attempt. Retry sleeps are
                    # reported separately so blocked time never inflates the
                    # provider's own latency.
                    request_started = time.monotonic()
                    response = self.transport.request(
                        self.provider, endpoint, body, idempotency_key=reservation
                    )
                    latency_ms = int((time.monotonic() - request_started) * 1000)
                    break
                except ProviderHTTPError as exc:
                    # A returned rate-limit/overload rejection is known not to
                    # have started model work. Retry only those explicit cases,
                    # preserving the same idempotency key and reservation.
                    if exc.status == 429:
                        if rate_limit_retries >= self.budgets["max_rate_limit_retries"]:
                            raise
                        if exc.retry_after_seconds is not None:
                            delay = exc.retry_after_seconds
                        else:
                            base = self.budgets["retry_backoff_seconds"] * (
                                2**rate_limit_retries
                            )
                            delay = min(base, self.budgets["retry_max_backoff_seconds"])
                            if delay:
                                delay += random.uniform(0, min(1.0, delay * 0.1))
                        if (
                            rate_limit_wait + delay
                            > self.budgets["max_rate_limit_wait_seconds"]
                        ):
                            raise
                        rate_limit_retries += 1
                        rate_limit_wait += delay
                        self._save(
                            retry_count=rate_limit_retries,
                            last_retry_status=exc.status,
                            last_retry_delay_seconds=round(delay, 3),
                            total_rate_limit_wait_seconds=round(rate_limit_wait, 3),
                        )
                        time.sleep(delay)
                        continue
                    if (
                        exc.status != 529
                        or overload_retries >= self.budgets["max_pre_ack_retries"]
                    ):
                        raise
                    overload_retries += 1
                    delay = self.budgets["retry_backoff_seconds"] * overload_retries
                    self._save(
                        overload_retry_count=overload_retries,
                        last_retry_status=exc.status,
                        last_retry_delay_seconds=delay,
                    )
                    time.sleep(delay)
        except ProviderAdmissionError as exc:
            self.ledger.release(
                reservation,
                self.run_id,
                "shared provider admission refused before submission",
            )
            self._save(
                status="rejected",
                pending_reservation=None,
                pending_request=None,
                error=str(exc),
            )
            raise
        except ProviderHTTPError as exc:
            if (
                400 <= exc.status < 500 and exc.status not in {408, 409}
            ) or exc.status == 529:
                self.ledger.release(
                    reservation, self.run_id, f"provider rejected HTTP {exc.status}"
                )
                self._save(
                    status="rejected",
                    pending_reservation=None,
                    pending_request=None,
                    error=str(exc),
                )
            else:
                self._save(status="needs_reconcile", error=str(exc))
            raise
        except ProviderAmbiguous as exc:
            self._save(status="needs_reconcile", error=str(exc))
            raise
        if response is None:
            raise ProviderAmbiguous("provider retry loop ended without a response")
        response_id = str(response.get("id") or "")
        if not response_id:
            self._save(status="needs_reconcile", error="provider response had no id")
            raise ProviderAmbiguous("provider response had no durable id")
        usage = normalize_usage(self.provider, response)
        if (
            sum(
                usage[key]
                for key in (
                    "input_tokens",
                    "cache_write_tokens",
                    "cache_read_tokens",
                    "output_tokens",
                )
            )
            <= 0
        ):
            self.state["response_ids"].append(response_id)
            self._save(
                status="needs_reconcile",
                last_response_id=response_id,
                error="provider response omitted billable usage",
            )
            raise ProviderAmbiguous("provider response omitted billable usage")
        cost = self.pricing.actual_cost(usage)
        self.ledger.settle(
            reservation,
            run_id=self.run_id,
            ticket=self.ticket,
            sprint=self.sprint,
            provider=self.provider,
            model=self.model,
            response_id=response_id,
            usage=usage,
            cost=cost,
            role=self.role,
            latency_ms=latency_ms,
            rate_limit_wait_seconds=round(rate_limit_wait, 3)
            if rate_limit_wait
            else None,
            tool_round=int(self.state.get("tool_rounds") or 0),
        )
        cumulative = decimal_value(self.state.get("cost_usd", "0"), "state cost") + cost
        self.state["response_ids"].append(response_id)
        self._save(
            status="submitted",
            pending_reservation=None,
            pending_request=None,
            cost_usd=str(cumulative),
            last_usage=usage,
            last_response_id=response_id,
        )
        return response

    def _execute_calls(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        allowed = {
            str((tool.get("function") or {}).get("name") or "")
            if self.provider in {"azure_adm", "bedrock_mantle"}
            else str((tool.get("toolSpec") or {}).get("name") or "")
            if self.provider == "bedrock"
            else str(tool.get("name") or "")
            for tool in self.tools
        }
        for call in calls:
            name = str(call.get("name") or "")
            call_id = str(call.get("id") or call.get("call_id") or "")
            if self.provider in {"anthropic", "bedrock"}:
                arguments = call.get("input") or {}
            else:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            is_error = False
            try:
                if name not in allowed:
                    raise AgentError(f"tool {name!r} is not allowed for {self.role}")
                if not isinstance(arguments, dict):
                    raise AgentError("tool arguments must be an object")
                output = self.tool_executor.execute(name, arguments)
            except (AgentError, OSError, ValueError) as exc:
                is_error = True
                output = f"ERROR: {exc}"
            if self.provider == "anthropic":
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": output,
                        "is_error": is_error,
                    }
                )
            elif self.provider == "bedrock":
                results.append(
                    {
                        "toolResult": {
                            "toolUseId": call_id,
                            "content": [{"text": output}],
                            "status": "error" if is_error else "success",
                        }
                    }
                )
            elif self.provider in {"azure_adm", "bedrock_mantle"}:
                results.append(
                    {"role": "tool", "tool_call_id": call_id, "content": output}
                )
            else:
                results.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call.get("call_id") or call_id),
                        "output": output,
                    }
                )
        return results

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        self._active_review_head = ""
        try:
            return self._run(request)
        finally:
            # Only known terminal outcomes release the phase permit. Transport
            # ambiguity and asynchronous provider responses remain fenced.
            if (
                self._active_review_head
                and not self.state.get("pending_reservation")
                and self.state.get("status")
                in {
                    "ready",
                    "tool_running",
                    "rejected",
                    "budget_blocked",
                    "invalid_output",
                    "incomplete",
                }
            ):
                cancel_review_permit(
                    shared_root=self.shared_root,
                    ledger_dir=str(
                        self.config.get("review_ledger_dir")
                        or ".orchestration/.review-ledger"
                    ),
                    pr=str(self.review_pr),
                    token=str(self.review_authorization),
                    role=self.role,
                    head=self._active_review_head,
                    timestamp=utc_now(),
                )

    def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.provider == "openai":
            strip_openai_cache_request_fields(request)
        request_model = (
            request.get("modelId")
            if self.provider == "bedrock"
            else request.get("model")
        )
        if str(request_model or "") != self.model:
            raise AgentError("request model does not match the resolved role route")
        cap_key = (
            "max_tokens"
            if self.provider == "anthropic"
            else "maxTokens"
            if self.provider == "bedrock"
            else "max_completion_tokens"
            if self.provider == "azure_adm"
            else "max_tokens"
            if self.provider == "bedrock_mantle"
            else "max_output_tokens"
        )
        requested_cap = int(
            ((request.get("inferenceConfig") or {}).get(cap_key) or 0)
            if self.provider == "bedrock"
            else request.get(cap_key) or 0
        )
        if requested_cap <= 0:
            raise AgentError(f"request requires a positive {cap_key}")
        if self.provider == "bedrock":
            request.setdefault("inferenceConfig", {})[cap_key] = min(
                requested_cap, self.budgets["max_output_tokens_per_turn"]
            )
            request["toolConfig"] = {"tools": self.tools, "toolChoice": {"auto": {}}}
        else:
            request[cap_key] = min(
                requested_cap, self.budgets["max_output_tokens_per_turn"]
            )
            request["tools"] = self.tools
        if self.provider == "anthropic":
            request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
            request.pop("parallel_tool_calls", None)
        elif self.provider != "bedrock":
            request["tool_choice"] = "auto"
            request["parallel_tool_calls"] = False
        reviewer_roles = {"design-reviewer", "code-reviewer", "security-reviewer"}
        if self.role in reviewer_roles:
            if not self.review_authorization or not self.review_pr:
                raise AgentError(
                    "reviewer run requires --review-pr and a ledger-issued --review-authorization"
                )
            try:
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise AgentError(
                    "cannot bind review authorization to repository HEAD"
                ) from exc
            try:
                self.logical_review_id = consume_review_permit(
                    shared_root=self.shared_root,
                    ledger_dir=str(
                        self.config.get("review_ledger_dir")
                        or ".orchestration/.review-ledger"
                    ),
                    pr=self.review_pr,
                    token=self.review_authorization,
                    role=self.role,
                    head=head,
                    timestamp=utc_now(),
                )
            except ReviewPermitError as exc:
                raise AgentError(str(exc)) from exc
            self._active_review_head = head
        self._save(status="ready", request=request)
        body = request
        transcript: list[dict[str, Any]] = []
        while True:
            try:
                response = self._submit(body)
            except BudgetError as exc:
                self._save(status="budget_blocked", error=str(exc))
                raise
            calls = tool_calls(self.provider, response)
            text = response_text(self.provider, response)
            transcript.append(
                {
                    "response_id": response.get("id"),
                    "stop_reason": (
                        ((response.get("choices") or [{}])[0].get("finish_reason"))
                        if self.provider in {"azure_adm", "bedrock_mantle"}
                        else response.get("stopReason")
                        if self.provider == "bedrock"
                        else response.get("stop_reason") or response.get("status")
                    ),
                    "text": text,
                    "tool_calls": [
                        {
                            "name": call.get("name"),
                            "id": call.get("id") or call.get("call_id"),
                        }
                        for call in calls
                    ],
                }
            )
            self._save(transcript=transcript)
            if not calls:
                status = "completed"
                if self.provider == "anthropic" and response.get("stop_reason") not in {
                    "end_turn",
                    "stop_sequence",
                }:
                    status = "incomplete"
                if self.provider == "bedrock" and response.get("stopReason") not in {
                    "end_turn",
                    "stop_sequence",
                }:
                    status = "incomplete"
                if self.provider == "openai" and response.get("status") != "completed":
                    status = (
                        "incomplete"
                        if response.get("status")
                        in {"incomplete", "failed", "cancelled"}
                        else "needs_reconcile"
                    )
                if self.provider in {"azure_adm", "bedrock_mantle"}:
                    choices = response.get("choices") or []
                    if not choices or choices[0].get("finish_reason") != "stop":
                        status = "incomplete"
                review = None
                review_gate = {
                    "code-reviewer": "code-review",
                    "security-reviewer": "security-review",
                }.get(self.role)
                if status == "completed" and review_gate:
                    text = review_text(self.provider, text)
                    try:
                        review = context_pipeline.validate_review_output(
                            json.loads(text), review_gate
                        )
                    except (json.JSONDecodeError, context_pipeline.ContextError) as exc:
                        self._save(
                            status="invalid_output", output_text=text, error=str(exc)
                        )
                        raise AgentError(
                            f"reviewer returned invalid structured output: {exc}"
                        ) from exc
                if status == "completed" and self.role in reviewer_roles:
                    completed_result: Any
                    try:
                        completed_result = json.loads(review_text(self.provider, text))
                    except json.JSONDecodeError as exc:
                        self._save(
                            status="invalid_output", output_text=text, error=str(exc)
                        )
                        raise AgentError(
                            "reviewer completion output must be structured JSON"
                        ) from exc
                    try:
                        complete_review_permit(
                            shared_root=self.shared_root,
                            ledger_dir=str(
                                self.config.get("review_ledger_dir")
                                or ".orchestration/.review-ledger"
                            ),
                            pr=str(self.review_pr),
                            token=str(self.review_authorization),
                            role=self.role,
                            head=subprocess.run(
                                ["git", "rev-parse", "HEAD"],
                                cwd=self.root,
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            .stdout.strip()
                            .lower(),
                            result=completed_result,
                            timestamp=utc_now(),
                        )
                    except (
                        ReviewPermitError,
                        OSError,
                        subprocess.CalledProcessError,
                    ) as exc:
                        self._save(
                            status="invalid_output", output_text=text, error=str(exc)
                        )
                        raise AgentError(
                            f"could not create review completion receipt: {exc}"
                        ) from exc
                self._save(status=status, output_text=text, review=review)
                result = {
                    "run_id": self.run_id,
                    "status": status,
                    "provider": self.provider,
                    "model": self.model,
                    "role": self.role,
                    "output_text": text,
                    "cost_usd": self.state["cost_usd"],
                    "usage": self.ledger.summary(),
                    "state": str(self.state_path),
                }
                if review is not None:
                    result["review"] = review
                return result
            rounds = int(self.state["tool_rounds"])
            if rounds >= self.budgets["max_tool_rounds"]:
                error = f"max_tool_rounds ({self.budgets['max_tool_rounds']}) reached"
                self._save(status="budget_blocked", error=error)
                raise BudgetError(error)
            results = self._execute_calls(calls)
            self._save(status="tool_running", tool_rounds=rounds + 1)
            if self.provider == "anthropic":
                messages = list(body.get("messages") or [])
                messages.append(
                    {"role": "assistant", "content": response.get("content") or []}
                )
                messages.append({"role": "user", "content": results})
                body = dict(body)
                body["messages"] = roll_conversation_cache_breakpoint(messages)
            elif self.provider == "bedrock":
                message = (response.get("output") or {}).get("message") or {}
                messages = list(body.get("messages") or [])
                messages.append(message)
                messages.append({"role": "user", "content": results})
                body = {
                    key: body[key]
                    for key in (
                        "modelId",
                        "inferenceConfig",
                        "system",
                        "toolConfig",
                        "additionalModelRequestFields",
                    )
                    if key in body
                }
                body["messages"] = (
                    roll_bedrock_cache_breakpoint(messages)
                    if context_pipeline.bedrock_model_family(self.model) == "anthropic"
                    else messages
                )
            elif self.provider in {"azure_adm", "bedrock_mantle"}:
                choices = response.get("choices") or []
                assistant = dict((choices[0].get("message") or {}) if choices else {})
                messages = list(body.get("messages") or [])
                messages.append(
                    {
                        key: assistant[key]
                        for key in ("role", "content", "tool_calls")
                        if key in assistant
                    }
                )
                messages.extend(results)
                body = {
                    key: body[key]
                    for key in (
                        "model",
                        "max_completion_tokens",
                        "max_tokens",
                        "tools",
                        "tool_choice",
                        "parallel_tool_calls",
                    )
                    if key in body
                }
                body["messages"] = messages
            else:
                keep = {
                    key: body[key]
                    for key in (
                        "model",
                        "max_output_tokens",
                        "tools",
                        "tool_choice",
                        "parallel_tool_calls",
                        "reasoning",
                        "text",
                    )
                    if key in body
                }
                keep["previous_response_id"] = response["id"]
                keep["input"] = results
                body = keep


def read_request(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise AgentError("request payload must be a JSON object")
    return value


RELATIVE_WINDOW = re.compile(r"^(\d+)([mhdw])$")
WINDOW_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
TOKEN_FIELDS = (
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "reasoning_tokens",
)
GROUP_FIELDS = ("role", "model", "provider", "ticket", "sprint", "run_id", "day")


def parse_window(value: str) -> dt.datetime:
    """Accept `30m`, `24h`, `7d`, `2w`, or an absolute ISO 8601 timestamp."""
    match = RELATIVE_WINDOW.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=amount * WINDOW_SECONDS[unit]
        )
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise AgentError(
            f"could not read time window {value!r}; use 7d, 24h, 30m, 2w, or an ISO 8601 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def event_time(event: dict[str, Any]) -> dt.datetime | None:
    raw = str(event.get("timestamp") or "")
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile. Small samples are the norm here, so no interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def group_key(event: dict[str, Any], field: str) -> str:
    if field == "day":
        moment = event_time(event)
        return moment.date().isoformat() if moment else "unknown"
    return str(event.get(field) or "unknown")


def aggregate(events: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = {
        field: sum(int(event.get(field) or 0) for event in events)
        for field in TOKEN_FIELDS
    }
    cost = sum(
        (decimal_value(event.get("cost_usd", 0), "ledger cost") for event in events),
        Decimal("0"),
    )
    billed_input = (
        tokens["input_tokens"]
        + tokens["cache_read_tokens"]
        + tokens["cache_write_tokens"]
    )
    latencies = [
        int(event["latency_ms"])
        for event in events
        if event.get("latency_ms") is not None
    ]
    waits = sum(float(event.get("rate_limit_wait_seconds") or 0) for event in events)
    requests = len(events)
    result: dict[str, Any] = {
        "requests": requests,
        "runs": len({str(event.get("run_id") or "") for event in events}),
        "cost_usd": str(cost.quantize(Decimal("0.000001"))),
        **tokens,
        # The headline optimization metric: share of billed input served from cache.
        "cache_hit_rate": round(tokens["cache_read_tokens"] / billed_input, 4)
        if billed_input
        else None,
        "cost_per_request_usd": str((cost / requests).quantize(Decimal("0.000001")))
        if requests
        else None,
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "rate_limit_wait_seconds": round(waits, 3) if waits else 0,
        "latency_samples": len(latencies),
    }
    return result


def build_report(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    ledger = UsageLedger(root)
    events = ledger._events()
    since = parse_window(args.since) if args.since else None
    until = parse_window(args.until) if args.until else None

    def in_window(event: dict[str, Any]) -> bool:
        moment = event_time(event)
        if moment is None:
            return since is None and until is None
        if since and moment < since:
            return False
        return not (until and moment > until)

    filters = {
        field: getattr(args, field)
        for field in ("role", "model", "provider", "ticket", "sprint")
        if getattr(args, field, None)
    }

    def matches(event: dict[str, Any]) -> bool:
        return all(
            str(event.get(field) or "") == value for field, value in filters.items()
        )

    usage_events = [
        event
        for event in events
        if event.get("kind") == "usage" and in_window(event) and matches(event)
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in usage_events:
        grouped.setdefault(group_key(event, args.group_by), []).append(event)
    groups = [{"key": key, **aggregate(items)} for key, items in grouped.items()]
    if args.group_by == "day":
        groups.sort(key=lambda row: row["key"])
    else:
        groups.sort(key=lambda row: Decimal(row["cost_usd"]), reverse=True)
    hidden = 0
    if args.top and len(groups) > args.top:
        hidden = len(groups) - args.top
        groups = groups[: args.top]

    outcomes: dict[str, int] = {}
    for state_file in sorted((root / ".orchestration" / ".llm-runs").glob("*.json")):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        created = state.get("created_at")
        probe = {"timestamp": created, **{key: state.get(key) for key in filters}}
        if not (in_window(probe) and matches(probe)):
            continue
        status = str(state.get("status") or "unknown")
        outcomes[status] = outcomes.get(status, 0) + 1

    _, open_items = ledger._totals(events)
    return {
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "filters": filters or None,
        "group_by": args.group_by,
        "groups": groups,
        "groups_hidden_by_top": hidden,
        "totals": aggregate(usage_events),
        "run_outcomes": outcomes,
        "open_reservations": list(open_items.values()),
        "ledger": str(ledger.path),
    }


def format_report(report: dict[str, Any]) -> str:
    columns = [
        ("key", report["group_by"], 22, "<"),
        ("requests", "reqs", 6, ">"),
        ("runs", "runs", 5, ">"),
        ("cost_usd", "cost_usd", 11, ">"),
        ("input_tokens", "in_tok", 10, ">"),
        ("cache_read_tokens", "cache_rd", 10, ">"),
        ("cache_write_tokens", "cache_wr", 10, ">"),
        ("output_tokens", "out_tok", 9, ">"),
        ("cache_hit_rate", "cache_hit", 10, ">"),
        ("latency_p50_ms", "p50_ms", 8, ">"),
        ("latency_p95_ms", "p95_ms", 8, ">"),
    ]

    def cell(row: dict[str, Any], field: str) -> str:
        value = row.get(field)
        if value is None:
            return "-"
        if field == "cache_hit_rate":
            return f"{value * 100:.1f}%"
        if field == "cost_usd":
            return f"${Decimal(value).quantize(Decimal('0.0001'))}"
        if field.endswith("_tokens") and isinstance(value, int):
            if value >= 1_000_000:
                return f"{value / 1_000_000:.2f}M"
            if value >= 10_000:
                return f"{value / 1000:.1f}k"
        return str(value)

    def line(row: dict[str, Any]) -> str:
        return "  ".join(
            f"{cell(row, f):{align}{width}}" for f, _, width, align in columns
        )

    header = "  ".join(f"{label:{align}{width}}" for _, label, width, align in columns)
    lines = [header, "-" * len(header)]
    lines.extend(line(row) for row in report["groups"])
    if not report["groups"]:
        lines.append("(no usage recorded in this window)")
    lines.append("-" * len(header))
    lines.append(line({**report["totals"], "key": "TOTAL"}))
    if report.get("groups_hidden_by_top"):
        lines.append(
            f"({report['groups_hidden_by_top']} further groups hidden by --top; "
            "TOTAL covers every group in the window)"
        )
    window = report["window"]
    if window["since"] or window["until"]:
        lines.append("")
        lines.append(
            f"window: {window['since'] or 'start'} -> {window['until'] or 'now'}"
        )
    if report["run_outcomes"]:
        outcomes = ", ".join(
            f"{name} {count}" for name, count in sorted(report["run_outcomes"].items())
        )
        lines.append(f"run outcomes: {outcomes}")
    if report["open_reservations"]:
        lines.append(
            f"open reservations: {len(report['open_reservations'])} "
            "(reconcile before trusting spend totals)"
        )
    waits = report["totals"].get("rate_limit_wait_seconds") or 0
    if waits:
        lines.append(f"time blocked on provider rate limits: {waits}s")
    samples = report["totals"].get("latency_samples") or 0
    if samples < report["totals"].get("requests", 0):
        lines.append(
            f"latency recorded for {samples} of {report['totals']['requests']} requests "
            "(older ledger entries predate latency capture)"
        )
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="run a provider payload through the constrained tool loop"
    )
    run.add_argument(
        "--request", required=True, help="provider payload JSON file, or - for stdin"
    )
    run.add_argument("--config", default=".orchestration/config.yaml")
    run.add_argument("--role", required=True)
    run.add_argument("--repo", default=".")
    run.add_argument("--ticket")
    run.add_argument("--sprint")
    run.add_argument("--run-id")
    run.add_argument("--result")
    run.add_argument("--review-authorization")
    run.add_argument("--review-pr")
    run.add_argument("--attempt-capability")
    run.add_argument("--worker-ref")
    usage = commands.add_parser(
        "usage", help="summarize durable API usage and open reservations"
    )
    usage.add_argument("--repo", default=".")
    report = commands.add_parser(
        "report", help="group durable API usage into cost and performance insights"
    )
    report.add_argument("--repo", default=".")
    report.add_argument("--group-by", choices=list(GROUP_FIELDS), default="role")
    report.add_argument("--since", help="7d, 24h, 30m, 2w, or an ISO 8601 timestamp")
    report.add_argument("--until", help="7d, 24h, 30m, 2w, or an ISO 8601 timestamp")
    report.add_argument("--role")
    report.add_argument("--model")
    report.add_argument("--provider")
    report.add_argument("--ticket")
    report.add_argument("--sprint")
    report.add_argument("--top", type=int, help="keep only the N costliest groups")
    report.add_argument("--format", choices=["table", "json"], default="table")
    reconcile = commands.add_parser(
        "reconcile",
        help="close an uncertain reservation after checking provider records",
    )
    reconcile.add_argument("--repo", default=".")
    reconcile.add_argument("--config", default=".orchestration/config.yaml")
    reconcile.add_argument("--run-id", required=True)
    reconcile.add_argument(
        "--outcome", choices=["not-found", "completed"], required=True
    )
    reconcile.add_argument("--evidence", required=True)
    reconcile.add_argument("--response-id")
    reconcile.add_argument("--input-tokens", type=int, default=0)
    reconcile.add_argument("--cache-write-tokens", type=int, default=0)
    reconcile.add_argument("--cache-read-tokens", type=int, default=0)
    reconcile.add_argument("--output-tokens", type=int, default=0)
    migration_plan = commands.add_parser(
        "reservation-migration-plan",
        help="emit an evidence manifest template for every open reservation",
    )
    migration_plan.add_argument("--repo", default=".")
    migration = commands.add_parser(
        "reconcile-reservations",
        help="validate or apply provider-confirmed not-found reservation outcomes",
    )
    migration.add_argument("--repo", default=".")
    migration.add_argument("--manifest", required=True)
    migration.add_argument("--apply", action="store_true")
    return result


def reconcile_run(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", args.run_id):
        raise AgentError("invalid run id")
    state_path = runtime_path(root, ".orchestration/.llm-runs") / f"{args.run_id}.json"
    if not state_path.is_file():
        raise AgentError(f"run state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "needs_reconcile" or not state.get("pending_reservation"):
        raise AgentError(
            "only a needs_reconcile run with an open reservation can be reconciled"
        )
    reservation = str(state["pending_reservation"])
    ledger = UsageLedger(root)
    if args.outcome == "not-found":
        ledger.release(
            reservation,
            args.run_id,
            f"provider lookup found no request: {args.evidence}",
        )
        status = "reconciled_not_found"
        cost = Decimal("0")
    else:
        if not args.response_id:
            raise AgentError("--response-id is required for a completed reconciliation")
        usage = {
            "input_tokens": int_value(args.input_tokens, "input tokens", minimum=0),
            "cache_write_tokens": int_value(
                args.cache_write_tokens, "cache write tokens", minimum=0
            ),
            "cache_read_tokens": int_value(
                args.cache_read_tokens, "cache read tokens", minimum=0
            ),
            "output_tokens": int_value(args.output_tokens, "output tokens", minimum=0),
            "reasoning_tokens": 0,
        }
        if sum(usage.values()) <= 0:
            raise AgentError("completed reconciliation requires nonzero provider usage")
        pricing = Pricing.from_config(
            load_yaml(Path(args.config)), str(state.get("model") or "")
        )
        cost = pricing.actual_cost(usage)
        ledger.settle(
            reservation,
            run_id=args.run_id,
            ticket=state.get("ticket"),
            sprint=state.get("sprint"),
            provider=str(state.get("provider")),
            model=str(state.get("model")),
            response_id=args.response_id,
            usage=usage,
            cost=cost,
            role=state.get("role"),
        )
        status = "reconciled_completed"
        state.setdefault("response_ids", []).append(args.response_id)
    state.update(
        {
            "status": status,
            "pending_reservation": None,
            "pending_request": None,
            "reconciliation_evidence": args.evidence,
            "reconciled_at": utc_now(),
        }
    )
    atomic_json(state_path, state)
    return {
        "run_id": args.run_id,
        "status": status,
        "cost_usd": str(cost),
        "state": str(state_path),
        "usage": ledger.summary(),
    }


def reservation_migration_plan(root: Path) -> dict[str, Any]:
    """Produce a bounded manifest template without releasing uncertain work."""
    ledger = UsageLedger(root)
    open_items = sorted(
        ledger.summary()["open_reservations"],
        key=lambda item: (
            str(item.get("run_id") or ""),
            str(item.get("reservation_id") or ""),
        ),
    )
    return {
        "schema_version": 1,
        "repository": str(root.resolve()),
        "entries": [
            {
                "run_id": str(item.get("run_id") or ""),
                "reservation_id": str(item.get("reservation_id") or ""),
                "outcome": "not-found",
                "evidence": "",
            }
            for item in open_items
        ],
    }


def reconcile_reservation_manifest(
    args: argparse.Namespace, root: Path
) -> dict[str, Any]:
    """Bulk-apply provider-confirmed not-found outcomes with an audit trail."""
    root = root.resolve()
    manifest_path = Path(args.manifest).resolve()
    if manifest_path != root and root not in manifest_path.parents:
        raise AgentError(
            "reservation reconciliation manifest must be inside the repository"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("repository") != str(
        root.resolve()
    ):
        raise AgentError(
            "reservation reconciliation manifest has the wrong schema or repository"
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise AgentError(
            "reservation reconciliation manifest requires 1 through 100 entries"
        )
    ledger = UsageLedger(root)
    open_items = {
        str(item.get("reservation_id") or ""): item
        for item in ledger.summary()["open_reservations"]
    }
    validated = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AgentError("reservation reconciliation entries must be objects")
        run_id = str(entry.get("run_id") or "")
        reservation_id = str(entry.get("reservation_id") or "")
        evidence = str(entry.get("evidence") or "").strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id)
            or not re.fullmatch(r"resv_[a-f0-9]{32}", reservation_id)
            or entry.get("outcome") != "not-found"
            or not evidence
            or len(evidence) > 2000
        ):
            raise AgentError(
                "each manifest entry requires a valid run, reservation, "
                "not-found outcome, and evidence"
            )
        if reservation_id in seen:
            raise AgentError(f"duplicate reservation in manifest: {reservation_id}")
        seen.add(reservation_id)
        state_path = runtime_path(root, ".orchestration/.llm-runs") / f"{run_id}.json"
        if not state_path.is_file():
            raise AgentError(f"run state not found: {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        already_done = (
            state.get("status") == "reconciled_not_found"
            and state.get("pending_reservation") is None
            and state.get("reconciliation_evidence") == evidence
        )
        if not already_done and (
            state.get("status") != "needs_reconcile"
            or state.get("pending_reservation") != reservation_id
            or open_items.get(reservation_id, {}).get("run_id") != run_id
        ):
            raise AgentError(
                f"run {run_id} is not bound to open reservation {reservation_id}"
            )
        validated.append(
            (run_id, reservation_id, evidence, state_path, state, already_done)
        )
    applied = []
    if args.apply:
        for (
            run_id,
            reservation_id,
            evidence,
            state_path,
            state,
            already_done,
        ) in validated:
            if not already_done:
                ledger.release(
                    reservation_id,
                    run_id,
                    f"provider lookup found no request: {evidence}",
                )
                state.update(
                    {
                        "status": "reconciled_not_found",
                        "pending_reservation": None,
                        "pending_request": None,
                        "reconciliation_evidence": evidence,
                        "reconciled_at": utc_now(),
                    }
                )
                atomic_json(state_path, state)
            applied.append(run_id)
    return {
        "status": "completed",
        "mode": "apply" if args.apply else "validate",
        "validated_runs": [item[0] for item in validated],
        "applied_runs": applied,
        "usage": ledger.summary(),
    }


def main() -> int:
    args = parser().parse_args()
    try:
        root = Path(args.repo).resolve()
        if args.command == "usage":
            output = UsageLedger(shared_repository_root(root)).summary()
        elif args.command == "report":
            output = build_report(shared_repository_root(root), args)
        elif args.command == "reconcile":
            output = reconcile_run(args, shared_repository_root(root))
        elif args.command == "reservation-migration-plan":
            output = reservation_migration_plan(shared_repository_root(root))
        elif args.command == "reconcile-reservations":
            output = reconcile_reservation_manifest(args, shared_repository_root(root))
        else:
            run_id = args.run_id or "run_" + uuid.uuid4().hex
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
                raise AgentError(
                    "run id must contain only letters, digits, dot, underscore, or hyphen"
                )
            agent = ApiAgent(
                root=root,
                config_path=Path(args.config),
                role=args.role,
                ticket=args.ticket,
                sprint=args.sprint,
                run_id=run_id,
                transport=None,
                review_authorization=args.review_authorization,
                review_pr=args.review_pr,
                attempt_capability=args.attempt_capability,
                worker_ref=args.worker_ref,
            )
            output = agent.run(read_request(args.request))
        if getattr(args, "result", None):
            atomic_json(Path(args.result), output)
        if args.command == "report" and args.format == "table":
            print(format_report(output))
        else:
            print(json.dumps(output, indent=2, sort_keys=False))
        status = output.get("status", "completed")
        return (
            0
            if status in {"completed", "reconciled_not_found", "reconciled_completed"}
            else 3
        )
    except (AgentError, OSError, json.JSONDecodeError) as exc:
        print(f"api-agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
