#!/usr/bin/env python3
"""Host-neutral, resumable state machine for sprint orchestration.

Claude and Codex adapters query Jira and launch ticket workflows. This script
owns the shared safety-critical parts: dependency normalization, priority-ordered
readiness, bounded lane reservation, atomic checkpoints, restart reconciliation,
and exact summaries.
"""

from __future__ import annotations

from slice_delivery import (
    decomposition_provenance,
    required_contract_names,
    validate_contracts,
    validate_delivery,
    validate_owner,
)

import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from api_agent import (
    AgentError,
    Pricing,
    UsageLedger,
    budgets_from_config,
    load_yaml,
    TRANSIENT_PAUSE_REASONS,
    load_orchestration_env,
)
from provider_health import (
    bind_native_working_directory,
    ProviderHealth,
    HealthError,
    desktop_subscription_status,
    model_less_desktop_route,
    probe,
    route_identity,
    subscription_child_environment,
    subscription_launch_command,
    validate_native_command,
)
from context_pipeline import ContextError, llm_route_from_config

from operator_authority import (
    AuthorityError,
    activate_budget,
    activate_relaunch,
    restart_grant as host_restart_grant,
    budget_ceiling as authorized_budget_ceiling,
    consume_recovery,
    relaunch_ceiling as authorized_relaunch_ceiling,
)

from runtime_state import (
    RuntimeStateError,
    canonical_config_path,
    migrate_legacy_runtime_dir,
    shared_repository_root,
    working_repository_root,
)


SCHEMA_VERSION = 2
TERMINAL = {
    "completed",
    "blocked",
    "decomposed",
    "external_blocked",
    "operator_decision",
    "user_action",
}
AUTONOMOUS_INTERVENTIONS = {"needs_decomposition", "needs_repair", "recoverable"}
OUTCOMES = TERMINAL | AUTONOMOUS_INTERVENTIONS
PROGRESS_MILESTONES = {
    "design_passed",
    "failing_test",
    "tests_repaired",
    "implementation_commit",
    "pr_opened",
    "ci_advanced",
    "review_finding_closed",
}
DESIGN_REVIEWER_ROLES = {"design-reviewer"}
POST_IMPLEMENTATION_REVIEWER_ROLES = {"code-reviewer", "security-reviewer"}
DEFAULT_DONE = ["done", "closed", "resolved"]
DEFAULT_BLOCKED = ["blocked"]
DEFAULT_READY = ["ready", "to do", "open", "selected for development"]


class SprintError(RuntimeError):
    pass


class ProcessAbsent(SprintError):
    """The OS conclusively reported that a process no longer exists."""


def decision_registry(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return reviewed repository decisions that scopers may reuse."""
    raw = config.get("sprint_decisions") or {}
    if not isinstance(raw, dict) or len(raw) > 100:
        raise SprintError("sprint_decisions must be a map with at most 100 entries")
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key):
            raise SprintError(f"invalid sprint decision key: {key!r}")
        if not isinstance(value, dict):
            raise SprintError(f"sprint decision {key} must be a map")
        status = str(value.get("status") or "").strip().casefold()
        answer = value.get("answer")
        rationale = str(value.get("rationale") or "").strip()
        empty_answer = (
            answer is None
            or (isinstance(answer, str) and not answer.strip())
            or (isinstance(answer, (list, dict)) and not answer)
        )
        if status != "approved" or empty_answer or not rationale:
            raise SprintError(
                f"sprint decision {key} requires status approved, answer, and rationale"
            )
        encoded = json.dumps(answer, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 8000 or len(rationale) > 4000:
            raise SprintError(f"sprint decision {key} exceeds bounded limits")
        result[key] = {
            "status": "approved",
            "answer": answer,
            "rationale": rationale,
        }
    if len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > 32000:
        raise SprintError("sprint_decisions exceeds the 32,000-character context limit")
    return result


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


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


def config_scalar_any_depth(path: Path, key: str, default: str) -> str:
    if not path.exists():
        return default
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(.*?)\s*(?:#.*)?$")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match and match.group(1):
            return unquote(match.group(1))
    return default


def config_bool_any_depth(path: Path, key: str, default: bool) -> bool:
    raw = config_scalar_any_depth(path, key, "true" if default else "false").casefold()
    if raw in {"true", "yes", "1", "on"}:
        return True
    if raw in {"false", "no", "0", "off"}:
        return False
    raise SprintError(f"{key} must be true or false")


def config_list(path: Path, key: str, default: list[str]) -> list[str]:
    """Read a top-level list through the shared engine parser.

    Block lists, one-line flow lists, and Prettier-wrapped flow lists therefore
    resolve identically here and in jira_decomposition, which reads the same
    keys from the parsed configuration.
    """
    if not path.exists():
        return default
    try:
        value = load_yaml(path).get(key)
    except AgentError as exc:
        raise SprintError(str(exc)) from exc
    if not isinstance(value, list):
        return default
    values = [str(item) for item in value if item is not None and str(item) != ""]
    return values or default


def settings(args: argparse.Namespace) -> dict[str, Any]:
    root = project_root()
    shared_root = shared_repository_root(root)
    try:
        config = canonical_config_path(root, args.config)
    except RuntimeStateError as exc:
        raise SprintError(str(exc)) from exc
    try:
        concurrency = int(config_scalar(config, "concurrency_max", "2"))
    except ValueError as exc:
        raise SprintError("concurrency_max must be an integer") from exc
    if concurrency < 1:
        raise SprintError("concurrency_max must be at least 1")
    configured_dir = Path(
        config_scalar(config, "sprint_checkpoint_dir", ".orchestration/.sprint-state")
    )
    if args.state_dir:
        raise SprintError(
            "--state-dir overrides are not allowed; use the canonical repository config"
        )
    requested_dir = configured_dir
    if requested_dir.is_absolute():
        raise SprintError("sprint checkpoint directory must be repository-relative")
    try:
        state_dir = migrate_legacy_runtime_dir(root, requested_dir)
        migrate_legacy_runtime_dir(root, ".orchestration/.llm-usage")
    except RuntimeStateError as exc:
        raise SprintError(str(exc)) from exc
    if state_dir != shared_root and shared_root not in state_dir.parents:
        raise SprintError("sprint checkpoint directory escapes the repository")
    try:
        max_lane_relaunches = int(config_scalar(config, "max_lane_relaunches", "2"))
        max_worker_continuations = int(
            config_scalar(config, "max_worker_continuations", "6")
        )
        max_unmerged_prs = int(
            config_scalar(config, "max_unmerged_prs", str(concurrency))
        )
        legacy_worker_seconds = config_scalar_any_depth(
            config, "max_worker_seconds", ""
        ).strip()
        max_worker_idle_seconds = int(
            legacy_worker_seconds
            or config_scalar(config, "max_worker_idle_seconds", "1800")
        )
        max_worker_lifetime_seconds = int(
            legacy_worker_seconds
            or config_scalar(config, "max_worker_lifetime_seconds", "14400")
        )
    except ValueError as exc:
        raise SprintError(
            "lane, continuation, and work-in-progress limits must be integers"
        ) from exc
    if max_lane_relaunches < 0 or max_worker_continuations < 0:
        raise SprintError("lane relaunch and continuation limits must be at least 0")
    if max_unmerged_prs < 1 or max_unmerged_prs > 20:
        raise SprintError("max_unmerged_prs must be from 1 through 20")
    if legacy_worker_seconds:
        if not 1 <= max_worker_idle_seconds <= 3600:
            raise SprintError("legacy max_worker_seconds must be from 1 through 3600")
    elif (
        not 60 <= max_worker_idle_seconds <= 7200
        or not max_worker_idle_seconds <= max_worker_lifetime_seconds <= 43200
    ):
        raise SprintError(
            "worker idle seconds must be 60..7200 and lifetime must be idle..43200"
        )
    try:
        warning_budget = min(
            float(config_scalar_any_depth(config, "warn_usd_per_ticket", "10")) or 10,
            10,
        )
        pause_budget = min(
            float(config_scalar_any_depth(config, "pause_usd_per_ticket", "20")) or 20,
            20,
        )
        max_model_runs = min(
            int(config_scalar_any_depth(config, "max_model_runs_per_ticket", "12"))
            or 12,
            12,
        )
        max_reviewer_runs = min(
            int(config_scalar_any_depth(config, "max_reviewer_runs_per_ticket", "6"))
            or 6,
            6,
        )
        max_auto_slices = min(
            int(config_scalar_any_depth(config, "max_auto_slices", "6")) or 6,
            10,
        )
        decomposition_threshold = int(
            config_scalar_any_depth(config, "complexity_threshold", "70") or 70
        )
        max_usd_without_progress = min(
            float(config_scalar_any_depth(config, "max_usd_without_progress", "5"))
            or 5,
            10,
        )
    except ValueError as exc:
        raise SprintError("ticket budgets and run limits must be numbers") from exc
    if max_model_runs < 1 or max_reviewer_runs < 1:
        raise SprintError("model and reviewer run limits must be positive")
    if max_auto_slices < 2:
        raise SprintError("max_auto_slices must be at least 2")
    if not 1 <= decomposition_threshold <= 100:
        raise SprintError("complexity_threshold must be from 1 through 100")
    if max_usd_without_progress <= 0:
        raise SprintError("max_usd_without_progress must be positive")
    loaded_config = load_yaml(config)
    decomposition_feature = loaded_config.get("sprint_decomposition") or {}
    if not isinstance(decomposition_feature, dict):
        raise SprintError("sprint_decomposition must be a map")
    contracts = required_contract_names(decomposition_feature, SprintError)
    decisions = decision_registry(loaded_config)
    return {
        "config": config,
        "concurrency_max": concurrency,
        "state_dir": state_dir,
        "shared_root": shared_root,
        "max_lane_relaunches": max_lane_relaunches,
        "max_worker_continuations": max_worker_continuations,
        "max_unmerged_prs": max_unmerged_prs,
        "max_worker_idle_seconds": max_worker_idle_seconds,
        "max_worker_lifetime_seconds": max_worker_lifetime_seconds,
        "legacy_worker_timeout": bool(legacy_worker_seconds),
        "warn_usd_per_ticket": warning_budget,
        "pause_usd_per_ticket": pause_budget,
        "max_model_runs_per_ticket": max_model_runs,
        "max_reviewer_runs_per_ticket": max_reviewer_runs,
        "max_auto_slices": max_auto_slices,
        "decomposition_threshold": decomposition_threshold,
        "required_slice_contracts": contracts,
        "decision_registry": decisions,
        "max_usd_without_progress": max_usd_without_progress,
        "cooperative_auto_recovery": config_bool_any_depth(
            config, "cooperative_auto_recovery", False
        ),
        "auto_decompose_large_tickets": config_bool_any_depth(
            config, "auto_decompose_large_tickets", False
        ),
        "preserved_pr_auto_recovery": config_bool_any_depth(
            config, "preserved_pr_auto_recovery", False
        ),
        "pr_drain_first": config_bool_any_depth(config, "pr_drain_first", True),
        "ready": {
            x.casefold()
            for x in config_list(config, "sprint_ready_statuses", DEFAULT_READY)
        },
        "done": {
            x.casefold()
            for x in config_list(config, "sprint_done_statuses", DEFAULT_DONE)
        },
        "blocked": {
            x.casefold()
            for x in config_list(config, "sprint_blocked_statuses", DEFAULT_BLOCKED)
        },
        "allow_test_evidence": False,
        "runtime_admission": True,
    }


def normalize_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
        raise SprintError(f"invalid Jira ticket key: {value!r}")
    return key


def normalize_priority(value: Any, key: str) -> int | None:
    """Return an explicit integer rank, or None when the ticket carries none.

    Lower sorts first, matching Jira's own convention that priority 1 is the most
    urgent. Priority is optional per ticket: an inventory that omits it entirely
    schedules exactly as before.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SprintError(f"ticket {key} priority must be an integer or omitted")
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise SprintError(
            f"ticket {key} priority must be an integer or omitted"
        ) from exc


def order_key(ticket: dict[str, Any]) -> tuple[int, int, str]:
    """Sort tickets on (priority, key), unprioritized last.

    Unranked tickets cannot be compared against integers, and treating them as
    most urgent would let missing Jira data outrank an explicit decision, so they
    sort after every explicitly prioritized ticket and then by key.
    """
    priority = ticket.get("priority")
    if priority is None:
        return (1, 0, ticket["key"])
    return (0, priority, ticket["key"])


def sprint_identity(inventory: dict[str, Any]) -> tuple[str, str]:
    sprint = inventory.get("sprint")
    if not isinstance(sprint, dict):
        raise SprintError("inventory.sprint must be an object with id and name")
    sprint_id = str(sprint.get("id", "")).strip()
    if not sprint_id:
        raise SprintError(
            "inventory.sprint.id is required; resolve 'active' to the Jira sprint id"
        )
    return sprint_id, str(sprint.get("name", sprint_id)).strip() or sprint_id


def state_path(state_dir: Path, sprint_id: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", sprint_id).strip("-.")[:48] or "sprint"
    digest = hashlib.sha256(sprint_id.encode("utf-8")).hexdigest()[:10]
    return state_dir / f"{slug}-{digest}.json"


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


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SprintError(f"no sprint checkpoint at {path}; run sync first")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read checkpoint {path}: {exc}") from exc
    if value.get("schema_version") == 1:
        value["schema_version"] = SCHEMA_VERSION
        for ticket in value.get("tickets", {}).values():
            if ticket.get("state") == "running":
                ticket["state"] = "user_action"
                ticket["reason"] = (
                    "legacy running lane requires explicit recovery; verify the old worker is stopped, "
                    "then run recover-legacy"
                )
                ticket.setdefault("history", []).append(
                    {"at": now(), "event": "legacy-running-fenced"}
                )
                ticket["legacy_recovery_pending"] = True
            ticket["attempt_token"] = ""
            ticket["attempt_capability"] = {}
            ticket["worker_identity"] = str(ticket.get("run_ref") or "")
            ticket.setdefault("subtasks", [])
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SprintError(f"unsupported sprint checkpoint schema in {path}")
    for ticket in value.get("tickets", {}).values():
        ticket.setdefault("description", "")
        ticket.setdefault(
            "worker_identity",
            str(
                (ticket.get("attempt_capability") or {}).get("worker")
                or ticket.get("run_ref")
                or ""
            ),
        )
        ticket.setdefault("attach_capability", "")
        ticket.setdefault("attached_at", "")
        ticket.setdefault("launch_evidence", {})
        ticket.setdefault("scope_assessment", {})
        ticket.setdefault("resolved_scope_decisions", [])
        ticket.setdefault("recovery_binding", {})
        ticket.setdefault("decomposition_children", [])
        ticket.setdefault("progress", [])
    return value


def save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    write_json(path, state)


def write_json(path: Path, value: Any) -> None:
    """Atomically persist JSON without changing the serialized API payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
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


def read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProcessAbsent(f"{label} does not exist") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise SprintError(f"{label} must contain a JSON object")
    return value


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    """Atomically persist an OpenAI Batch input file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for value in values:
                handle.write(
                    json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def initial_state(raw_status: str, cfg: dict[str, Any]) -> tuple[str, str]:
    folded = raw_status.casefold()
    if folded in cfg["done"]:
        return "completed", f"already {raw_status} in Jira"
    if folded in cfg["blocked"]:
        return "blocked", f"Jira status is {raw_status}"
    if folded in cfg["ready"]:
        return "pending", ""
    return (
        "user_action",
        f"Jira status {raw_status!r} is not configured as ready, done, or blocked",
    )


def normalized_inventory(raw: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    sprint_id, sprint_name = sprint_identity(raw)
    project = str(raw.get("project", "")).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
        raise SprintError("inventory.project must be a Jira project key")
    source_query = str(raw.get("source_query", "")).strip()
    if not source_query:
        raise SprintError("inventory.source_query is required for auditability")
    subtask_source_query = str(raw.get("subtask_source_query", "")).strip()
    if not subtask_source_query:
        raise SprintError(
            "inventory.subtask_source_query is required; fetch sprint children independently"
        )
    raw_subtask_keys = raw.get("subtask_keys")
    if not isinstance(raw_subtask_keys, list):
        raise SprintError(
            "inventory.subtask_keys must be the complete result of subtask_source_query"
        )
    discovered_subtasks = {normalize_key(key) for key in raw_subtask_keys}
    raw_tickets = raw.get("tickets")
    if not isinstance(raw_tickets, list):
        raise SprintError("inventory.tickets must be an array")
    artifact_ref = raw.get("fetch_artifact")
    if not isinstance(artifact_ref, dict) or set(artifact_ref) != {"path", "sha256"}:
        raise SprintError(
            "inventory.fetch_artifact from the Jira fetch adapter is required; hand-authored receipts are rejected"
        )
    artifact_path = Path(str(artifact_ref["path"])).resolve()
    if (
        artifact_path != cfg["shared_root"]
        and cfg["shared_root"] not in artifact_path.parents
    ):
        raise SprintError("Jira fetch artifact must stay in the shared repository")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read Jira fetch artifact: {exc}") from exc
    if (
        not isinstance(artifact, dict)
        or artifact.get("schema_version") != 3
        or artifact.get("adapter") != "jira-rest-v3"
    ):
        raise SprintError("Jira fetch artifact identity is invalid")
    artifact_digest = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if artifact_ref["sha256"] != artifact_digest:
        raise SprintError("Jira fetch artifact digest is invalid")
    if artifact.get("authority") != "provider-network" and not (
        cfg["allow_test_evidence"] and artifact.get("authority") == "test-only"
    ):
        raise SprintError(
            "test-only or caller-authored Jira evidence cannot authorize production sync"
        )
    if artifact.get("authority") == "provider-network" and not cfg.get(
        "adapter_invoked"
    ):
        raise SprintError(
            "production Jira evidence must be fetched by the controller-owned adapter"
        )
    if artifact.get("authority") == "provider-network":
        approved = str(artifact.get("approved_origin") or "")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", approved):
            raise SprintError("Jira evidence has no approved HTTPS provider origin")
    queries = artifact.get("queries")
    if not isinstance(queries, list) or len(queries) not in {2, 3}:
        raise SprintError(
            "Jira fetch artifact requires parent and child query evidence"
        )
    by_kind = {item.get("kind"): item for item in queries if isinstance(item, dict)}
    if not {"parents", "children"}.issubset(by_kind) or not set(by_kind).issubset(
        {"parents", "children", "external"}
    ):
        raise SprintError("Jira fetch artifact query kinds are invalid")

    def proven_keys(query: dict[str, Any], expected_jql: str) -> list[str]:
        if (
            query.get("jql") != expected_jql
            or not isinstance(query.get("fields"), list)
            or not query["fields"]
            or not isinstance(query.get("pages"), list)
            or not query["pages"]
        ):
            raise SprintError(
                "Jira fetch artifact does not bind the exact query and its pages"
            )
        offset = 0
        cursor = ""
        keys: list[str] = []
        provider_total: int | None = None
        for index, page in enumerate(query["pages"]):
            if not isinstance(page, dict) or set(page) != {
                "start_at",
                "count",
                "total",
                "item_keys",
                "terminal",
                "cursor_in",
                "cursor_out",
                "raw_sha256",
                "raw_path",
            }:
                raise SprintError(
                    "Jira fetch pages require exact pagination and item-key fields"
                )
            page_keys = page["item_keys"]
            if (
                not isinstance(page["start_at"], int)
                or page["start_at"] != offset
                or not isinstance(page["count"], int)
                or page["count"] < 0
                or not isinstance(page_keys, list)
                or page["count"] != len(page_keys)
                or not isinstance(page["terminal"], bool)
                or page["cursor_in"] != cursor
                or not isinstance(page["cursor_out"], str)
                or (page["terminal"] and index != len(query["pages"]) - 1)
            ):
                raise SprintError(
                    "Jira fetch pagination is overlapping, gapped, or truncated"
                )
            if page["total"] is not None:
                if not isinstance(page["total"], int) or page["total"] < 0:
                    raise SprintError("Jira fetch provider total is invalid")
                if provider_total is None:
                    provider_total = page["total"]
                elif provider_total != page["total"]:
                    raise SprintError("Jira fetch provider total changed between pages")
            normalized = [normalize_key(key) for key in page_keys]
            raw_path = Path(str(page["raw_path"])).resolve()
            if (
                raw_path != cfg["shared_root"]
                and cfg["shared_root"] not in raw_path.parents
            ):
                raise SprintError(
                    "Jira raw response evidence escapes the shared repository"
                )
            try:
                raw_response = json.loads(raw_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SprintError(
                    f"cannot read Jira raw response evidence: {exc}"
                ) from exc
            if not isinstance(raw_response, dict):
                raise SprintError("Jira raw response evidence must be an object")
            raw_digest = hashlib.sha256(
                json.dumps(raw_response, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            if (
                raw_digest != page["raw_sha256"]
                or raw_path.name != f"sha256-{raw_digest}.json"
            ):
                raise SprintError("Jira raw response is not content-addressed")
            allowed_top_level = {
                "startAt",
                "total",
                "isLast",
                "nextPageToken",
                "issues",
            }
            if not set(raw_response).issubset(allowed_top_level) or any(
                not set((issue.get("fields") or {})).issubset(set(query["fields"]))
                for issue in raw_response.get("issues", [])
                if isinstance(issue, dict)
            ):
                raise SprintError(
                    "Jira raw evidence exceeds the explicitly requested field surface"
                )
            if (
                raw_response.get("startAt") != page["start_at"]
                and "startAt" in raw_response
            ) or (
                str(raw_response.get("nextPageToken") or "") != page["cursor_out"]
                or page["cursor_in"] != cursor
                or raw_response.get("total") != page["total"]
                or (
                    "isLast" in raw_response
                    and bool(raw_response.get("isLast")) != page["terminal"]
                )
                or len(raw_response.get("issues", [])) != page["count"]
                or [
                    str(item.get("key", "")).upper()
                    for item in raw_response.get("issues", [])
                ]
                != page_keys
            ):
                raise SprintError("Jira page summary does not match its raw response")
            if len(normalized) != len(set(normalized)) or set(normalized) & set(keys):
                raise SprintError("Jira fetch pages contain duplicate item keys")
            keys.extend(normalized)
            offset += page["count"]
            cursor = page["cursor_out"]
        last = query["pages"][-1]
        if not last["terminal"] and (
            provider_total is None or offset != provider_total
        ):
            raise SprintError("Jira fetch artifact does not prove provider exhaustion")
        if provider_total is not None and offset != provider_total:
            raise SprintError("Jira fetch artifact is truncated before provider total")
        return sorted(keys)

    inventory_keys = sorted(
        normalize_key(item.get("key")) for item in raw_tickets if isinstance(item, dict)
    )
    sprint_keys = proven_keys(by_kind["parents"], source_query)
    child_keys = proven_keys(by_kind["children"], subtask_source_query)
    if sorted(set(sprint_keys) | set(child_keys)) != inventory_keys:
        raise SprintError(
            "Jira sprint and child pages do not bind the exact inventory ticket keys"
        )
    if child_keys != sorted(discovered_subtasks):
        raise SprintError(
            "Jira child pages do not bind the exact independent child keys"
        )
    tickets: dict[str, dict[str, Any]] = {}
    for item in raw_tickets:
        if not isinstance(item, dict):
            raise SprintError("each inventory ticket must be an object")
        key = normalize_key(item.get("key"))
        if not key.startswith(f"{project}-"):
            raise SprintError(
                f"sprint ticket {key} is outside configured project {project}"
            )
        if key in tickets:
            raise SprintError(f"duplicate ticket in inventory: {key}")
        dependencies: list[str] = []
        raw_dependencies = item.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise SprintError(f"ticket {key} dependencies must be an array")
        for dependency in raw_dependencies:
            normalized = normalize_key(dependency)
            if normalized not in dependencies:
                dependencies.append(normalized)
        subtasks: list[str] = []
        if "subtasks" not in item:
            raise SprintError(
                f"ticket {key} omits subtasks; Jira inventory must explicitly include an empty or complete array"
            )
        raw_subtasks = item["subtasks"]
        if not isinstance(raw_subtasks, list):
            raise SprintError(f"ticket {key} subtasks must be an array")
        for subtask in raw_subtasks:
            normalized = normalize_key(
                subtask.get("key") if isinstance(subtask, dict) else subtask
            )
            if normalized not in subtasks:
                subtasks.append(normalized)
        raw_status = str(item.get("status", "")).strip()
        raw_labels = item.get("labels", [])
        if not isinstance(raw_labels, list) or any(
            not isinstance(label, str) or not label.strip() for label in raw_labels
        ):
            raise SprintError(f"ticket {key} labels must be an array of strings")
        state, reason = initial_state(raw_status, cfg)
        tickets[key] = {
            "key": key,
            "summary": str(item.get("summary", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "url": str(item.get("url", "")).strip(),
            "raw_status": raw_status,
            "priority": normalize_priority(item.get("priority"), key),
            "labels": sorted(set(raw_labels)),
            "issue_type": str(item.get("issue_type", "")).strip(),
            "is_subtask": item.get("is_subtask") is True,
            "parent": (
                normalize_key((item.get("parent") or {}).get("key"))
                if isinstance(item.get("parent"), dict)
                else normalize_key(item.get("parent"))
                if item.get("parent")
                else ""
            ),
            "dependencies": sorted(dependencies),
            "subtasks": sorted(subtasks),
            "state": state,
            "reason": reason,
            "run_ref": "",
            "branch": "",
            "pr": "",
            "attempts": 0,
            "continuations": 0,
            "next_launch_continuation": False,
            "attempt_token": "",
            "worker_identity": "",
            "attach_capability": "",
            "attached_at": "",
            "launch_evidence": {},
            "scope_assessment": {},
            "resolved_scope_decisions": [],
            "recovery_binding": {},
            "decomposition_children": [],
            "progress": [],
            "history": [],
        }
    missing_subtasks = sorted(
        {
            subtask
            for ticket in tickets.values()
            for subtask in ticket["subtasks"]
            if subtask not in tickets
        }
    )
    if missing_subtasks:
        raise SprintError(
            "Jira inventory is incomplete; fetch every referenced subtask explicitly: "
            + ", ".join(missing_subtasks)
        )
    declared_subtasks = {
        subtask for ticket in tickets.values() for subtask in ticket["subtasks"]
    }
    if declared_subtasks != discovered_subtasks:
        missing_from_parents = sorted(discovered_subtasks - declared_subtasks)
        missing_from_query = sorted(declared_subtasks - discovered_subtasks)
        raise SprintError(
            "Jira subtask inventory disagrees with the independent child query; "
            f"unlinked query results={missing_from_parents}, absent query results={missing_from_query}"
        )
    absent_children = sorted(discovered_subtasks - set(tickets))
    if absent_children:
        raise SprintError(
            "Jira child query results are absent from tickets: "
            + ", ".join(absent_children)
        )
    expected_relations = sorted(
        {
            f"{parent}:{child}"
            for parent, item in tickets.items()
            for child in item["subtasks"]
        }
    )
    relations = artifact.get("relations")
    actual_relations = (
        sorted(
            {
                f"{normalize_key(item.get('parent'))}:{normalize_key(item.get('child'))}"
                for item in relations
            }
        )
        if isinstance(relations, list)
        and all(isinstance(item, dict) for item in relations)
        else []
    )
    child_parents = artifact.get("child_parents")
    expected_child_parents = {
        child: parent for parent, item in tickets.items() for child in item["subtasks"]
    }
    if (
        actual_relations != expected_relations
        or child_parents != expected_child_parents
    ):
        raise SprintError(
            "Jira fetch artifact does not bind bidirectional parent/child relations"
        )
    external: dict[str, str] = {}
    raw_external = raw.get("dependency_status", {})
    if not isinstance(raw_external, dict):
        raise SprintError("inventory.dependency_status must be an object when present")
    for key, status in raw_external.items():
        external[normalize_key(key)] = str(status).strip()
    expected_external = sorted(
        {
            dependency
            for ticket in tickets.values()
            for dependency in ticket["dependencies"]
            if dependency not in tickets
        }
    )
    external_query = by_kind.get("external")
    if expected_external:
        expected_jql = "key in (" + ",".join(expected_external) + ")"
        if (
            external_query is None
            or proven_keys(external_query, expected_jql) != expected_external
        ):
            raise SprintError(
                "Jira external dependency query does not bind every dependency"
            )
        proven_status: dict[str, str] = {}
        for page in external_query["pages"]:
            response = json.loads(
                Path(str(page["raw_path"])).read_text(encoding="utf-8")
            )
            for issue in response.get("issues", []):
                status_value = (issue.get("fields") or {}).get("status")
                status = (
                    status_value.get("name")
                    if isinstance(status_value, dict)
                    else status_value
                )
                proven_status[normalize_key(issue.get("key"))] = str(
                    status or ""
                ).strip()
        if proven_status != external:
            raise SprintError(
                "Jira external dependency statuses disagree with provider evidence"
            )
    elif external_query is not None or external:
        raise SprintError("Jira external dependency evidence is unexpected")
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "sprint": {"id": sprint_id, "name": sprint_name},
        "source_query": source_query,
        "subtask_source_query": subtask_source_query,
        "subtask_keys": sorted(discovered_subtasks),
        "tickets": tickets,
        "dependency_status": external,
        "created_at": now(),
        "updated_at": now(),
    }


def effective_dependencies(tickets: dict[str, dict[str, Any]], key: str) -> list[str]:
    """Children inherit tracking-parent prerequisites without depending on the parent."""
    dependencies: set[str] = set()
    pending, visited = [key], set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        dependencies.update(tickets[current]["dependencies"])
        for parent in tickets.values():
            if parent["state"] == "decomposed" and current in parent.get(
                "decomposition_children", []
            ):
                pending.append(parent["key"])
    return sorted(dependencies)


def dependency_complete(
    state: dict[str, Any],
    key: str,
    cfg: dict[str, Any],
    visiting: frozenset[str] = frozenset(),
) -> bool:
    """Resolve a tracking parent only through its bound, completed child set."""
    if key in visiting:
        return False
    ticket = state["tickets"].get(key)
    if ticket is None:
        return str(state["dependency_status"].get(key, "")).casefold() in cfg["done"]
    if ticket["state"] == "completed":
        return True
    if ticket["state"] != "decomposed":
        return False
    children = ticket.get("decomposition_children", [])
    if len(children) < 2 or set(children) != set(ticket.get("subtasks", [])):
        return False
    # Missing children must not be substituted with unrelated external status.
    if any(child not in state["tickets"] for child in children):
        return False
    return all(
        dependency_complete(state, dependency, cfg, visiting | {key})
        for dependency in children + effective_dependencies(state["tickets"], key)
    )


def find_cycles(tickets: dict[str, dict[str, Any]]) -> dict[str, str]:
    visiting: list[str] = []
    visited: set[str] = set()
    cycle_reason: dict[str, str] = {}

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            start = visiting.index(key)
            cycle = visiting[start:] + [key]
            reason = "dependency cycle: " + " -> ".join(cycle)
            for member in cycle[:-1]:
                cycle_reason[member] = reason
            return
        visiting.append(key)
        dependencies = effective_dependencies(tickets, key)
        if tickets[key]["state"] == "decomposed":
            dependencies += tickets[key].get("decomposition_children", [])
        for dependency in dependencies:
            if dependency in tickets:
                visit(dependency)
        visiting.pop()
        visited.add(key)

    for ticket_key in sorted(tickets):
        visit(ticket_key)
    return cycle_reason


def blockers(
    state: dict[str, Any],
    key: str,
    cfg: dict[str, Any],
    cycles: dict[str, str] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if cycles is None:
        cycles = find_cycles(state["tickets"])
    if key in cycles:
        reasons.append(cycles[key])
    for parent in state["tickets"].values():
        if (
            key in parent.get("subtasks", [])
            and parent["state"] == "needs_decomposition"
        ):
            reasons.append(f"parent {parent['key']} awaits decomposition binding")
        elif parent["state"] == "decomposed" and key in parent.get("subtasks", []):
            if set(parent.get("subtasks", [])) != set(
                parent.get("decomposition_children", [])
            ):
                reasons.append(
                    f"parent {parent['key']} child inventory changed after decomposition"
                )
    for dependency in effective_dependencies(state["tickets"], key):
        if dependency == key:
            reasons.append(f"self dependency: {key}")
            continue
        internal = state["tickets"].get(dependency)
        if internal:
            dep_state = internal["state"]
            if dependency_complete(state, dependency, cfg):
                continue
            if dep_state == "decomposed":
                reasons.append(
                    f"dependency {dependency} awaits completion of its bound children and prerequisites"
                )
                continue
            if dep_state in {
                "blocked",
                "external_blocked",
                "operator_decision",
                "user_action",
            }:
                reasons.append(f"dependency {dependency} ended {dep_state}")
            else:
                reasons.append(f"dependency {dependency} is {dep_state}")
            continue
        raw_status = state["dependency_status"].get(dependency)
        if raw_status is None:
            reasons.append(
                f"dependency {dependency} is outside the sprint and has no fetched status"
            )
        elif raw_status.casefold() not in cfg["done"]:
            reasons.append(f"external dependency {dependency} is {raw_status}")
    return sorted(set(reasons))


def authorized_restart_grant(repository, ticket, token=""):
    try:
        return host_restart_grant(repository, ticket, token)
    except AuthorityError as exc:
        raise SprintError(str(exc)) from exc


def current_startup_failure(ticket, cfg):
    """Credit only a fenced stopped launch with explicit rejection and no paid/uncertain work."""
    identity = ticket.get("worker_identity")
    if not isinstance(identity, dict) or identity.get("kind") != "execution_unit":
        return None
    if execution_unit_status(identity) != "absent":
        return None
    try:
        terminal = read_json(
            Path(identity.get("tombstone_path", "")), label="startup terminal"
        )
    except (SprintError, OSError):
        return None
    invocation = identity.get("invocation_id")
    if (
        not invocation
        or terminal.get("invocation_id") != invocation
        or terminal.get("startup_retryable") is not True
        or terminal.get("stop_reason") != "provider_rate_limited"
    ):
        return None
    events = UsageLedger(cfg["shared_root"]).snapshot()
    reservations = {
        e.get("reservation_id")
        for e in events
        if e.get("kind") == "reservation" and e.get("run_id") == invocation
    }
    released = {e.get("reservation_id") for e in events if e.get("kind") == "release"}
    if not reservations <= released or any(
        e.get("kind") == "usage" and e.get("run_id") == invocation for e in events
    ):
        return None
    return {"invocation_id": invocation, "finished_at": terminal.get("finished_at", "")}


def startup_credits(ticket, cfg):
    receipts = {
        item["invocation_id"] for item in ticket.get("startup_retry_receipts", [])
    }
    current = current_startup_failure(ticket, cfg)
    if current:
        receipts.add(current["invocation_id"])
    return min(2, len(receipts))


def attempt_limit_reason(ticket: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Return a launch blocker when this ticket has used every authorized attempt."""
    attempts = int(ticket.get("attempts") or 0)
    continuations = int(ticket.get("continuations") or 0)
    charged_attempts = max(
        int(ticket.get("charged_attempts", attempts) or 0),
        attempts - continuations,
    )
    if ticket.get("next_launch_continuation") and continuations < int(
        cfg.get("max_worker_continuations", 6)
    ):
        return None
    restart = authorized_restart_grant(cfg["shared_root"], str(ticket["key"]))
    base_ceiling = cfg["max_lane_relaunches"] + 1 + startup_credits(ticket, cfg)
    if restart:
        base_ceiling = max(base_ceiling, restart["allowances"]["attempts"])
    if charged_attempts < base_ceiling:
        return None
    try:
        grant_ceiling = authorized_relaunch_ceiling(
            cfg["shared_root"], str(ticket["key"])
        )
    except AuthorityError as exc:
        raise SprintError(str(exc)) from exc
    effective_ceiling = max(base_ceiling, grant_ceiling or 0)
    if charged_attempts < effective_ceiling:
        return None
    grant_detail = (
        f"; active ticket ceiling={grant_ceiling}" if grant_ceiling is not None else ""
    )
    return (
        f"attempt ceiling exhausted after {charged_attempts} charged attempts "
        f"(max_lane_relaunches={cfg['max_lane_relaunches']}{grant_detail}); "
        "root-issued ticket relaunch authority is required"
    )


def usage_snapshots(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = cfg["shared_root"] / ".orchestration/.llm-usage/usage.jsonl"
    result: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return result
    open_reservations: dict[str, dict[str, Any]] = {}
    pause_events: dict[str, dict[str, Any]] = {}
    events = UsageLedger(cfg["shared_root"]).snapshot()
    for event in events:
        ticket = str(event.get("ticket") or "")
        kind = event.get("kind")
        if kind == "reservation":
            open_reservations[str(event["reservation_id"])] = event
            if ticket:
                item = result.setdefault(
                    ticket,
                    {
                        "spent_usd": 0.0,
                        "reserved_usd": 0.0,
                        "run_ids": set(),
                        "design_review_run_ids": set(),
                        "reviewer_run_ids": set(),
                    },
                )
                if event.get("run_id"):
                    item["run_ids"].add(str(event["run_id"]))
                    if event.get("role") in DESIGN_REVIEWER_ROLES:
                        item["design_review_run_ids"].add(str(event["run_id"]))
        elif kind in {"usage", "release"}:
            open_reservations.pop(str(event.get("reservation_id") or ""), None)
        if (
            kind == "ticket_budget_pause"
            and ticket
            and str(event.get("reason") or "") not in TRANSIENT_PAUSE_REASONS
        ):
            pause_events[ticket] = event
            # The first request can exceed a ticket ceiling before any
            # reservation exists. Its pause must still block planner admission.
            result.setdefault(
                ticket,
                {
                    "spent_usd": 0.0,
                    "reserved_usd": 0.0,
                    "run_ids": set(),
                    "design_review_run_ids": set(),
                    "reviewer_run_ids": set(),
                },
            )
        elif kind == "ticket_budget_reset" and ticket:
            pause_events.pop(ticket, None)
        if kind == "usage" and ticket:
            item = result.setdefault(
                ticket,
                {
                    "spent_usd": 0.0,
                    "reserved_usd": 0.0,
                    "run_ids": set(),
                    "design_review_run_ids": set(),
                    "reviewer_run_ids": set(),
                },
            )
            item["spent_usd"] += float(event.get("cost_usd", 0))
            if event.get("run_id"):
                item["run_ids"].add(str(event["run_id"]))
                if event.get("role") in DESIGN_REVIEWER_ROLES:
                    item["design_review_run_ids"].add(str(event["run_id"]))
                if event.get("role") in POST_IMPLEMENTATION_REVIEWER_ROLES:
                    item["reviewer_run_ids"].add(
                        str(event.get("logical_review_id") or event["run_id"])
                    )
    for event in open_reservations.values():
        ticket = str(event.get("ticket") or "")
        if ticket:
            item = result.setdefault(
                ticket,
                {
                    "spent_usd": 0.0,
                    "reserved_usd": 0.0,
                    "run_ids": set(),
                    "design_review_run_ids": set(),
                    "reviewer_run_ids": set(),
                },
            )
            item["reserved_usd"] += float(event.get("projected_cost_usd", 0))
            if event.get("run_id"):
                item["run_ids"].add(str(event["run_id"]))
                if event.get("role") in DESIGN_REVIEWER_ROLES:
                    item["design_review_run_ids"].add(str(event["run_id"]))
                if event.get("role") in POST_IMPLEMENTATION_REVIEWER_ROLES:
                    item["reviewer_run_ids"].add(
                        str(event.get("logical_review_id") or event["run_id"])
                    )
    phase_limits = budgets_from_config(
        load_yaml(cfg["config"]) if cfg.get("config") else {}
    )
    for ticket, item in result.items():
        restart = authorized_restart_grant(cfg["shared_root"], ticket)
        allowances = restart["allowances"] if restart else {}
        item["phase_budgets"] = {}
        for phase, totals in UsageLedger.phase_totals(events, ticket).items():
            limit = max(
                UsageLedger.phase_limits(events, ticket, phase_limits)[phase],
                Decimal(allowances.get(phase + "_usd", "0")),
            )
            total = totals["spent_usd"] + totals["reserved_usd"]
            item["phase_budgets"][phase] = {
                **{name: float(value) for name, value in totals.items()},
                "limit_usd": float(limit),
                "remaining_usd": float(max(0, limit - total)),
                "state": "exhausted"
                if totals["spent_usd"] >= limit
                else "fully_reserved"
                if total >= limit
                else "available",
            }
        item["run_count"] = len(item.pop("run_ids") - item["design_review_run_ids"])
        item["design_review_run_count"] = len(item.pop("design_review_run_ids"))
        item["reviewer_run_count"] = len(item.pop("reviewer_run_ids"))
        total = item["spent_usd"] + item["reserved_usd"]
        item["projected_total_usd"] = round(total, 6)
        pause = cfg["pause_usd_per_ticket"]
        grant_ceiling = None
        # Older ledgers may contain PR numbers, smoke-test labels, and other
        # non-Jira accounting buckets. Preserve their spend in reports, but do
        # not present them to the root authority as ticket scopes.
        if re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", ticket):
            try:
                grant_ceiling = authorized_budget_ceiling(cfg["shared_root"], ticket)
            except AuthorityError as exc:
                raise SprintError(str(exc)) from exc
        if restart:
            grant_ceiling = max(
                float(grant_ceiling or 0), float(allowances["ticket_usd"])
            )
        if grant_ceiling is not None:
            pause = max(pause, float(grant_ceiling))
        warning = cfg["warn_usd_per_ticket"]
        item["state"] = (
            "operator_action"
            if (
                (ticket in pause_events and grant_ceiling is None)
                or (pause and total > pause)
                or item["run_count"]
                >= max(
                    cfg["max_model_runs_per_ticket"], allowances.get("model_runs", 0)
                )
                or item["reviewer_run_count"]
                >= max(
                    cfg["max_reviewer_runs_per_ticket"],
                    allowances.get("review_runs", 0),
                )
            )
            else "warning"
            if warning and total > warning
            else "ok"
        )
    return result


def sync(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    inventory_path = Path(args.inventory or "")
    if args.inventory_template:
        template_path = Path(args.inventory_template).resolve()
        digest = hashlib.sha256(template_path.read_bytes()).hexdigest()[:20]
        evidence_dir = cfg["state_dir"] / "jira-evidence"
        inventory_path = evidence_dir / f"inventory-{digest}.json"
        artifact_path = evidence_dir / f"artifact-{digest}.json"
        adapter = Path(__file__).with_name("jira_inventory_fetch.py")
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(adapter),
                    "--inventory-template",
                    str(template_path),
                    "--artifact",
                    str(artifact_path),
                    "--output",
                    str(inventory_path),
                ],
                cwd=cfg["shared_root"],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SprintError("controller-owned Jira fetch failed") from exc
        cfg = {**cfg, "adapter_invoked": True}
    try:
        raw = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read inventory {inventory_path}: {exc}") from exc
    incoming = normalized_inventory(raw, cfg)
    path = state_path(cfg["state_dir"], incoming["sprint"]["id"])
    with locked(path):
        if path.exists():
            current = load(path)
            if current["sprint"]["id"] != incoming["sprint"]["id"]:
                raise SprintError("checkpoint sprint identity mismatch")
            incoming_keys = set(incoming["tickets"])
            for key, previous in current["tickets"].items():
                if key not in incoming_keys and previous["state"] == "pending":
                    previous["state"] = "user_action"
                    previous["reason"] = (
                        "ticket disappeared from the refreshed Jira sprint query"
                    )
                    previous["history"].append(
                        {"at": now(), "event": "removed-from-query"}
                    )
            for key, fresh in incoming["tickets"].items():
                previous = current["tickets"].get(key)
                if previous:
                    authoritative_ready = fresh["state"] == "pending"
                    # Only Jira-owned, never-started readiness follows Jira.
                    # A temporary inventory exclusion can clear when the
                    # authenticated fetch includes the untouched ticket again.
                    # Worker and scoping decisions still require recovery.
                    previous_initial = initial_state(
                        previous.get("raw_status", ""), cfg
                    )
                    returned_to_query = (
                        previous["state"] == "user_action"
                        and previous.get("reason")
                        == "ticket disappeared from the refreshed Jira sprint query"
                        and any(
                            event.get("event") == "removed-from-query"
                            for event in previous.get("history", [])
                        )
                        and not any(
                            previous.get(field)
                            for field in (
                                "attempts",
                                "attempt_token",
                                "attempt_capability",
                                "run_ref",
                                "branch",
                                "pr",
                                "worker_identity",
                                "attach_capability",
                                "attached_at",
                                "launch_evidence",
                                "legacy_recovery_pending",
                                "scope_assessment",
                                "decomposition_children",
                                "progress",
                                "verified_commits",
                                "ci_progress",
                                "test_progress",
                            )
                        )
                    )
                    no_execution_evidence = not any(
                        previous.get(field)
                        for field in (
                            "attempts",
                            "attempt_token",
                            "attempt_capability",
                            "run_ref",
                            "branch",
                            "pr",
                            "worker_identity",
                            "attach_capability",
                            "attached_at",
                            "launch_evidence",
                            "scope_assessment",
                            "decomposition_children",
                            "progress",
                            "verified_commits",
                            "ci_progress",
                            "test_progress",
                            "subtasks",
                        )
                    ) and not any(
                        event.get("event")
                        in {
                            "reserved",
                            "batch-reserved",
                            "worker-launched",
                            "finished",
                            "progress",
                            "scope-recorded",
                            "decomposition-recorded",
                        }
                        for event in previous.get("history", [])
                    )
                    reconcile_legacy_readiness = bool(
                        authoritative_ready
                        and previous["state"] in {"blocked", "user_action"}
                        and re.fullmatch(
                            r"(?i)(verify[_ -]?jira[_ -]?readiness|jira readiness(?: verification)? required)",
                            str(previous.get("reason") or "").strip(),
                        )
                        and no_execution_evidence
                        and previous.get("history")
                        and not any(
                            event.get("event") == "removed-from-query"
                            for event in previous.get("history", [])
                        )
                    )
                    refresh_readiness = (
                        not previous.get("attempts")
                        and previous["state"] in {"pending", "blocked", "user_action"}
                        and not previous.get("scope_assessment")
                        and (
                            returned_to_query
                            or (previous["state"], previous.get("reason", ""))
                            == previous_initial
                        )
                        and all(
                            event.get("event")
                            in {
                                "jira-status-refreshed",
                                "removed-from-query",
                                "returned-to-query",
                            }
                            for event in previous.get("history", [])
                        )
                    )
                    restart_status_refresh = bool(
                        authoritative_ready
                        and previous["state"] == "user_action"
                        and previous.get("reason", "") == previous_initial[1]
                        and any(
                            event.get("event") == "operator-restart"
                            for event in previous.get("history", [])
                        )
                        and previous.get("scope_assessment", {}).get("verdict")
                        != "operator_decision"
                    )
                    stale_decomposition_hold = bool(
                        cfg.get("auto_decompose_large_tickets")
                        and previous["state"] in {"user_action", "operator_decision"}
                        and previous.get("reason", "")
                        == "automatic decomposition is disabled by repository policy"
                        and previous.get("scope_assessment", {}).get("verdict")
                        == "decompose"
                    )
                    for field in (
                        "state",
                        "reason",
                        "run_ref",
                        "branch",
                        "pr",
                        "attempts",
                        "charged_attempts",
                        "continuations",
                        "next_launch_continuation",
                        "attempt_token",
                        "history",
                        "attempt_capability",
                        "reserved_route",
                        "legacy_recovery_pending",
                        "worker_identity",
                        "attach_capability",
                        "attached_at",
                        "launch_evidence",
                        "scope_assessment",
                        "resolved_scope_decisions",
                        "recovery_binding",
                        "restart_grant_id",
                        "startup_retry_receipts",
                        "decomposition_children",
                        "progress",
                        "verified_commits",
                        "ci_progress",
                        "test_progress",
                    ):
                        if (
                            refresh_readiness
                            or restart_status_refresh
                            or stale_decomposition_hold
                        ) and field in {"state", "reason"}:
                            continue
                        if field in previous:
                            fresh[field] = previous[field]
                    if stale_decomposition_hold:
                        fresh["state"] = "needs_decomposition"
                        fresh["reason"] = (
                            "repository policy now authorizes automatic decomposition"
                        )
                        fresh["history"].append(
                            {
                                "at": now(),
                                "event": "decomposition-policy-enabled",
                            }
                        )
                    elif restart_status_refresh:
                        fresh["state"] = "pending"
                        fresh["reason"] = ""
                        fresh["history"].append(
                            {
                                "at": now(),
                                "event": "jira-status-refreshed",
                                "status": fresh["raw_status"],
                                "source": "post-restart-authoritative-sync",
                            }
                        )
                    elif reconcile_legacy_readiness:
                        fresh["state"] = "pending"
                        fresh["reason"] = ""
                        fresh["history"].append(
                            {
                                "at": now(),
                                "event": "legacy-readiness-reconciled",
                                "status": fresh["raw_status"],
                            }
                        )
                    elif refresh_readiness and returned_to_query:
                        fresh["history"].append(
                            {
                                "at": now(),
                                "event": "returned-to-query",
                                "status": fresh["raw_status"],
                            }
                        )
                    elif refresh_readiness and fresh["raw_status"] != previous.get(
                        "raw_status"
                    ):
                        fresh["history"].append(
                            {
                                "at": now(),
                                "event": "jira-status-refreshed",
                                "status": fresh["raw_status"],
                            }
                        )
                prior_scope = fresh.get("scope_assessment") or {}
                if (
                    cfg.get("runtime_admission")
                    and fresh["state"] == "operator_decision"
                    and prior_scope.get("decision_kind") == "dependency_reconciliation"
                    and prior_scope.get("missing_dependencies")
                    and set(prior_scope["missing_dependencies"]).issubset(
                        fresh.get("dependencies", [])
                    )
                ):
                    fresh["state"] = "pending"
                    fresh["reason"] = (
                        "authenticated prerequisite relationships reconciled; rescoping required"
                    )
                    fresh["scope_assessment"] = {}
                    fresh["history"].append(
                        {"at": now(), "event": "dependencies-reconciled"}
                    )
                elif (
                    cfg.get("runtime_admission")
                    and fresh["state"] == "operator_decision"
                    and prior_scope.get("decision_key")
                    in cfg.get("decision_registry", {})
                ):
                    decision_key = prior_scope["decision_key"]
                    fresh["state"] = "pending"
                    fresh["reason"] = (
                        f"repository decision {decision_key} is approved; rescoping with that policy"
                    )
                    fresh.setdefault("resolved_scope_decisions", []).append(
                        {"at": now(), "decision_key": decision_key}
                    )
                    fresh["scope_assessment"] = {}
                    fresh["history"].append(
                        {
                            "at": now(),
                            "event": "repository-decision-applied",
                            "decision_key": decision_key,
                        }
                    )
                if cfg.get("runtime_admission") and fresh["state"] == "pending":
                    assessment = fresh.get("scope_assessment") or {}
                    if assessment.get("inventory_digest") != scope_digest(fresh):
                        fresh["scope_assessment"] = {}
                current["tickets"][key] = fresh
            current["project"] = incoming["project"]
            current["sprint"] = incoming["sprint"]
            current["source_query"] = incoming["source_query"]
            current["subtask_source_query"] = incoming["subtask_source_query"]
            current["subtask_keys"] = incoming["subtask_keys"]
            current["dependency_status"] = incoming["dependency_status"]
            state = current
        else:
            state = incoming
        save(path, state)
    emit(
        {
            "checkpoint": str(path),
            "sprint": state["sprint"],
            "tickets": len(state["tickets"]),
        }
    )


def get_state(
    args: argparse.Namespace, cfg: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    path = state_path(cfg["state_dir"], str(args.sprint))
    return path, load(path)


def validated_scope_assessment(
    raw: dict[str, Any],
    ticket: str,
    max_slices: int,
    threshold: int,
    required_contracts: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a scoper result before it can change controller scheduling."""
    if raw.get("schema_version") != 1:
        raise SprintError("scope assessment schema_version must be 1")
    if normalize_key(raw.get("ticket")) != ticket:
        raise SprintError("scope assessment belongs to a different ticket")
    verdict = str(raw.get("verdict") or "").strip().casefold()
    if verdict not in {"ready", "decompose", "operator_decision", "tracking_parent"}:
        raise SprintError(
            "scope assessment verdict must be ready, decompose, operator_decision, or tracking_parent"
        )
    score = raw.get("complexity_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise SprintError(
            "scope assessment complexity_score must be an integer from 0 to 100"
        )
    if verdict == "ready" and score >= threshold:
        raise SprintError(
            f"complexity score {score} reaches decomposition threshold {threshold}"
        )
    reasons = raw.get("reasons")
    if (
        not isinstance(reasons, list)
        or not reasons
        or any(not isinstance(item, str) or not item.strip() for item in reasons)
    ):
        raise SprintError("scope assessment requires non-empty reasons")
    if len(reasons) > 20 or any(len(item) > 2000 for item in reasons):
        raise SprintError("scope assessment reasons exceed the bounded schema")
    slices = raw.get("slices", [])
    required_contracts = required_contracts or []
    if verdict == "decompose":
        if not isinstance(slices, list) or not 2 <= len(slices) <= max_slices:
            raise SprintError(
                f"decomposition requires 2 through {max_slices} bounded slices"
            )
        identifiers: set[str] = set()
        for index, item in enumerate(slices, 1):
            if not isinstance(item, dict):
                raise SprintError("every decomposition slice must be an object")
            identifier = str(item.get("id") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", identifier):
                raise SprintError(f"slice {index} has an invalid id")
            if identifier in identifiers:
                raise SprintError(f"duplicate decomposition slice id: {identifier}")
            identifiers.add(identifier)
            for field in ("summary", "behavior"):
                if not str(item.get(field) or "").strip():
                    raise SprintError(f"slice {identifier} requires {field}")
            if len(str(item["summary"])) > 255 or len(str(item["behavior"])) > 8000:
                raise SprintError(f"slice {identifier} exceeds Jira field limits")
            validate_delivery(item, SprintError)
            validate_contracts(item, required_contracts, SprintError)
            criteria = item.get("acceptance_criteria")
            if (
                not isinstance(criteria, list)
                or not criteria
                or any(
                    not isinstance(value, str) or not value.strip()
                    for value in criteria
                )
            ):
                raise SprintError(
                    f"slice {identifier} requires testable acceptance_criteria"
                )
            if len(criteria) > 30 or any(len(value) > 2000 for value in criteria):
                raise SprintError(
                    f"slice {identifier} acceptance criteria exceed limits"
                )
            dependencies = item.get("depends_on", [])
            if not isinstance(dependencies, list) or any(
                not isinstance(value, str) for value in dependencies
            ):
                raise SprintError(f"slice {identifier} depends_on must be an array")
        for item in slices:
            validate_owner(item, identifiers, SprintError)
            unknown = sorted(set(item.get("depends_on", [])) - identifiers)
            if unknown or item["id"] in item.get("depends_on", []):
                raise SprintError(
                    f"slice {item['id']} has invalid dependencies: {', '.join(unknown) or item['id']}"
                )
        graph = {item["id"]: set(item.get("depends_on", [])) for item in slices}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(identifier: str) -> None:
            if identifier in visiting:
                raise SprintError("decomposition slice dependencies contain a cycle")
            if identifier in visited:
                return
            visiting.add(identifier)
            for dependency in graph[identifier]:
                visit(dependency)
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in graph:
            visit(identifier)
    elif slices:
        raise SprintError("only a decompose verdict may include slices")
    decision_key = str(raw.get("decision_key") or "").strip()
    decision_question = str(raw.get("decision_question") or "").strip()
    if verdict == "operator_decision":
        if decision_key and not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", decision_key):
            raise SprintError(
                "scope decision_key must be a stable lower-case identifier"
            )
        if decision_key and (not decision_question or len(decision_question) > 2000):
            raise SprintError("scope decision_question is required and must be bounded")
    elif decision_key or decision_question:
        raise SprintError("only an operator_decision verdict may identify a decision")
    return {
        "schema_version": 1,
        "ticket": ticket,
        "verdict": verdict,
        "complexity_score": score,
        "reasons": [item.strip() for item in reasons],
        "slices": slices,
        "children": raw.get("children", []),
        "decision_key": decision_key,
        "decision_question": decision_question,
    }


def scope_digest(ticket):
    return hashlib.sha256(
        json.dumps(
            {
                k: ticket.get(k)
                for k in ("description", "summary", "dependencies", "subtasks")
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def record_scope(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    shared_root = Path(cfg["shared_root"]).resolve()
    assessment_path = Path(args.assessment).resolve()
    if assessment_path != shared_root and shared_root not in assessment_path.parents:
        raise SprintError("scope assessment must be stored inside the repository")
    raw = read_json(assessment_path, label="scope assessment")
    assessment = validated_scope_assessment(
        raw,
        key,
        cfg["max_auto_slices"],
        cfg["decomposition_threshold"],
        cfg.get("required_slice_contracts", []),
    )
    assessment["artifact"] = str(assessment_path.relative_to(shared_root))
    assessment["artifact_sha256"] = hashlib.sha256(
        assessment_path.read_bytes()
    ).hexdigest()
    assessment["recorded_at"] = now()
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] not in {"pending", "needs_decomposition"}:
            current = ticket["state"] if ticket else "missing"
            raise SprintError(f"ticket {key} cannot be scoped from state {current}")
        if cfg.get("runtime_admission") and assessment["verdict"] == "ready":
            prerequisites = raw.get("prerequisites")
            if not isinstance(prerequisites, list):
                raise SprintError(
                    "ready scope must explicitly enumerate prerequisites, including an empty list"
                )
            prerequisites = sorted(set(normalize_key(key) for key in prerequisites))
            missing = sorted(set(prerequisites) - set(ticket.get("dependencies", [])))
            if missing:
                assessment["verdict"] = "operator_decision"
                assessment["decision_kind"] = "dependency_reconciliation"
                assessment["missing_dependencies"] = missing
                assessment["reasons"] = [
                    "dependency reconciliation required before implementation: "
                    + ", ".join(missing)
                ]
            assessment["prerequisites"] = prerequisites
        assessment["inventory_digest"] = scope_digest(ticket)
        if assessment["verdict"] == "tracking_parent":
            children = assessment.get("children")
            if (
                not isinstance(children, list)
                or not children
                or not all(isinstance(child, str) for child in children)
                or sorted(children) != sorted(ticket.get("subtasks", []))
            ):
                raise SprintError(
                    "tracking parent assessment must bind every authenticated child exactly once"
                )
        ticket["scope_assessment"] = assessment
        verdict = assessment["verdict"]
        if verdict == "tracking_parent":
            ticket["state"] = "decomposed"
            ticket["decomposition_children"] = sorted(assessment["children"])
            ticket["reason"] = "existing child chain reconciled by scope assessment"
        elif verdict == "ready":
            ticket["state"] = "pending"
            ticket["reason"] = "scope assessment passed"
        elif verdict == "decompose":
            ticket["state"] = "needs_decomposition"
            ticket["reason"] = (
                f"complexity score {assessment['complexity_score']} requires "
                f"{len(assessment['slices'])} bounded slices"
            )
        elif assessment.get("decision_key") in cfg.get("decision_registry", {}):
            decision_key = assessment["decision_key"]
            already_applied = any(
                item.get("decision_key") == decision_key
                for item in ticket.get("resolved_scope_decisions", [])
            )
            if already_applied:
                ticket["state"] = "operator_decision"
                ticket["reason"] = (
                    f"scoping still requires {decision_key} after its approved "
                    "repository decision was applied"
                )
            else:
                ticket["state"] = "pending"
                ticket["reason"] = (
                    f"repository decision {decision_key} is approved; rescoping with that policy"
                )
                ticket.setdefault("resolved_scope_decisions", []).append(
                    {
                        "at": now(),
                        "decision_key": decision_key,
                        "assessment_sha256": assessment["artifact_sha256"],
                    }
                )
                ticket["scope_assessment"] = {}
        else:
            ticket["state"] = "operator_decision"
            ticket["reason"] = "; ".join(assessment["reasons"])
        ticket["history"].append(
            {"at": now(), "event": "scope-recorded", "verdict": verdict}
        )
        save(path, state)
    emit({"ticket": key, "state": ticket["state"], "assessment": assessment})


def scope_context(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Return only the requested ticket body for one ephemeral scoping pass."""
    _, state = get_state(args, cfg)
    key = normalize_key(args.ticket)
    ticket = state["tickets"].get(key)
    if not ticket:
        raise SprintError(f"ticket {key} is absent from the synchronized sprint")
    emit(
        {
            "ticket": key,
            "summary": ticket.get("summary", ""),
            "description": ticket.get("description", ""),
            "url": ticket.get("url", ""),
            "dependencies": ticket.get("dependencies", []),
            "subtasks": ticket.get("subtasks", []),
            "resolved_decisions": cfg.get("decision_registry", {}),
            "required_slice_contracts": cfg.get("required_slice_contracts", []),
        }
    )


def record_decomposition(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Close a tracking parent after fresh Jira sync proves every created child."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    requested = sorted(
        {normalize_key(value) for value in args.children.split(",") if value.strip()}
    )
    if len(requested) < 2:
        raise SprintError(
            "record-decomposition requires at least two child ticket keys"
        )
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] != "needs_decomposition":
            current = ticket["state"] if ticket else "missing"
            raise SprintError(
                f"ticket {key} cannot record decomposition from state {current}"
            )
        missing = sorted(set(requested) - set(state["tickets"]))
        if missing:
            raise SprintError(
                "decomposition children are absent from inventory: "
                + ", ".join(missing)
            )
        exact_children = requested == sorted(ticket.get("subtasks", []))
        mode = config_scalar_any_depth(
            cfg["config"], "jira_subtask_decomposition_mode", "sibling"
        )
        assessment_slices = (ticket.get("scope_assessment") or {}).get("slices", [])
        slice_by_label = {
            f"orchestration-slice-{key.casefold()}-{slice_.get('id', '')}": slice_
            for slice_ in assessment_slices
            if isinstance(slice_, dict)
        }
        expected_labels = set(slice_by_label)
        observed_labels = {
            label
            for child in requested
            for label in state["tickets"][child].get("labels", [])
            if label in expected_labels
        }
        labels_match = bool(
            len(requested) == len(assessment_slices)
            and len(expected_labels) == len(assessment_slices)
            and observed_labels == expected_labels
            and all(
                len(set(state["tickets"][child].get("labels", [])) & expected_labels)
                == 1
                for child in requested
            )
        )
        expected_issue_type = str(
            config_scalar_any_depth(cfg["config"], "jira_child_issue_type", "Sub-task")
        )
        identity_match = labels_match and all(
            state["tickets"][child].get("is_subtask") is True
            and str(state["tickets"][child].get("issue_type") or "").casefold()
            == expected_issue_type.casefold()
            and any(
                state["tickets"][child].get("summary", "").strip()
                == str(slice_by_label[label].get("summary") or "").strip()
                and decomposition_provenance(key, slice_by_label[label])
                in state["tickets"][child].get("labels", [])
                for label in set(state["tickets"][child].get("labels", []))
                & expected_labels
            )
            for child in requested
        )
        child_by_slice: dict[str, str] = {}
        for child in requested:
            child_labels = set(state["tickets"][child].get("labels", []))
            matching = [
                str(slice_["id"])
                for label, slice_ in slice_by_label.items()
                if label in child_labels
            ]
            if len(matching) == 1:
                child_by_slice[matching[0]] = child
        dependency_match = len(child_by_slice) == len(assessment_slices)
        if dependency_match:
            for slice_ in assessment_slices:
                child = child_by_slice[str(slice_["id"])]
                expected = {
                    child_by_slice[str(dependency)]
                    for dependency in slice_.get("depends_on", [])
                }
                if not expected.issubset(
                    set(state["tickets"][child].get("dependencies", []))
                ):
                    dependency_match = False
                    break
        source_parent = ticket.get("parent") or ""
        sibling_children = bool(
            mode == "sibling"
            and source_parent
            and identity_match
            and all(
                state["tickets"][child].get("parent") == source_parent
                for child in requested
            )
        )
        if not dependency_match:
            raise SprintError(
                "created slices are missing one or more authenticated Jira dependency links"
            )
        if not (exact_children and identity_match) and not sibling_children:
            raise SprintError(
                "created keys must be authoritative children, or verified sibling slices of a Jira subtask"
            )
        ticket["state"] = "decomposed"
        ticket["reason"] = "tracking parent decomposed into " + ", ".join(requested)
        ticket["decomposition_children"] = requested
        ticket["history"].append(
            {"at": now(), "event": "decomposition-recorded", "children": requested}
        )
        save(path, state)
    emit({"ticket": key, "state": "decomposed", "children": requested})


def progress_review_ledger(
    cfg: dict[str, Any], key: str, evidence: str
) -> dict[str, Any]:
    directory = (
        cfg["shared_root"]
        / str(
            load_yaml(cfg["config"]).get(
                "review_ledger_dir", ".orchestration/.review-ledger"
            )
        )
    ).resolve()
    evidence_path = (cfg["shared_root"] / evidence).resolve()
    if evidence_path.parent != directory or evidence_path.suffix != ".json":
        raise SprintError(
            "progress requires this ticket's canonical review ledger file"
        )
    with locked(evidence_path):
        review = read_json(evidence_path, label="progress review ledger")
    subject = review.get("work_subject") or {}
    if subject.get("id") != key or subject.get("repository") != str(
        cfg["shared_root"].resolve()
    ):
        raise SprintError("progress receipt belongs to another ticket or repository")
    return review


def record_progress(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if args.milestone not in PROGRESS_MILESTONES:
        raise SprintError("unsupported progress milestone")
    if not args.evidence.strip():
        raise SprintError("progress evidence must not be empty")
    github_observation = None
    test_observation = None
    initial_ticket = None
    if args.milestone in {"pr_opened", "ci_advanced", "failing_test", "tests_repaired"}:
        # Network requests and test processes never hold the sprint-wide checkpoint lock.
        with locked(path):
            snapshot = load(path)
            initial_ticket = snapshot["tickets"].get(key)
            if not initial_ticket or initial_ticket["state"] not in {
                "running",
                "needs_repair",
                "recoverable",
            }:
                raise SprintError("ticket is not eligible for progress verification")
            require_attempt(initial_ticket, args.attempt_token)
        if args.milestone in {"failing_test", "tests_repaired"}:
            from test_progress import observe, TestProgressError

            try:
                test_observation = observe(
                    cfg["shared_root"],
                    initial_ticket,
                    load_yaml(cfg["config"]),
                    args.milestone,
                    args.evidence,
                )
            except TestProgressError as exc:
                raise SprintError(str(exc)) from exc
        else:
            from github_progress import observe, ProgressError

            try:
                github_observation = observe(
                    cfg["shared_root"], initial_ticket, args.milestone, args.evidence
                )
            except ProgressError as exc:
                raise SprintError(str(exc)) from exc
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] not in {
            "running",
            "needs_repair",
            "recoverable",
        }:
            current = ticket["state"] if ticket else "missing"
            raise SprintError(
                f"ticket {key} cannot record progress from state {current}"
            )
        require_attempt(ticket, args.attempt_token)
        if initial_ticket is not None and ticket != initial_ticket:
            raise SprintError(
                "ticket changed during progress verification; retry with the current attempt"
            )
        verified = False
        fingerprint = args.evidence.strip()
        if args.milestone == "implementation_commit":
            evidence = args.evidence.strip()
            launch = ticket.get("launch_evidence") or {}
            baseline = launch.get("base_commit", "")
            worker_cwd_raw = str(launch.get("worker_cwd") or "")
            if (
                not re.fullmatch(r"[0-9a-f]{40,64}", evidence)
                or not baseline
                or not worker_cwd_raw
                or evidence == baseline
            ):
                raise SprintError(
                    "implementation progress requires a worker-bound new full commit SHA after launch"
                )
            worker_cwd = Path(worker_cwd_raw).resolve()
            try:
                shared_common = subprocess.check_output(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd=cfg["shared_root"],
                    text=True,
                ).strip()
                worker_common = subprocess.check_output(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd=worker_cwd,
                    text=True,
                ).strip()
                worker_head = subprocess.check_output(
                    ["git", "rev-parse", "--verify", "HEAD"],
                    cwd=worker_cwd,
                    text=True,
                ).strip()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise SprintError(
                    "implementation progress requires the authenticated worker checkout"
                ) from exc
            if (
                Path(shared_common).resolve() != Path(worker_common).resolve()
                or worker_head != evidence
            ):
                raise SprintError(
                    "progress commit must be the authenticated worker checkout HEAD"
                )
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", baseline, evidence],
                cwd=worker_cwd,
                capture_output=True,
            )
            changed = subprocess.run(
                ["git", "diff", "--quiet", baseline, evidence, "--"],
                cwd=worker_cwd,
                capture_output=True,
            )
            if ancestor.returncode != 0 or changed.returncode != 1:
                raise SprintError(
                    "progress commit must descend from launch HEAD and change its tree"
                )
            fingerprint = subprocess.check_output(
                ["git", "rev-parse", evidence + "^{tree}"],
                cwd=worker_cwd,
                text=True,
            ).strip()
            ticket.setdefault("verified_commits", {})[evidence] = fingerprint
            verified = True
        if args.milestone == "design_passed":
            review = progress_review_ledger(cfg, key, args.evidence.strip())
            rounds = (review.get("design") or {}).get("rounds", [])
            result = rounds[-1].get("result", {}) if rounds else {}
            if (
                not rounds
                or rounds[-1].get("verdict") != "PASS"
                or not result.get("phase_permit")
                or not any(
                    permit.get("token") == result["phase_permit"]
                    and permit.get("receipt_consumed_at")
                    for permit in review.get("review_permits", [])
                )
            ):
                raise SprintError(
                    "design progress requires a consumed PASS receipt for this ticket"
                )
            fingerprint = str(result["phase_permit"])
            UsageLedger(cfg["shared_root"]).transfer_design_budget(
                key, budgets_from_config(load_yaml(cfg["config"])), fingerprint
            )
            verified = True
        if args.milestone == "review_finding_closed":
            try:
                evidence = json.loads(args.evidence)
                ledger_file, finding = evidence["ledger"], evidence["finding"]
                if (
                    not isinstance(ledger_file, str)
                    or not isinstance(finding, str)
                    or not finding
                ):
                    raise ValueError()
            except (ValueError, TypeError, KeyError) as exc:
                raise SprintError(
                    'finding progress requires JSON with "ledger" and "finding"'
                ) from exc
            review = progress_review_ledger(cfg, key, ledger_file)
            component = review.get("components", {}).get(finding, {})
            finalized = any(
                attempt.get("claims_finalized_at")
                and attempt.get("completed_at")
                and finding in attempt.get("closed", [])
                for attempt in review.get("repair_attempts", [])
            )
            claims = component.get("claims") or {}
            if (
                not finalized
                or component.get("status") != "resolved"
                or not claims
                or any(claim.get("status") != "resolved" for claim in claims.values())
                or review.get("repair_pending_review")
            ):
                raise SprintError(
                    "finding must be closed by finalized independent review, with no open gate claims"
                )
            fingerprint = finding
            verified = True
        if github_observation is not None:
            verified = github_observation["verified"]
            fingerprint = github_observation["fingerprint"]
            receipt = github_observation["receipt"]
            ticket["pr"], ticket["branch"] = receipt["url"], receipt["branch"]
            if args.milestone == "pr_opened":
                identity = ticket.get("worker_identity") or {}
                ticket["recovery_binding"] = {
                    "at": now(),
                    "attempt": int(ticket.get("attempts") or 0),
                    "invocation_id": str(identity.get("invocation_id") or ""),
                    "branch": receipt["branch"],
                    "pr": receipt["url"],
                    "head": receipt["head"],
                    "tree": receipt["tree"],
                }
            if "ci_highest" in github_observation:
                ticket.setdefault("ci_progress", {})[receipt["tree"]] = (
                    github_observation["ci_highest"]
                )
        if test_observation is not None:
            verified = test_observation["verified"]
            fingerprint = test_observation["fingerprint"]
            ticket.setdefault("test_progress", {})[test_observation["definition"]] = (
                test_observation["cases"]
            )
        spent = usage_snapshots(cfg).get(key, {}).get("spent_usd", 0.0)
        event = {
            "verified": verified,
            "fingerprint": fingerprint,
            "at": now(),
            "attempt": int(ticket.get("attempts") or 0),
            "milestone": args.milestone,
            "evidence": args.evidence.strip(),
            "spent_usd": spent,
        }
        if github_observation is not None:
            event["receipt"] = github_observation["receipt"]
        if test_observation is not None:
            event["receipt"] = test_observation["receipt"]
        previous = next(
            (
                item
                for item in ticket.get("progress", [])
                if item.get("milestone") == event["milestone"]
                and item.get("fingerprint", item.get("evidence")) == fingerprint
            ),
            None,
        )
        if previous:
            save(path, state)
            emit(
                {
                    "ticket": key,
                    "state": ticket["state"],
                    "progress": previous,
                    "duplicate": True,
                }
            )
            return
        ticket.setdefault("progress", []).append(event)
        ticket["history"].append({"at": event["at"], "event": "progress", **event})
        save(path, state)
    emit({"ticket": key, "state": ticket["state"], "progress": event})


def legacy_reconciliation(state):
    """Expose mechanical next steps without interpreting old free-text holds as permission."""
    result = []
    for key, ticket in sorted(state["tickets"].items()):
        if ticket["state"] not in {"blocked", "user_action"}:
            continue
        reason = ticket.get("reason", "")
        if reason == "ticket disappeared from the refreshed Jira sprint query":
            action = "refresh_inventory"
        elif ticket.get("subtasks"):
            action = "reconcile_existing_children"
        elif ticket.get("pr"):
            action = "inspect_preserved_pr"
        elif ticket.get("attempts"):
            action = "classify_preserved_outcome"
        else:
            action = "verify_jira_readiness"
        result.append(
            {
                "key": key,
                "state": ticket["state"],
                "reason": reason,
                "next_action": action,
                "pr": ticket.get("pr"),
                "children": ticket.get("subtasks", []),
                "dependencies": ticket.get("dependencies", []),
            }
        )
    return result


def reconcile_legacy(args, cfg):
    """Replace an opaque label without discarding the original decision or execution fence."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] not in {"blocked", "user_action"}:
            raise SprintError(
                "legacy reconciliation requires a blocked or user_action ticket"
            )
        if args.classification not in {"operator_decision", "external_blocked"}:
            raise SprintError("legacy classification cannot authorize launches")
        if not args.reason.strip():
            raise SprintError("legacy reconciliation requires an evidence-based reason")
        # Only non-launching classifications are accepted here. Restart/recovery
        # or authenticated decomposition bindings remain separate transitions.
        ticket.setdefault("history", []).append(
            {
                "at": now(),
                "event": "legacy-classified",
                "previous_state": ticket["state"],
                "previous_reason": ticket.get("reason", ""),
                "state": args.classification,
                "reason": args.reason,
            }
        )
        ticket["state"], ticket["reason"] = args.classification, args.reason
        save(path, state)
    emit({"ticket": key, "state": ticket["state"]})


def progress_spending(ticket, cfg, spent):
    milestones = [item for item in ticket.get("progress", []) if item.get("verified")]
    baseline = max(
        (float(item.get("spent_usd", 0)) for item in milestones), default=0.0
    )
    grant = authorized_restart_grant(cfg["shared_root"], ticket["key"])
    if grant and ticket.get("restart_grant_id") == grant["grant_id"]:
        baseline = max(baseline, float(grant["allowances"]["progress_baseline_usd"]))
    return max(0.0, float(spent) - baseline)


def spending_admission_reason(ticket, cfg, spend):
    if spend.get("state") == "operator_action":
        return "ticket spending or execution-count ceiling requires operator action"
    if (
        progress_spending(ticket, cfg, spend.get("spent_usd", 0))
        >= cfg["max_usd_without_progress"]
    ):
        return "max_usd_without_progress: verified progress or a root-issued restart allowance is required"
    return None


def runtime_admission(cfg, role="sprint-worker"):
    if not cfg.get("runtime_admission"):
        return None
    route = llm_route_from_config(cfg["config"], role)
    if model_less_desktop_route(route):
        status = desktop_subscription_status(route)
        return (
            None
            if status["state"] == "healthy"
            else {"provider": route["provider"], "role": role, **status}
        )
    if not route.get("model"):
        return {
            "provider": route["provider"],
            "state": "unconfigured",
            "reason": "explicit role model required",
        }
    status = ProviderHealth(cfg["shared_root"]).status(
        route["provider"], route_identity(route)
    )
    return (
        {"provider": route["provider"], "role": role, **status}
        if status["state"] != "healthy"
        else None
    )


def preserved_pr_reconciliation_candidate(ticket, cfg, spend):
    """Identify a stopped PR that can be proven and resumed as bounded repair."""
    return bool(
        cfg.get("preserved_pr_auto_recovery")
        and ticket.get("state") == "recoverable"
        and ticket.get("pr")
        and ticket.get("branch")
        and int(ticket.get("attempts") or 0) > 0
        and not spend.get("reserved_usd", 0)
        and (
            ticket.get("verified_commits")
            or any(
                item.get("verified")
                and item.get("milestone") == "implementation_commit"
                for item in ticket.get("progress", [])
            )
        )
    )


def branch_worktree(root: Path, branch: str, configured_base: str) -> Path:
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SprintError("cannot enumerate preserved worktrees") from exc
    records = result.stdout.strip().split("\n\n") if result.stdout.strip() else []
    matches = []
    for record in records:
        fields = {}
        for line in record.splitlines():
            name, _, value = line.partition(" ")
            fields[name] = value
        if fields.get("branch") == f"refs/heads/{branch}" and fields.get("worktree"):
            matches.append(Path(fields["worktree"]).resolve())
    if len(matches) != 1:
        raise SprintError("preserved PR branch must identify exactly one worktree")
    base = Path(configured_base)
    base = (base if base.is_absolute() else root / base).resolve()
    try:
        matches[0].relative_to(base)
    except ValueError as exc:
        raise SprintError(
            "preserved PR worktree is outside the configured worktree root"
        ) from exc
    return matches[0]


def worktree_is_quiescent(path: Path) -> bool:
    """Require a clean Linux worktree with no process cwd or open fd beneath it."""
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return False
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if status.stdout.strip():
        return False
    prefix = str(path) + os.sep
    for process in Path("/proc").glob("[0-9]*"):
        if process.name == str(os.getpid()):
            continue
        candidates = [process / "cwd"]
        try:
            candidates.extend((process / "fd").iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
        for candidate in candidates:
            try:
                target = str(candidate.resolve(strict=True))
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            if target == str(path) or target.startswith(prefix):
                return False
    return True


def worktree_revision(path: Path) -> dict[str, str]:
    """Return the exact commit and tree currently checked out by a worktree."""
    values = {}
    for name, revision in (("head", "HEAD"), ("tree", "HEAD^{tree}")):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", revision],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SprintError("cannot verify preserved PR worktree revision") from exc
        value = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise SprintError("preserved PR worktree returned an invalid revision")
        values[name] = value
    return values


def verify_worktree_receipt(path: Path, receipt: dict[str, Any]) -> None:
    revision = worktree_revision(path)
    if revision["head"] != receipt.get("head") or revision["tree"] != receipt.get(
        "tree"
    ):
        raise SprintError(
            "preserved PR worktree revision differs from the authenticated PR head/tree"
        )


def observe_preserved_pr(
    cfg: dict[str, Any], ticket: dict[str, Any]
) -> dict[str, Any]:
    from github_progress import ProgressError, observe

    try:
        return observe(cfg["shared_root"], ticket, "pr_opened", str(ticket["pr"]))
    except ProgressError as exc:
        raise SprintError(str(exc)) from exc


def verify_recovery_binding(
    cfg: dict[str, Any], ticket: dict[str, Any]
) -> dict[str, Any] | None:
    binding = ticket.get("recovery_binding") or {}
    if not binding:
        return
    if binding.get("kind") != "preserved_pr":
        if binding.get("recovery_id") or binding.get("worktree"):
            raise SprintError("preserved PR recovery binding has an invalid kind")
        return
    observation = observe_preserved_pr(cfg, ticket)
    receipt = observation["receipt"]
    for field in ("branch", "url", "head", "tree"):
        binding_field = "pr" if field == "url" else field
        if receipt.get(field) != binding.get(binding_field):
            raise SprintError(
                "preserved PR changed after recovery authentication; reconcile again"
            )
    configured_base = config_scalar(
        cfg["config"], "worktree_base", ".claude/worktrees"
    )
    worktree = branch_worktree(
        cfg["shared_root"], str(binding["branch"]), configured_base
    )
    if str(worktree) != binding.get("worktree"):
        raise SprintError("preserved PR recovery worktree binding changed")
    if not worktree_is_quiescent(worktree):
        raise SprintError("preserved PR recovery worktree is no longer quiescent")
    verify_worktree_receipt(worktree, receipt)
    return binding


def reconcile_preserved_pr(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Convert a mechanically verified stopped PR into a bounded repair continuation."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        spend = usage_snapshots(cfg).get(key, {})
        if not ticket or not preserved_pr_reconciliation_candidate(ticket, cfg, spend):
            raise SprintError(
                f"ticket {key} is not eligible for preserved PR reconciliation"
            )
        snapshot = json.loads(json.dumps(ticket))
    observation = observe_preserved_pr(cfg, snapshot)
    configured_base = config_scalar(cfg["config"], "worktree_base", ".claude/worktrees")
    worktree = branch_worktree(
        cfg["shared_root"], str(snapshot["branch"]), configured_base
    )
    if not worktree_is_quiescent(worktree):
        raise SprintError(
            "preserved PR worktree is dirty, active, or cannot be proven quiescent"
        )
    verify_worktree_receipt(worktree, observation["receipt"])
    identity = snapshot.get("worker_identity")
    mechanically_absent = (
        isinstance(identity, dict)
        and identity.get("kind") == "execution_unit"
        and execution_unit_status(identity) == "absent"
    )
    operator_attested = not mechanically_absent
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if ticket != snapshot:
            raise SprintError("ticket changed during preserved PR verification; retry")
        refreshed = observe_preserved_pr(cfg, ticket)
        if refreshed["receipt"] != observation["receipt"]:
            raise SprintError("preserved PR changed during authentication; retry")
        if not worktree_is_quiescent(worktree):
            raise SprintError(
                "preserved PR worktree changed or became active during verification"
            )
        verify_worktree_receipt(worktree, observation["receipt"])
        recovery_id = "recovery_" + uuid.uuid4().hex
        try:
            UsageLedger(cfg["shared_root"]).fence_recovery(key, recovery_id)
        except Exception as exc:
            raise SprintError(str(exc)) from exc
        if operator_attested:
            try:
                consume_recovery(
                    cfg["shared_root"],
                    key,
                    int(snapshot.get("attempts") or 0),
                    operator_capability(args),
                )
            except AuthorityError as exc:
                UsageLedger(cfg["shared_root"]).release_recovery_fence(
                    key, recovery_id
                )
                raise SprintError(
                    "preserved PR execution unit is not proven absent; "
                    "a separately issued one-shot recovery capability is required: "
                    + str(exc)
                ) from exc
        ticket["state"] = "pending"
        ticket["reason"] = (
            "preserved PR and clean quiescent worktree verified for bounded repair"
        )
        ticket["next_launch_continuation"] = True
        ticket["run_ref"] = ""
        ticket["attempt_token"] = ""
        ticket["attempt_capability"] = {}
        ticket["worker_identity"] = ""
        ticket["attach_capability"] = ""
        ticket["attached_at"] = ""
        ticket["launch_evidence"] = {}
        ticket["recovery_binding"] = {
            "kind": "preserved_pr",
            "recovery_id": recovery_id,
            "absence_proof": "operator-capability" if operator_attested else "execution-unit",
            "at": now(),
            "attempt": int(ticket.get("attempts") or 0),
            "invocation_id": str(
                (ticket.get("last_terminal") or {}).get("invocation_id") or ""
            ),
            "worktree": str(worktree),
            "branch": observation["receipt"]["branch"],
            "pr": observation["receipt"]["url"],
            "head": observation["receipt"]["head"],
            "tree": observation["receipt"]["tree"],
        }
        ticket.setdefault("history", []).append(
            {
                "at": now(),
                "event": "preserved-pr-reconciled",
                "binding": ticket["recovery_binding"],
            }
        )
        save(path, state)
    emit(
        {
            "ticket": key,
            "state": "needs_repair",
            "recovery_binding": ticket["recovery_binding"],
        }
    )


def health_check(args, cfg):
    emit(probe(cfg["shared_root"], cfg["config"], args.role, args.after_repair))


def plan_value(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    runtime_hold = runtime_admission(cfg)
    scope_hold = runtime_admission(cfg, "ticket-scoper")
    health_probes = [
        dict(
            provider=hold["provider"],
            role=hold.get("role", "sprint-worker"),
            retry_at=max(hold.get("retry_at", 0), hold.get("probe_until", 0)),
        )
        for hold in (runtime_hold, scope_hold)
        if hold
        and hold["state"] in {"unverified", "rate_limited", "transport"}
        and hold.get("probe_count", 0) < 3
    ]
    spend = usage_snapshots(cfg)
    cycles = find_cycles(state["tickets"])
    running = sorted(
        key for key, ticket in state["tickets"].items() if ticket["state"] == "running"
    )
    ordered = sorted(state["tickets"].values(), key=order_key)
    admission_reasons = {
        ticket["key"]: sorted(
            set(
                blockers(state, ticket["key"], cfg, cycles)
                + ([reason] if (reason := attempt_limit_reason(ticket, cfg)) else [])
                + (
                    [reason]
                    if (
                        reason := spending_admission_reason(
                            ticket, cfg, spend.get(ticket["key"], {})
                        )
                    )
                    else []
                )
            )
        )
        for ticket in ordered
        if ticket["state"] == "pending"
    }
    scope_candidates = sorted(
        ticket["key"]
        for ticket in ordered
        if ticket["state"] == "pending"
        and not admission_reasons[ticket["key"]]
        and not ticket.get("scope_assessment")
        and (cfg["auto_decompose_large_tickets"] or cfg.get("runtime_admission"))
        and spend.get(ticket["key"], {}).get("state") != "operator_action"
    )
    scope = [] if scope_hold else scope_candidates
    ready = [
        ticket["key"]
        for ticket in ordered
        if ticket["state"] == "pending"
        and not admission_reasons[ticket["key"]]
        and (
            not (cfg["auto_decompose_large_tickets"] or cfg.get("runtime_admission"))
            or (ticket.get("scope_assessment") or {}).get("verdict") == "ready"
        )
        and spend.get(ticket["key"], {}).get("state") != "operator_action"
    ]
    waiting = [
        {
            "key": ticket["key"],
            "priority": ticket.get("priority"),
            "reasons": admission_reasons[ticket["key"]],
        }
        for ticket in ordered
        if ticket["state"] == "pending" and admission_reasons[ticket["key"]]
    ]
    decomposition, repair, recovery = [], [], []
    preserved_candidates = sorted(
        ticket["key"]
        for ticket in ordered
        if preserved_pr_reconciliation_candidate(
            ticket, cfg, spend.get(ticket["key"], {})
        )
    )
    pr_reconciliation = []
    pr_reconciliation_requires_authority = []
    for key in preserved_candidates:
        identity = state["tickets"][key].get("worker_identity")
        if (
            isinstance(identity, dict)
            and identity.get("kind") == "execution_unit"
            and execution_unit_status(identity) == "absent"
        ):
            pr_reconciliation.append(key)
        else:
            pr_reconciliation_requires_authority.append(key)
    pr_reconciliation_set = set(preserved_candidates)
    recovery_waiting = []
    retry_waiting = []
    decisions = []
    for ticket in ordered:
        key, status = ticket["key"], ticket["state"]
        if key in pr_reconciliation_set:
            if key in pr_reconciliation_requires_authority:
                decisions.append(
                    {
                        "key": key,
                        "reason": (
                            "preserved PR execution absence requires a separately "
                            "issued one-shot recovery capability"
                        ),
                        "action": "reconcile-preserved-pr-with-operator-capability",
                    }
                )
            continue
        failure = (
            current_startup_failure(ticket, cfg) if status == "recoverable" else None
        )
        if failure:
            try:
                retry_at = (
                    datetime.fromisoformat(
                        failure["finished_at"].replace("Z", "+00:00")
                    ).timestamp()
                    + 30
                )
            except (ValueError, TypeError):
                retry_at = 0
            if time.time() < retry_at and attempt_limit_reason(ticket, cfg) is None:
                retry_waiting.append(
                    {
                        "key": key,
                        "retry_at": retry_at,
                        "reason": "provider startup cooldown",
                    }
                )
                continue
        if status in {"completed", "decomposed", "running"}:
            continue
        reasons = []
        if reason := spending_admission_reason(ticket, cfg, spend.get(key, {})):
            reasons.append(reason)
        if status in {"pending", "recoverable", "needs_repair"}:
            if reason := attempt_limit_reason(ticket, cfg):
                reasons.append(reason)
        if status in {
            "operator_decision",
            "user_action",
            "blocked",
            "external_blocked",
        }:
            reasons.append(ticket.get("reason") or status)
        if status == "needs_decomposition" and not cfg["auto_decompose_large_tickets"]:
            reasons.append("automatic decomposition is disabled by repository policy")
        if status in {"recoverable", "needs_repair"}:
            reasons.extend(blockers(state, key, cfg, cycles))
            identity = ticket.get("worker_identity")
            unit_status = (
                execution_unit_status(identity)
                if isinstance(identity, dict)
                and identity.get("kind") == "execution_unit"
                else "unknown"
            )
            if unit_status == "live":
                recovery_waiting.append(key)
            if not ticket.get("attempt_token"):
                reasons.append("recovery requires the current attempt token")
            elif unit_status == "live":
                if not reasons:
                    continue
            elif not automatic_recovery_available(ticket, cfg, unit_status):
                reasons.append("recovery requires external execution-unit authority")
        if reasons:
            decisions.append(
                {"key": key, "state": status, "reasons": sorted(set(reasons))}
            )
        elif status == "needs_decomposition":
            decomposition.append(key)
        elif status == "needs_repair":
            repair.append(key)
        elif status == "recoverable":
            recovery.append(key)
    # A terminal report can precede process exit. Do not reuse its lane while
    # the previous execution unit is still alive, even when admission is paused.
    occupied = len(running) + len(recovery_waiting)
    unfinished_prs = sorted(
        ticket["key"]
        for ticket in ordered
        if ticket.get("pr") and ticket["state"] not in {"completed", "decomposed"}
    )
    active_unfinished_prs = sorted(
        ticket["key"]
        for ticket in ordered
        if ticket.get("pr")
        and (
            ticket["state"] in {"running", "needs_repair", "recoverable"}
            or (ticket["state"] == "pending" and ticket.get("next_launch_continuation"))
        )
    )
    available = max(0, cfg["concurrency_max"] - occupied)
    finish_first = bool(repair or recovery or pr_reconciliation)
    wip_limited = len(active_unfinished_prs) >= int(
        cfg.get("max_unmerged_prs", cfg["concurrency_max"])
    )
    continuation_ready = [
        key for key in ready if state["tickets"][key].get("next_launch_continuation")
    ]
    pr_ready = [
        key
        for key in ready
        if cfg.get("pr_drain_first")
        and key not in continuation_ready
        and state["tickets"][key].get("pr")
    ]
    fresh_ready = [
        key for key in ready if key not in continuation_ready and key not in pr_ready
    ]
    if finish_first:
        launch = []
    else:
        launch = (continuation_ready + pr_ready)[:available]
        if not wip_limited and len(launch) < available:
            launch.extend(fresh_ready[: available - len(launch)])
    stalled = []
    for key in running:
        ticket = state["tickets"][key]
        progress = [item for item in ticket.get("progress", []) if item.get("verified")]
        current = float(spend.get(key, {}).get("spent_usd", 0))
        delta = progress_spending(ticket, cfg, current)
        if delta >= cfg["max_usd_without_progress"]:
            stalled.append(
                {
                    "key": key,
                    "usd_since_progress": round(delta, 6),
                    "threshold_usd": cfg["max_usd_without_progress"],
                    "last_milestone": progress[-1].get("milestone")
                    if progress
                    else None,
                }
            )
    needed_roles = set()
    if ready or repair or recovery or running or retry_waiting:
        needed_roles.add("sprint-worker")
    if scope_candidates:
        needed_roles.add("ticket-scoper")
    health_probes = [item for item in health_probes if item["role"] in needed_roles]
    return {
        "sprint": state["sprint"],
        "concurrency_max": cfg["concurrency_max"],
        "work_in_progress": {
            "unfinished_prs": unfinished_prs,
            "active_unfinished_prs": active_unfinished_prs,
            "count": len(active_unfinished_prs),
            "total_visible": len(unfinished_prs),
            "limit": int(cfg.get("max_unmerged_prs", cfg["concurrency_max"])),
            "fresh_launch_paused": finish_first or wip_limited,
            "reason": (
                "finish existing repair or recovery work first"
                if finish_first
                else "unfinished PR limit reached"
                if wip_limited
                else ""
            ),
        },
        "running": running,
        "needs_reconcile": sorted(running + recovery_waiting),
        "launch": [] if runtime_hold else launch,
        "provider_holds": [hold for hold in (runtime_hold, scope_hold) if hold],
        "health_probes": health_probes,
        "scope": scope,
        "decomposition": decomposition,
        "repair": [] if runtime_hold else repair,
        "recovery": [] if runtime_hold else recovery,
        "pr_reconciliation": pr_reconciliation,
        "pr_reconciliation_requires_authority": pr_reconciliation_requires_authority,
        "recovery_waiting": recovery_waiting,
        "retry_waiting": retry_waiting,
        "legacy_reconciliation": legacy_reconciliation(state),
        "decision_queue": decisions,
        "stalled": stalled,
        "waiting": waiting,
        "autonomous_work_remaining": bool(
            health_probes
            or running
            or (launch and not runtime_hold)
            or scope
            or decomposition
            or (repair and not runtime_hold)
            or (recovery and not runtime_hold)
            or pr_reconciliation
            or recovery_waiting
            or (retry_waiting and not runtime_hold)
        ),
        "over_capacity": max(0, occupied - cfg["concurrency_max"]),
        "spend": spend,
    }


def plan(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    with locked(path):
        state = load(path)
        emit(plan_value(state, cfg))


def prepare_batch(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Reserve background lanes and serialize an Anthropic Message Batch.

    Submission remains a host operation so this controller never handles API
    credentials. The request and marker make the asynchronous handoff durable.
    """
    path = state_path(cfg["state_dir"], str(args.sprint))
    try:
        source = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(f"cannot read batch jobs {args.jobs}: {exc}") from exc
    raw_jobs = source.get("jobs") if isinstance(source, dict) else None
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise SprintError("batch jobs must be a non-empty object with a jobs array")
    provider = str(source.get("provider", "anthropic")).strip().casefold()
    if provider not in {"anthropic", "openai"}:
        raise SprintError("batch provider must be anthropic or openai")

    route = (
        llm_route_from_config(cfg["config"], "sprint-worker")
        if cfg.get("runtime_admission")
        else None
    )
    if route and (route["execution"] != "api" or route["provider"] != provider):
        raise SprintError(
            "batch provider must match the configured API sprint-worker route"
        )
    if hold := runtime_admission(cfg):
        raise SprintError(
            "provider admission held: " + json.dumps(hold, sort_keys=True)
        )
    jobs: dict[str, dict[str, Any]] = {}
    for job in raw_jobs:
        if not isinstance(job, dict):
            raise SprintError("each batch job must be an object")
        key = normalize_key(job.get("ticket"))
        if key in jobs:
            raise SprintError(f"duplicate batch ticket: {key}")
        if job.get("background") is not True or job.get("interactive") is not False:
            raise SprintError(f"ticket {key} is not a non-interactive background job")
        params = job.get("params")
        if not isinstance(params, dict):
            raise SprintError(f"ticket {key} batch params must be an object")
        required = (
            ("model", "max_tokens", "messages")
            if provider == "anthropic"
            else ("model", "max_output_tokens", "input")
        )
        missing = [name for name in required if name not in params]
        if missing:
            raise SprintError(
                f"ticket {key} batch params missing: {', '.join(missing)}"
            )
        if params.get("stream"):
            raise SprintError(f"ticket {key} batch params cannot enable streaming")
        input_key = "messages" if provider == "anthropic" else "input"
        if not isinstance(params.get(input_key), list) or not params[input_key]:
            raise SprintError(
                f"ticket {key} batch {input_key} must be a non-empty array"
            )
        if route and params.get("model") != route["model"]:
            raise SprintError("batch model must match the resolved sprint-worker route")
        jobs[key] = params

    batch_id = uuid.uuid4().hex[:16]
    extension = "json" if provider == "anthropic" else "jsonl"
    request_path = cfg["state_dir"] / f"batch-{batch_id}.request.{extension}"
    marker_path = cfg["state_dir"] / f"batch-{batch_id}.state.json"
    with locked(path):
        state = load(path)
        current_plan = plan_value(state, cfg)
        launch_order = current_plan["launch"]
        unexpected = sorted(set(jobs) - set(launch_order))
        if unexpected:
            raise SprintError(
                "batch may contain only currently launchable tickets: "
                + ", ".join(unexpected)
            )
        ordered_keys = [key for key in launch_order if key in jobs]
        requests = []
        marker_jobs = []
        config = load_yaml(cfg["config"])
        limits = budgets_from_config(config)
        usage = UsageLedger(cfg["shared_root"])
        reservations: list[tuple[str, str]] = []
        prepared = []
        for key in ordered_keys:
            limit_reason = attempt_limit_reason(state["tickets"][key], cfg)
            if limit_reason:
                raise SprintError(f"ticket {key} is blocked: {limit_reason}")
            custom_id = f"ticket_{key.replace('-', '_')}_{batch_id}"
            run_ref = f"{provider}-batch:{batch_id}:{custom_id}"
            run_id = f"batch-{batch_id}-{key}"
            params = jobs[key]
            output_cap = int(
                params["max_tokens"]
                if provider == "anthropic"
                else params["max_output_tokens"]
            )
            input_tokens = max(
                1, len(json.dumps(params, separators=(",", ":")).encode("utf-8"))
            )
            projected = Pricing.from_config(config, str(params["model"])).worst_case(
                input_tokens, output_cap
            )
            prepared.append((key, custom_id, run_ref, run_id, projected))
        try:
            for key, custom_id, run_ref, run_id, projected in prepared:
                reservation_id = usage.reserve(
                    projected=projected,
                    limits=limits,
                    run_id=run_id,
                    ticket=key,
                    sprint=str(args.sprint),
                    provider=provider,
                    model=str(jobs[key]["model"]),
                    role="sprint-worker",
                    origin="provider-batch",
                )
                reservations.append((reservation_id, run_id))
                if provider == "anthropic":
                    requests.append({"custom_id": custom_id, "params": jobs[key]})
                else:
                    requests.append(
                        {
                            "custom_id": custom_id,
                            "method": "POST",
                            "url": "/v1/responses",
                            "body": jobs[key],
                        }
                    )
                marker_jobs.append(
                    {
                        "ticket": key,
                        "custom_id": custom_id,
                        "run_ref": run_ref,
                        "run_id": run_id,
                        "reservation_id": reservation_id,
                        "projected_cost_usd": str(projected),
                    }
                )
        except (AgentError, ValueError) as exc:
            for reservation_id, run_id in reservations:
                usage.release(reservation_id, run_id, "batch preparation failed")
            raise SprintError(f"batch budget reservation failed: {exc}") from exc
        for marker_job in marker_jobs:
            key = marker_job["ticket"]
            custom_id = marker_job["custom_id"]
            run_ref = marker_job["run_ref"]
            run_id = marker_job["run_id"]
            ticket = state["tickets"][key]
            if route:
                ticket["reserved_route"] = route
            ticket["state"] = "running"
            ticket["reason"] = ""
            ticket["run_ref"] = run_ref
            ticket["attempts"] += 1
            ticket["attempt_token"] = "attempt_" + uuid.uuid4().hex
            ticket["attempt_capability"] = {
                "token": "attemptcap_" + uuid.uuid4().hex,
                "repository": str(cfg["shared_root"]),
                "sprint": str(args.sprint),
                "ticket": key,
                "role": "sprint-worker",
                "run_id": run_id,
                "worker": run_ref,
                "attempt": ticket["attempts"],
                "issued_at": now(),
            }
            ticket["worker_identity"] = run_ref
            ticket["attach_capability"] = "attachcap_" + uuid.uuid4().hex
            ticket["attached_at"] = ""
            marker_job["attempt_token"] = ticket["attempt_token"]
            marker_job["attempt_capability"] = ticket["attempt_capability"]["token"]
            ticket["history"].append(
                {
                    "at": now(),
                    "event": "batch-reserved",
                    "batch_id": batch_id,
                    "custom_id": custom_id,
                }
            )
        if not requests:
            raise SprintError(
                "none of the supplied batch jobs are currently launchable"
            )
        request = {"requests": requests}
        endpoint = "/v1/messages/batches" if provider == "anthropic" else "/v1/batches"
        if provider == "anthropic":
            write_json(request_path, request)
        else:
            write_jsonl(request_path, requests)
        request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()
        marker = {
            "schema_version": 2,
            "reserved_route": route,
            "batch_id": batch_id,
            "sprint_id": state["sprint"]["id"],
            "provider": provider,
            "status": "pending_submission"
            if provider == "anthropic"
            else "pending_upload",
            "endpoint": endpoint,
            "request_file": str(request_path),
            "request_sha256": request_sha256,
            "provider_batch_id": "",
            "jobs": marker_jobs,
            "created_at": now(),
            "updated_at": now(),
        }
        state.setdefault("batches", {})[batch_id] = marker
        save(path, state)
        write_json(marker_path, marker)
    emit(
        {
            "batch_id": batch_id,
            "provider": provider,
            "status": marker["status"],
            "request": str(request_path),
            "marker": str(marker_path),
            "tickets": ordered_keys,
        }
    )


def run_batch_adapter(
    action: str,
    marker_path: Path,
    cfg: dict[str, Any],
    *,
    in_process_runner: Any | None = None,
) -> dict[str, Any]:
    if in_process_runner is not None:
        value = in_process_runner(action, marker_path, cfg)
        if not isinstance(value, dict):
            raise SprintError("provider batch adapter returned an invalid receipt")
        return value
    adapter = Path(__file__).with_name("provider_batch_adapter.py")
    command = [
        sys.executable,
        str(adapter),
        action,
        "--marker",
        str(marker_path),
    ]
    if action == "fetch":
        command.extend(["--output-dir", str(cfg["state_dir"])])
    try:
        result = subprocess.run(
            command, cwd=cfg["shared_root"], check=True, capture_output=True, text=True
        )
        value = json.loads(result.stdout)
    except (OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SprintError(
            f"provider batch {action} did not produce authoritative adapter evidence; uncertainty remains reserved"
        ) from exc
    if not isinstance(value, dict):
        raise SprintError("provider batch adapter returned an invalid receipt")
    return value


def load_batch_marker(marker_path: Path, *, migrate: bool = True) -> dict[str, Any]:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError("batch marker is unreadable") from exc
    if marker.get("schema_version") == 1:
        if not migrate:
            return marker
        legacy_digest = hashlib.sha256(
            json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        marker.update(
            {
                "schema_version": 2,
                "status": "legacy_uncertain",
                "legacy_marker_sha256": legacy_digest,
                "legacy_status": str(marker.get("status") or "unknown"),
                "operator_recovery_required": True,
                "updated_at": now(),
            }
        )
        write_json(marker_path, marker)
    if marker.get("schema_version") != 2:
        raise SprintError("unsupported batch marker schema")
    return marker


def inspect_batch(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    marker_path = cfg["state_dir"] / f"batch-{args.batch}.state.json"
    if not marker_path.is_file():
        raise SprintError(f"batch marker not found: {marker_path}")
    with locked(marker_path):
        marker = load_batch_marker(marker_path)
        emit(
            {
                "batch_id": args.batch,
                "status": marker.get("status"),
                "provider_batch_id": marker.get("provider_batch_id") or "",
                "reservations_fenced": marker.get("status")
                in {"legacy_uncertain", "legacy_operator_action"},
                "operator_recovery_required": bool(
                    marker.get("operator_recovery_required")
                ),
            }
        )


def recover_legacy_batch(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    marker_path = cfg["state_dir"] / f"batch-{args.batch}.state.json"
    if not args.reason.strip():
        raise SprintError("legacy batch recovery requires an operator reason")
    with locked(marker_path):
        marker = load_batch_marker(marker_path)
        if marker.get("status") != "legacy_uncertain":
            raise SprintError("batch is not awaiting legacy operator recovery")
        marker.update(
            {
                "status": "legacy_operator_action",
                "operator_recovery_required": False,
                "operator_reason": args.reason.strip(),
                "reservations_released": False,
                "updated_at": now(),
            }
        )
        write_json(marker_path, marker)
    emit(
        {
            "batch_id": args.batch,
            "status": "legacy_operator_action",
            "reservations_fenced": True,
        }
    )


def submit_batch(
    args: argparse.Namespace, cfg: dict[str, Any], in_process_runner: Any | None = None
) -> None:
    """Submit a prepared request through the credential-owning provider adapter."""
    marker_path = cfg["state_dir"] / f"batch-{args.batch}.state.json"
    if not marker_path.is_file():
        raise SprintError(f"batch marker not found: {marker_path}")
    marker = load_batch_marker(marker_path)
    if marker.get("status") in {"legacy_uncertain", "legacy_operator_action"}:
        raise SprintError(
            "legacy batch is fenced; run inspect-batch for operator recovery"
        )
    if cfg.get("runtime_admission"):
        if marker.get("reserved_route") != llm_route_from_config(
            cfg["config"], "sprint-worker"
        ):
            raise SprintError(
                "batch route changed after preparation; reconcile before submission"
            )
        if hold := runtime_admission(cfg):
            raise SprintError(
                "provider admission held: " + json.dumps(hold, sort_keys=True)
            )
    receipt = run_batch_adapter(
        "submit", marker_path, cfg, in_process_runner=in_process_runner
    )
    marker = load_batch_marker(marker_path)
    if (
        marker.get("status") != "submitted"
        or receipt.get("provider_batch_id") != marker.get("provider_batch_id")
        or receipt.get("request_sha256") != marker.get("request_sha256")
        or receipt.get("sha256")
        != hashlib.sha256(
            json.dumps(
                {key: value for key, value in receipt.items() if key != "sha256"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    ):
        raise SprintError("provider adapter acceptance receipt is invalid")
    emit(
        {
            "batch_id": args.batch,
            "provider_batch_id": marker["provider_batch_id"],
            "status": "submitted",
        }
    )


def reconcile_batch(
    args: argparse.Namespace, cfg: dict[str, Any], in_process_runner: Any | None = None
) -> None:
    marker_path = cfg["state_dir"] / f"batch-{args.batch}.state.json"
    with locked(marker_path):
        _reconcile_batch_locked(args, cfg, in_process_runner)


def _reconcile_batch_locked(
    args: argparse.Namespace, cfg: dict[str, Any], in_process_runner: Any | None = None
) -> None:
    """Apply one immutable adapter-owned terminal bundle exactly once per job."""
    marker_path = cfg["state_dir"] / f"batch-{args.batch}.state.json"
    if not marker_path.is_file():
        raise SprintError(f"batch marker not found: {marker_path}")
    marker = load_batch_marker(marker_path)
    if args.provider_evidence or args.results or args.provider_batch_id:
        raise SprintError(
            "caller-authored provider identity, evidence, and results are never authoritative"
        )
    if marker.get("status") not in {
        "submitted",
        "reconciling",
        "completed",
        "failed",
        "completed_with_failures",
        "completed_with_uncertainty",
    }:
        raise SprintError("batch has no certain adapter-owned provider submission")
    bundle_ref = marker.get("terminal_bundle")
    if bundle_ref:
        bundle_path = Path(str(bundle_ref.get("path") or "")).resolve()
    else:
        bundle_ref = run_batch_adapter(
            "fetch", marker_path, cfg, in_process_runner=in_process_runner
        )
        bundle_path = Path(str(bundle_ref.get("path") or "")).resolve()
    if (
        bundle_path != cfg["shared_root"]
        and cfg["shared_root"] not in bundle_path.parents
    ):
        raise SprintError("provider terminal bundle escapes the shared repository")
    try:
        evidence = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(
            "provider terminal bundle is unreadable; reservations remain fenced"
        ) from exc
    evidence_digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        bundle_ref.get("sha256") != evidence_digest
        or bundle_path.name
        != f"batch-{args.batch}.terminal.sha256-{evidence_digest}.json"
    ):
        raise SprintError(
            "provider terminal bundle is not immutable and content-addressed"
        )
    expected_jobs = sorted(str(item["custom_id"]) for item in marker.get("jobs", []))
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != 2
        or evidence.get("adapter") != f"{marker.get('provider')}-batch"
        or evidence.get("authority") != "provider-network"
        or evidence.get("batch_id") != marker.get("batch_id")
        or evidence.get("provider_batch_id") != marker.get("provider_batch_id")
        or evidence.get("request_sha256") != marker.get("request_sha256")
        or evidence.get("acceptance_receipt_sha256")
        != marker.get("acceptance_receipt", {}).get("sha256")
        or evidence.get("acceptance_receipt") != marker.get("acceptance_receipt")
        or sorted(evidence.get("job_ids") or []) != expected_jobs
    ):
        raise SprintError(
            "provider batch evidence does not bind this provider, batch, and exact job set"
        )
    if evidence.get("authority") == "provider-network" and not re.fullmatch(
        r"https://[A-Za-z0-9.-]+(?::[0-9]+)?",
        str(evidence.get("approved_origin") or ""),
    ):
        raise SprintError("provider batch evidence has no approved HTTPS origin")
    raw_refs = [evidence.get("acceptance_receipt", {}).get("raw")] + list(
        evidence.get("raw_pages", [])
    )
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, dict):
            raise SprintError("provider evidence has an invalid raw page reference")
        raw_path = Path(str(raw_ref.get("path") or "")).resolve()
        if (
            raw_path != cfg["shared_root"]
            and cfg["shared_root"] not in raw_path.parents
        ):
            raise SprintError("provider raw evidence escapes the shared repository")
        raw_value = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_digest = hashlib.sha256(
            json.dumps(raw_value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            raw_ref.get("sha256") != raw_digest
            or raw_path.name != f"sha256-{raw_digest}.json"
        ):
            raise SprintError("provider raw evidence is not content-addressed")
    if evidence.get("status") not in {
        "completed",
        "ended",
        "failed",
        "cancelled",
        "expired",
    }:
        raise SprintError("provider status is not terminal")
    if marker.get("status") in {
        "completed",
        "failed",
        "completed_with_failures",
        "completed_with_uncertainty",
    }:
        if marker.get("terminal_bundle", {}).get("sha256") != evidence_digest:
            raise SprintError("completed batch reconciliation evidence is immutable")
        emit({"batch_id": args.batch, "status": marker["status"]})
        return
    usage_ledger = UsageLedger(cfg["shared_root"])
    rows = evidence.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SprintError("batch results require a normalized jobs array")
    results_by_custom = {str(row.get("custom_id")): row for row in rows}
    unresolved = set(evidence.get("unresolved_job_ids") or [])
    if (
        len(results_by_custom) != len(rows)
        or set(results_by_custom) & unresolved
        or set(results_by_custom) | unresolved != set(expected_jobs)
    ):
        raise SprintError(
            "batch results and unresolved jobs must partition reservations"
        )
    if (
        evidence.get("results_sha256")
        != hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise SprintError("normalized provider results digest is invalid")
    config = load_yaml(cfg["config"])
    reservation_events = {
        str(event.get("reservation_id")): event
        for event in usage_ledger._events()
        if event.get("kind") == "reservation"
    }
    journal = marker.setdefault("application_journal", {})
    # Freeze the accepted bundle, normalized results, and every per-job intent
    # before the first ledger settlement or requeue can occur.
    marker.update(
        {
            "status": "reconciling",
            "terminal_bundle": bundle_ref,
            "provider_terminal_status": evidence["status"],
            "results_sha256": evidence.get("results_sha256"),
            "updated_at": now(),
        }
    )
    for item in marker["jobs"]:
        row = results_by_custom.get(item["custom_id"], {})
        job_outcome = str(row.get("outcome") or "ambiguous")
        result_digest = hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        prior = journal.get(item["custom_id"])
        intent = {
            "outcome": job_outcome,
            "result_sha256": result_digest,
            "ledger_applied": job_outcome == "ambiguous",
            "state_applied": job_outcome in {"completed", "ambiguous"},
        }
        if prior and (
            prior.get("outcome") != job_outcome
            or prior.get("result_sha256") != result_digest
        ):
            raise SprintError("frozen per-job reconciliation intent changed")
        journal.setdefault(item["custom_id"], intent)
    write_json(marker_path, marker)
    for item in marker["jobs"]:
        entry = journal[item["custom_id"]]
        row = results_by_custom.get(item["custom_id"], {})
        if not entry["ledger_applied"] and entry["outcome"] == "failed":
            if row.get("provider_proven_nonexecuted") is not True:
                raise SprintError("failed provider row is not proven nonexecuted")
            usage_ledger.release(
                item["reservation_id"], item["run_id"], "provider batch failed"
            )
            entry["ledger_applied"] = True
            write_json(marker_path, marker)
        elif not entry["ledger_applied"]:
            usage = row.get("usage")
            response_id = str(row.get("response_id") or "")
            event = reservation_events.get(str(item["reservation_id"]))
            if (
                not isinstance(usage, dict)
                or sum(int(x) for x in usage.values()) <= 0
                or not response_id
                or not event
            ):
                raise SprintError(
                    f"batch result for {item['ticket']} requires reservation, response id, and usage"
                )
            model = str(event["model"])
            usage_ledger.settle(
                item["reservation_id"],
                run_id=item["run_id"],
                ticket=item["ticket"],
                sprint=str(marker["sprint_id"]),
                provider=str(marker["provider"]),
                model=model,
                response_id=response_id,
                usage=usage,
                cost=Pricing.from_config(config, model).actual_cost(usage),
                role="sprint-worker",
            )
            entry["ledger_applied"] = True
            write_json(marker_path, marker)
        if not entry["state_applied"]:
            checkpoint = state_path(cfg["state_dir"], str(marker["sprint_id"]))
            with locked(checkpoint):
                state = load(checkpoint)
                ticket = state["tickets"].get(item["ticket"])
                if (
                    ticket
                    and ticket.get("state") == "running"
                    and ticket.get("run_ref") == item["run_ref"]
                ):
                    ticket.update(
                        {
                            "state": "pending",
                            "reason": "provider batch failed before worker output",
                            "run_ref": "",
                            "attempt_token": "",
                            "attempt_capability": {},
                        }
                    )
                    ticket["history"].append(
                        {"at": now(), "event": "batch-failed-requeued"}
                    )
                save(checkpoint, state)
            entry["state_applied"] = True
            write_json(marker_path, marker)
    outcomes = {entry["outcome"] for entry in journal.values()}
    marker["status"] = (
        "completed_with_uncertainty"
        if "ambiguous" in outcomes
        else "failed"
        if outcomes == {"failed"}
        else "completed"
        if outcomes == {"completed"}
        else "completed_with_failures"
    )
    marker["updated_at"] = now()
    write_json(marker_path, marker)
    emit({"batch_id": args.batch, "status": marker["status"]})


def reserve(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.run_ref.strip():
        raise SprintError("run reference must not be empty")
    if hold := runtime_admission(cfg):
        raise SprintError(
            "provider admission held: " + json.dumps(hold, sort_keys=True)
        )
    with locked(path):
        state = load(path)
        if key not in state["tickets"]:
            raise SprintError(f"ticket {key} is not in the sprint checkpoint")
        ticket = state["tickets"][key]
        if ticket["state"] != "pending":
            raise SprintError(
                f"ticket {key} cannot be reserved from state {ticket['state']}"
            )
        verify_recovery_binding(cfg, ticket)
        if (
            cfg.get("runtime_admission")
            and (ticket.get("scope_assessment") or {}).get("verdict") != "ready"
        ):
            raise SprintError("ticket requires bounded pre-implementation scoping")
        reasons = blockers(state, key, cfg)
        if reasons:
            raise SprintError(f"ticket {key} is blocked: {'; '.join(reasons)}")
        running = sum(
            1
            for value in state["tickets"].values()
            if value["state"] == "running"
            or (
                value["state"] in {"recoverable", "needs_repair"}
                and isinstance(value.get("worker_identity"), dict)
                and value["worker_identity"].get("kind") == "execution_unit"
                and execution_unit_status(value["worker_identity"]) == "live"
            )
        )
        if running >= cfg["concurrency_max"]:
            raise SprintError(
                f"concurrency_max={cfg['concurrency_max']} is already reached"
            )
        if not ticket.get("next_launch_continuation"):
            unfinished_prs = [
                value["key"]
                for value in state["tickets"].values()
                if value.get("pr")
                and value["state"] in {"running", "needs_repair", "recoverable"}
            ]
            if len(unfinished_prs) >= int(
                cfg.get("max_unmerged_prs", cfg["concurrency_max"])
            ):
                raise SprintError(
                    "unfinished PR limit is already reached; finish repair/recovery work first"
                )
        limit_reason = attempt_limit_reason(ticket, cfg)
        if limit_reason:
            raise SprintError(f"ticket {key} is blocked: {limit_reason}")
        if reason := spending_admission_reason(
            ticket, cfg, usage_snapshots(cfg).get(key, {})
        ):
            raise SprintError(f"ticket {key} is blocked: {reason}")
        current_plan = plan_value(state, cfg)
        if key not in current_plan["launch"]:
            raise SprintError(
                f"ticket {key} is not in the controller's current launch plan; "
                "finish repair, recovery, continuation, or WIP work first"
            )
        if cfg.get("runtime_admission"):
            ticket["reserved_route"] = llm_route_from_config(
                cfg["config"], "sprint-worker"
            )
        ticket["state"] = "running"
        ticket["reason"] = ""
        ticket["run_ref"] = args.run_ref
        requested_continuation = bool(ticket.pop("next_launch_continuation", False))
        continuation = requested_continuation and int(
            ticket.get("continuations") or 0
        ) < int(cfg.get("max_worker_continuations", 6))
        ticket["attempts"] += 1
        if continuation:
            ticket["continuations"] = int(ticket.get("continuations") or 0) + 1
        else:
            ticket["charged_attempts"] = (
                int(ticket.get("charged_attempts", ticket["attempts"] - 1) or 0) + 1
            )
        ticket["attempt_token"] = "attempt_" + uuid.uuid4().hex
        capability_run_id = args.run_id or args.run_ref
        capability = {
            "token": "attemptcap_" + uuid.uuid4().hex,
            "repository": str(cfg["shared_root"]),
            "sprint": str(args.sprint),
            "ticket": key,
            "attempt": ticket["attempts"],
            "role": args.role,
            "run_id": capability_run_id,
            "worker": args.worker_ref or args.run_ref,
            "issued_at": now(),
        }
        ticket["attempt_capability"] = capability
        # Reservation references are provisional routing/display values. Only
        # the one-use attach transition can establish the actual worker whose
        # liveness later authorizes an automatic requeue.
        ticket["worker_identity"] = ""
        ticket["attach_capability"] = "attachcap_" + uuid.uuid4().hex
        ticket["attached_at"] = ""
        ticket["launch_evidence"] = {}
        # Terminal evidence belongs to the attempt that produced it. A later
        # crash must never inherit an earlier timeout's continuation credit.
        ticket["last_terminal"] = {}
        event = {
            "at": now(),
            "event": "reserved",
            "run_ref": args.run_ref,
            "continuation": continuation,
        }
        ticket["history"].append(event)
        save(path, state)
    emit(
        {
            "ticket": key,
            "state": "running",
            "run_ref": args.run_ref,
            "attempt_token": ticket["attempt_token"],
            "attempt_capability": capability["token"],
            "attach_capability": ticket["attach_capability"],
            "attempt": ticket["attempts"],
        }
    )


def require_attempt(ticket: dict[str, Any], supplied: str) -> None:
    expected = str(ticket.get("attempt_token") or "")
    if not expected or supplied != expected:
        raise SprintError(
            "attempt token is missing or stale; refusing cross-attempt state mutation"
        )


def _repository_path(root: Path, raw: str, *, label: str) -> Path:
    path = Path(raw)
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise SprintError(f"{label} must stay inside the shared repository") from exc
    return resolved


def linux_systemd_scope_available() -> bool:
    if (
        not sys.platform.startswith("linux")
        or not Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    ):
        return False
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    if os.environ.get("ORCHESTRATION_TEST_MODE") == "1":
        return False
    try:
        check = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return check.returncode == 0


def wait_for_runtime_record(
    path: Path, process: subprocess.Popen[Any], label: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if path.is_file():
            last = read_json(path, label=label)
            if label == "supervisor readiness" or last.get("phase") in {
                "launched",
                "terminal",
            }:
                return last
        if process.poll() is not None:
            break
        time.sleep(0.02)
    detail = f" (exit {process.returncode})" if process.poll() is not None else ""
    raise SprintError(f"controller timed out waiting for {label}{detail}")


def lane_invocation_matches(ticket, invocation):
    return bool(invocation) and any(
        isinstance(value, dict) and value.get("invocation_id") == invocation
        for value in (ticket.get("launch_evidence"), ticket.get("worker_identity"))
    )


def signal_process_group(child: subprocess.Popen[Any], signum: int) -> None:
    """Signal a worker group; suppress EPERM only after proven termination."""
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        return
    except PermissionError:
        if child.poll() is None:
            raise


def authenticate_supervisor_context(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    worker_cwd: Path,
    original_command: list[str],
    *,
    claim: bool,
) -> None:
    checkpoint = state_path(cfg["state_dir"], str(args.sprint))
    # Checkpoint writes are atomic. Do not take the controller lock here: the
    # launching controller deliberately retains it until this supervisor has
    # acknowledged and spawned, which itself fences concurrent mutation.
    state = load(checkpoint)
    ticket = state.get("tickets", {}).get(normalize_key(args.ticket))
    if not isinstance(ticket, dict) or ticket.get("state") != "running":
        raise SprintError("supervisor ticket is not an active controller lane")
    evidence = ticket.get("launch_evidence")
    if not isinstance(evidence, dict):
        raise SprintError("supervisor has no persisted controller launch evidence")
    artifact_paths = {
        "ready_path": args.ready,
        "ack_path": args.ack,
        "tombstone_path": args.tombstone,
        "output_path": args.output,
        "input_path": args.stdin_file or "",
    }
    command_sha256 = hashlib.sha256(
        json.dumps(original_command, separators=(",", ":")).encode()
    ).hexdigest()
    capability_sha256 = hashlib.sha256(
        args.supervisor_capability.encode()
    ).hexdigest()
    if (
        evidence.get("status") != "launching"
        or evidence.get("invocation_id") != args.invocation_id
        or evidence.get("ticket") != normalize_key(args.ticket)
        or str(evidence.get("sprint")) != str(args.sprint)
        or Path(str(evidence.get("repository") or "")).resolve()
        != Path(cfg["shared_root"]).resolve()
        or Path(str(evidence.get("worker_cwd") or "")).resolve() != worker_cwd
        or evidence.get("command_sha256") != command_sha256
        or evidence.get("subscription_route")
        is not bool(getattr(args, "subscription_route", False))
        or evidence.get("supervisor_capability_sha256") != capability_sha256
        or any(evidence.get(key) != value for key, value in artifact_paths.items())
    ):
        raise SprintError("supervisor context differs from persisted launch evidence")
    claim_path = Path(str(evidence.get("supervisor_claim_path") or ""))
    expected_claim_path = Path(args.ready).parent / (
        f"execution-{args.invocation_id}.claim.json"
    )
    if claim_path != expected_claim_path:
        raise SprintError("supervisor claim path differs from persisted launch evidence")
    claim_value = {
        "invocation_id": args.invocation_id,
        "pid": os.getpid(),
        "capability_sha256": capability_sha256,
    }
    if claim:
        try:
            with claim_path.open("x", encoding="utf-8") as handle:
                json.dump(claim_value, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise SprintError("supervisor capability was already claimed") from exc
    elif read_json(claim_path, label="supervisor claim") != claim_value:
        raise SprintError("supervisor claim does not belong to this process")
    recovery_binding = ticket.get("recovery_binding") or None
    if evidence.get("recovery_binding") != recovery_binding:
        raise SprintError("recovery binding changed after launch intent was persisted")
    verified_binding = verify_recovery_binding(cfg, ticket)
    expected_cwd = Path(cfg["shared_root"]).resolve()
    if isinstance(verified_binding, dict):
        expected_cwd = Path(str(verified_binding["worktree"])).resolve()
    if worker_cwd != expected_cwd:
        raise SprintError("worker cwd is not the controller-authenticated checkout")
    try:
        shared_common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cfg["shared_root"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        worker_common_dir = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worker_cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SprintError("worker cwd must be a checkout of the managed repository") from exc
    if Path(shared_common_dir).resolve() != Path(worker_common_dir).resolve():
        raise SprintError("worker cwd belongs to a different git repository")


def supervise_local(args: argparse.Namespace, _cfg: dict[str, Any]) -> None:
    """Internal shim: establish identity before spawn and retain a tombstone."""
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SprintError("supervisor requires a worker command")
    original_command = list(command)
    explicit_worker_cwd = hasattr(args, "worker_cwd")
    worker_cwd = Path(
        getattr(args, "worker_cwd", _cfg["shared_root"])
    ).expanduser().resolve()
    # Production invocations can only arrive through the parser, where this
    # controller-owned argument is required. The fallback exists solely for
    # older in-process test fixtures.
    if explicit_worker_cwd:
        authenticate_supervisor_context(
            args, _cfg, worker_cwd, original_command, claim=True
        )
    ready_path, ack_path = Path(args.ready), Path(args.ack)
    tombstone_path, output_path = Path(args.tombstone), Path(args.output)
    identity = process_identity(str(os.getpid()))
    identity["invocation_id"] = args.invocation_id
    write_json(ready_path, {"phase": "ready", "identity": identity})
    deadline = time.monotonic() + 30
    while not ack_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ack_path.exists():
        terminal = {
            "invocation_id": args.invocation_id,
            "phase": "terminal",
            "spawned": False,
        }
        write_json(tombstone_path, terminal)
        write_json(ready_path, {**terminal, "identity": identity})
        return
    input_handle: Any = subprocess.DEVNULL
    child: subprocess.Popen[Any] | None = None
    gateway = None
    stop_reason = ""
    supervisor_error = ""
    returncode: int | None = None
    gateway_closed = False
    child_env = dict(os.environ)
    # Older internal callers and recovery fixtures construct the supervisor
    # namespace directly, so absence means the existing metered desktop path.
    subscription_route = bool(getattr(args, "subscription_route", False))
    started = time.monotonic()
    last_activity = started
    last_output_size = output_path.stat().st_size if output_path.exists() else 0
    last_progress_count = 0
    checkpoint = state_path(_cfg["state_dir"], args.sprint)
    legacy_timeout = bool(_cfg.get("legacy_worker_timeout"))
    if "legacy_worker_timeout" not in _cfg:
        legacy_timeout = bool(
            config_scalar_any_depth(_cfg["config"], "max_worker_seconds", "").strip()
        )
    if legacy_timeout:
        max_idle_seconds = max_lifetime_seconds = int(
            _cfg.get("max_worker_idle_seconds")
            or config_scalar_any_depth(_cfg["config"], "max_worker_seconds", "1800")
        )
        idle_reason = lifetime_reason = "max_worker_seconds"
    else:
        max_idle_seconds = int(_cfg.get("max_worker_idle_seconds", 1800))
        max_lifetime_seconds = int(_cfg.get("max_worker_lifetime_seconds", 14400))
        idle_reason = "max_worker_idle_seconds"
        lifetime_reason = "max_worker_lifetime_seconds"

    def forward(signum: int, _frame: Any) -> None:
        if child is not None and child.poll() is None:
            signal_process_group(child, signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        if subscription_route:
            route = llm_route_from_config(_cfg["config"], "sprint-worker")
            if not model_less_desktop_route(route):
                raise SprintError(
                    "subscription launch no longer matches repository policy"
                )
            command = subscription_launch_command(command, route)
            child_env = subscription_child_environment(
                child_env, str(route["provider"])
            )
        elif Path(command[0]).name == "claude":
            from native_gateway import (
                NativeGateway,
                claude_child_environment,
                claude_launch_arguments,
            )

            load_orchestration_env(_cfg["config"])
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SprintError(
                    "native Claude budget enforcement requires ANTHROPIC_API_KEY"
                )
            gateway = NativeGateway(
                _cfg["shared_root"],
                load_yaml(_cfg["config"]),
                args.ticket,
                args.sprint,
                args.invocation_id,
                route_scope=route_identity(
                    llm_route_from_config(_cfg["config"], "sprint-worker")
                ),
            )
            endpoint = gateway.start()
            child_env = claude_child_environment(child_env, gateway.token, endpoint)
            command = claude_launch_arguments(command, gateway.token, endpoint)
        elif Path(command[0]).name == "codex":
            from codex_gateway import (
                CodexGateway,
                child_environment,
                install_launcher,
                launch_arguments,
            )

            load_orchestration_env(_cfg["config"])
            if not os.environ.get("OPENAI_API_KEY"):
                raise SprintError(
                    "native Codex budget enforcement requires OPENAI_API_KEY"
                )
            executable = shutil.which(command[0])
            if not executable:
                raise SprintError("native Codex executable was not found")
            gateway = CodexGateway(
                _cfg["shared_root"],
                load_yaml(_cfg["config"]),
                args.ticket,
                args.sprint,
                args.invocation_id,
                # Client-incompatibility incidents hold only this route/client
                # revision, never the provider, API roles, or running siblings.
                route_scope=route_identity(
                    llm_route_from_config(_cfg["config"], "sprint-worker")
                ),
            )
            endpoint = gateway.start()
            child_env = child_environment(child_env, gateway.token, endpoint)
            install_launcher(
                ready_path.parent / (args.invocation_id + ".bin"), executable, child_env
            )
            command = launch_arguments([executable, *command[1:]], endpoint)
        if args.stdin_file:
            input_handle = Path(args.stdin_file).open("rb")
        if explicit_worker_cwd:
            authenticate_supervisor_context(
                args, _cfg, worker_cwd, original_command, claim=False
            )
        if Path(command[0]).name == "codex":
            route = llm_route_from_config(_cfg["config"], "sprint-worker")
            command = bind_native_working_directory(command, route, worker_cwd)
        with output_path.open("ab") as output_handle:
            child = subprocess.Popen(
                command,
                cwd=worker_cwd,
                stdin=input_handle,
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_env,
            )
            write_json(
                ready_path,
                {"phase": "launched", "identity": identity, "worker_pid": child.pid},
            )
            while child.poll() is None:
                try:
                    output_size = output_path.stat().st_size
                except OSError:
                    output_size = last_output_size
                if output_size > last_output_size:
                    last_output_size = output_size
                    last_activity = time.monotonic()
                if gateway and gateway.stopped.is_set():
                    stop_reason = gateway.reason
                elif checkpoint.is_file():
                    snapshot = load(checkpoint)
                    lane = snapshot.get("tickets", {}).get(args.ticket, {})
                    if lane_invocation_matches(lane, args.invocation_id):
                        progress_count = sum(
                            1
                            for item in lane.get("progress", [])
                            if item.get("verified")
                        )
                        if progress_count > last_progress_count:
                            last_progress_count = progress_count
                            last_activity = time.monotonic()
                        spend = usage_snapshots(_cfg).get(args.ticket, {})
                        if (
                            progress_spending(lane, _cfg, spend.get("spent_usd", 0))
                            >= _cfg["max_usd_without_progress"]
                        ):
                            stop_reason = "max_usd_without_progress"
                if time.monotonic() - started >= max_lifetime_seconds:
                    stop_reason = lifetime_reason
                elif time.monotonic() - last_activity >= max_idle_seconds:
                    stop_reason = idle_reason
                if stop_reason:
                    signal_process_group(child, signal.SIGTERM)
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    # Descendants can outlive their parent or ignore TERM.
                    signal_process_group(child, signal.SIGKILL)
                    break
                time.sleep(0.1)
            returncode = child.wait()
    except (
        OSError,
        subprocess.SubprocessError,
        AgentError,
        ContextError,
        HealthError,
        SprintError,
        AuthorityError,
    ) as exc:
        supervisor_error = str(exc)
    finally:
        # A supervisor exception must never leave an unmonitored paid worker.
        # Also collect children that outlived a normally exiting parent.
        if child is not None:
            signal_process_group(child, signal.SIGTERM)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            signal_process_group(child, signal.SIGKILL)
            try:
                returncode = child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if gateway:
            try:
                gateway.close()
                gateway_closed = True
            except (
                OSError,
                subprocess.SubprocessError,
                AgentError,
                AuthorityError,
            ) as exc:
                if not supervisor_error:
                    supervisor_error = f"gateway cleanup failed: {exc}"
        if args.stdin_file and input_handle is not subprocess.DEVNULL:
            input_handle.close()
    # A native client can exit before the polling loop observes its gateway.
    # Read the final reason only after all handlers have settled in close().
    if not stop_reason and gateway and gateway.stopped.is_set():
        stop_reason = gateway.reason
    terminal = {
        "invocation_id": args.invocation_id,
        "phase": "terminal",
        "spawned": child is not None,
        "returncode": returncode,
        "stop_reason": stop_reason,
        "startup_retryable": bool(gateway and gateway.startup_retryable()),
        "finished_at": now(),
        "cooperative_cleanup": {
            "worker_pgid": child.pid if child else 0,
            "gateway_closed": gateway_closed if gateway else True,
        },
    }
    if supervisor_error:
        terminal["error"] = supervisor_error
    write_json(tombstone_path, terminal)
    write_json(
        ready_path,
        {**terminal, "identity": identity, "worker_pid": child.pid if child else 0},
    )
    if (
        stop_reason or (supervisor_error and child is not None)
    ) and checkpoint.is_file():
        with locked(checkpoint):
            state = load(checkpoint)
            lane = state.get("tickets", {}).get(args.ticket, {})
            if (
                lane_invocation_matches(lane, args.invocation_id)
                and lane.get("state") == "running"
            ):
                terminal["attempt"] = int(lane.get("attempts") or 0)
                # Shared admission pressure can disappear when another lane's
                # reservation is released. Keep it in automatic recovery; the
                # ledger rechecks capacity before any subsequent paid request.
                shared_pressure = stop_reason.startswith(
                    "max_usd_per_sprint would be exceeded:"
                )
                # A client request the gateway cannot meter stops only this
                # lane. Its route-scoped incident gates relaunch admission.
                client_incompatible = stop_reason.startswith("client_incompatible:")
                lane["state"] = (
                    "operator_decision"
                    if not shared_pressure
                    and not client_incompatible
                    and ("budget" in stop_reason or "usd" in stop_reason)
                    else "recoverable"
                )
                lane["reason"] = stop_reason or supervisor_error
                lane["last_terminal"] = terminal
                lane.setdefault("history", []).append(
                    {
                        "at": now(),
                        "event": "supervisor-stopped",
                        "state": lane["state"],
                        "reason": stop_reason or supervisor_error,
                    }
                )
                save(checkpoint, state)


def execution_unit_status(identity: dict[str, Any]) -> str:
    """Return live, absent, or unknown without collapsing inspection failure."""
    tombstone_path = Path(str(identity.get("tombstone_path") or ""))
    tombstone = None
    if tombstone_path.is_file():
        tombstone = read_json(tombstone_path, label="execution tombstone")
        if tombstone.get("invocation_id") != identity.get("invocation_id"):
            return "unknown"
    containment = identity.get("containment")
    if containment == "cgroup-v2-systemd-scope":
        raw_cgroup = str(identity.get("cgroup") or "")
        if not raw_cgroup.startswith("/") or ".." in Path(raw_cgroup).parts:
            return "unknown"
        cgroup = Path("/sys/fs/cgroup") / raw_cgroup.lstrip("/")
        try:
            if not cgroup.exists():
                return "absent" if tombstone else "unknown"
            populated = (cgroup / "cgroup.events").read_text(encoding="utf-8")
        except (OSError, PermissionError):
            return "unknown"
        if re.search(r"^populated\s+1$", populated, re.MULTILINE):
            return "live"
        return "absent" if tombstone else "unknown"
    try:
        current = process_identity(str(identity.get("pid")))
    except ProcessAbsent:
        return "absent" if tombstone else "unknown"
    except SprintError:
        return "unknown"
    if current.get("start_identity") != identity.get("start_identity"):
        return "absent" if tombstone else "unknown"
    return "live"


STOPPED_INVOCATION_IDENTITY_FIELDS = (
    "kind",
    "containment",
    "unit_name",
    "tombstone_path",
    "pid",
    "start_identity",
    "cgroup",
)


def stopped_invocation_evidence(
    shared_root: Path,
    *,
    ticket: str,
    sprint: str,
    invocation_id: str,
    reserved_at: str,
) -> dict[str, Any]:
    """Prove a controller-launched invocation is terminal and no longer running.

    Native gateways run inside the supervisor and write ledger reservations under
    the invocation id without an API run marker. Only the controller checkpoint,
    its terminal record, and a live identity check can show that no gateway
    remains to settle such a reservation.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", invocation_id):
        raise SprintError("reservation run id is not a controller invocation id")
    if not sprint:
        raise SprintError("gateway reservation has no sprint binding")
    key = normalize_key(ticket)
    shared_root = Path(shared_root).resolve()
    try:
        config = canonical_config_path(shared_root)
    except RuntimeStateError as exc:
        raise SprintError(str(exc)) from exc
    configured = Path(
        config_scalar(config, "sprint_checkpoint_dir", ".orchestration/.sprint-state")
    )
    if configured.is_absolute():
        raise SprintError("sprint checkpoint directory must be repository-relative")
    state_dir = (shared_root / configured).resolve()
    if state_dir != shared_root and shared_root not in state_dir.parents:
        raise SprintError("sprint checkpoint directory escapes the repository")
    checkpoint = state_path(state_dir, str(sprint))
    lane = load(checkpoint).get("tickets", {}).get(key)
    if not isinstance(lane, dict):
        raise SprintError(f"no controller launch record binds {invocation_id} to {key}")
    evidence = lane.get("launch_evidence")
    candidates = [
        evidence.get("identity") if isinstance(evidence, dict) else None,
        lane.get("worker_identity"),
        *(item.get("worker_identity") for item in lane.get("history", [])),
    ]
    identities = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("invocation_id") == invocation_id
    ]
    if not identities:
        raise SprintError(
            f"no controller launch record binds invocation {invocation_id} to {key}"
        )
    identity = identities[0]
    if any(
        other.get(field) != identity.get(field)
        for other in identities[1:]
        for field in STOPPED_INVOCATION_IDENTITY_FIELDS
    ):
        raise SprintError(
            f"controller launch records for invocation {invocation_id} are ambiguous"
        )
    tombstone_path = state_dir / f"execution-{invocation_id}.terminal.json"
    if (
        identity.get("kind") != "execution_unit"
        or Path(str(identity.get("tombstone_path") or "")).resolve()
        != tombstone_path.resolve()
    ):
        raise SprintError(
            f"controller identity for invocation {invocation_id} is not a supervised execution unit"
        )
    try:
        raw = tombstone_path.read_bytes()
        tombstone = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SprintError(
            f"controller execution terminal record for {invocation_id} is missing or unreadable"
        ) from exc
    if (
        not isinstance(tombstone, dict)
        or tombstone.get("phase") != "terminal"
        or tombstone.get("invocation_id") != invocation_id
        or tombstone.get("spawned") is not True
    ):
        raise SprintError(
            f"controller execution terminal record for {invocation_id} does not show an exited unit"
        )
    try:
        finished = datetime.fromisoformat(str(tombstone.get("finished_at")))
        reserved = datetime.fromisoformat(str(reserved_at))
    except ValueError as exc:
        raise SprintError(
            f"terminal record or reservation for {invocation_id} lacks a valid timestamp"
        ) from exc
    if finished.tzinfo is None or reserved.tzinfo is None or finished < reserved:
        raise SprintError(
            f"execution unit {invocation_id} finished before the reservation was recorded"
        )
    containment = identity.get("containment")
    if containment not in {
        "cgroup-v2-systemd-scope",
        "cooperative-session",
        "test-supervisor",
    }:
        raise SprintError(
            f"execution unit {invocation_id} has unsupported containment {containment!r}"
        )
    status = execution_unit_status(identity)
    if status != "absent":
        raise SprintError(
            f"execution unit for invocation {invocation_id} is {status}; "
            "its gateway may still settle the reservation"
        )
    if containment == "cooperative-session":
        cleanup = tombstone.get("cooperative_cleanup") or {}
        pgid = cleanup.get("worker_pgid")
        if (
            cleanup.get("gateway_closed") is not True
            or not isinstance(pgid, int)
            or isinstance(pgid, bool)
            or pgid <= 1
        ):
            raise SprintError(
                f"cooperative execution unit {invocation_id} lacks a gateway cleanup receipt"
            )
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise SprintError(
                f"worker process group for {invocation_id} cannot be verified: {exc}"
            ) from exc
        else:
            raise SprintError(
                f"worker process group for invocation {invocation_id} is still alive"
            )
    return {
        "checkpoint": str(checkpoint),
        "ticket": key,
        "sprint": str(sprint),
        "containment": containment,
        "unit_status": status,
        "tombstone": str(tombstone_path),
        "tombstone_sha256": hashlib.sha256(raw).hexdigest(),
        "stop_reason": str(tombstone.get("stop_reason") or ""),
        "returncode": tombstone.get("returncode"),
        "finished_at": str(tombstone.get("finished_at")),
    }


def launch_local(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Launch through a controller-owned execution unit and durable tombstone."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SprintError("launch-local requires an executable and arguments after --")
    output_path = _repository_path(
        cfg["shared_root"], args.output, label="worker output"
    )
    input_path = (
        _repository_path(cfg["shared_root"], args.stdin_file, label="worker input")
        if args.stdin_file
        else None
    )
    if input_path is not None and not input_path.is_file():
        raise SprintError("worker input must be an existing repository file")
    route = None
    worker_cwd = Path(cfg["shared_root"]).resolve()
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] != "running":
            raise SprintError(f"ticket {key} is not running")
        recovery_binding = verify_recovery_binding(cfg, ticket)
        if isinstance(recovery_binding, dict) and recovery_binding.get("kind") == "preserved_pr":
            worker_cwd = Path(str(recovery_binding["worktree"])).resolve()
        expected = str(ticket.get("attach_capability") or "")
        if (
            not expected
            or args.attach_capability != expected
            or ticket.get("attached_at")
            or ticket.get("launch_evidence")
        ):
            raise SprintError(
                "attach capability is missing, stale, or already used for a launch"
            )
        if cfg.get("runtime_admission"):
            route = llm_route_from_config(cfg["config"], "sprint-worker")
            if ticket.get("reserved_route") != route:
                raise SprintError(
                    "reservation route is missing or changed; reconcile before launch"
                )
            if hold := runtime_admission(cfg):
                raise SprintError(
                    "provider admission held: " + json.dumps(hold, sort_keys=True)
                )
            command = validate_native_command(command, route)
            command = bind_native_working_directory(command, route, worker_cwd)
        invocation_id = uuid.uuid4().hex
        runtime_prefix = cfg["state_dir"] / f"execution-{invocation_id}"
        ready_path = runtime_prefix.with_suffix(".ready.json")
        ack_path = runtime_prefix.with_suffix(".ack")
        tombstone_path = runtime_prefix.with_suffix(".terminal.json")
        claim_path = runtime_prefix.with_suffix(".claim.json")
        supervisor_capability = "supervisor_" + uuid.uuid4().hex
        evidence = {
            "token": "launch_" + uuid.uuid4().hex,
            "status": "launching",
            "repository": str(cfg["shared_root"]),
            "worker_cwd": str(worker_cwd),
            "recovery_binding": recovery_binding,
            "sprint": str(args.sprint),
            "ticket": key,
            "attempt": ticket["attempts"],
            "attempt_token": ticket["attempt_token"],
            "invocation_id": invocation_id,
            "ready_path": str(ready_path),
            "ack_path": str(ack_path),
            "tombstone_path": str(tombstone_path),
            "output_path": str(output_path),
            "input_path": str(input_path) if input_path is not None else "",
            "command_sha256": hashlib.sha256(
                json.dumps(command, separators=(",", ":")).encode()
            ).hexdigest(),
            "subscription_route": bool(route and model_less_desktop_route(route)),
            "supervisor_capability_sha256": hashlib.sha256(
                supervisor_capability.encode()
            ).hexdigest(),
            "supervisor_claim_path": str(claim_path),
            "created_at": now(),
            "base_commit": subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=worker_cwd,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        }
        # Persist the launch intent first. A controller crash can then fence the
        # lane for reconciliation instead of allowing a duplicate launch.
        ticket["launch_evidence"] = evidence
        save(path, state)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        supervisor = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--config",
            str(cfg["config"]),
            "supervise-local",
            "--ticket",
            key,
            "--sprint",
            str(args.sprint),
            "--invocation-id",
            invocation_id,
            "--ready",
            str(ready_path),
            "--ack",
            str(ack_path),
            "--tombstone",
            str(tombstone_path),
            "--output",
            str(output_path),
            "--worker-cwd",
            str(worker_cwd),
            "--supervisor-capability",
            supervisor_capability,
        ]
        if input_path is not None:
            supervisor.extend(["--stdin-file", str(input_path)])
        if route and model_less_desktop_route(route):
            supervisor.append("--subscription-route")
        supervisor.extend(["--", *command])
        containment = "cooperative-session"
        unit_name = ""
        launch_command = supervisor
        if linux_systemd_scope_available():
            unit_name = f"orchestration-{invocation_id}.scope"
            containment = "cgroup-v2-systemd-scope"
            launch_command = [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                f"--unit={unit_name}",
                *supervisor,
            ]
        elif os.environ.get("ORCHESTRATION_TEST_MODE") == "1":
            containment = "test-supervisor"
        evidence["cooperative_auto_recovery"] = cfg.get(
            "cooperative_auto_recovery", False
        )
        evidence["containment"] = containment
        evidence["unit_name"] = unit_name
        ticket["launch_evidence"] = evidence
        save(path, state)
        try:
            worker = subprocess.Popen(
                launch_command,
                cwd=cfg["shared_root"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            evidence.update({"status": "launch-failed", "error": str(exc)})
            ticket["launch_evidence"] = evidence
            save(path, state)
            raise SprintError(
                f"controller could not launch local worker: {exc}"
            ) from exc
        ready = wait_for_runtime_record(ready_path, worker, "supervisor readiness")
        identity = ready.get("identity")
        if not isinstance(identity, dict):
            worker.terminate()
            raise SprintError("controller supervisor omitted its execution identity")
        identity.update(
            {
                "kind": "execution_unit",
                "invocation_id": invocation_id,
                "containment": containment,
                "unit_name": unit_name,
                "tombstone_path": str(tombstone_path),
            }
        )
        if containment == "cgroup-v2-systemd-scope" and unit_name not in str(
            identity.get("cgroup") or ""
        ):
            worker.terminate()
            raise SprintError("systemd launch did not enter its assigned cgroup scope")
        ack_path.touch(exist_ok=False)
        launched = wait_for_runtime_record(ready_path, worker, "worker launch")
        spawn_proved = launched.get("phase") == "launched" or (
            launched.get("phase") == "terminal" and launched.get("spawned") is True
        )
        if not spawn_proved or not launched.get("worker_pid"):
            worker.terminate()
            raise SprintError("supervisor did not prove a spawned worker")
        evidence.update({"status": "launched", "identity": identity})
        ticket["launch_evidence"] = evidence
        # The binding remains active through the supervisor's final pre-spawn
        # validation. Only a proved child launch consumes it.
        recovery_id = str((ticket.get("recovery_binding") or {}).get("recovery_id") or "")
        if recovery_id:
            try:
                UsageLedger(cfg["shared_root"]).release_recovery_fence(
                    key, recovery_id
                )
            except Exception as exc:
                worker.terminate()
                evidence.update({"status": "launch-failed", "error": str(exc)})
                ticket["launch_evidence"] = evidence
                save(path, state)
                raise SprintError(
                    "controller could not release the preserved-PR recovery fence; "
                    "the worker remains unacknowledged: " + str(exc)
                ) from exc
        ticket["recovery_binding"] = {}
        ticket["history"].append(
            {"at": now(), "event": "worker-launched", "worker_identity": identity}
        )
        save(path, state)
        worker_pid = launched["worker_pid"]
    emit(
        {
            "ticket": key,
            "state": "running",
            "launch_evidence": evidence["token"],
            "worker_pid": worker_pid,
            "run_ref": ticket["run_ref"],
        }
    )


def attach(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] != "running":
            raise SprintError(f"ticket {key} is not running")
        evidence = ticket.get("launch_evidence") or {}
        expected = {
            "repository": str(cfg["shared_root"]),
            "sprint": str(args.sprint),
            "ticket": key,
            "attempt": ticket["attempts"],
            "attempt_token": ticket["attempt_token"],
        }
        if (
            not evidence
            or args.launch_evidence != evidence.get("token")
            or evidence.get("status") not in {"launching", "launched"}
            or any(evidence.get(name) != value for name, value in expected.items())
            or ticket.get("attached_at")
        ):
            raise SprintError(
                "controller launch evidence is missing, stale, or belongs to another attempt"
            )
        identity = evidence.get("identity")
        if not isinstance(identity, dict) and evidence.get("ready_path"):
            ready = read_json(
                Path(str(evidence["ready_path"])), label="supervisor readiness"
            )
            identity = ready.get("identity")
        if not isinstance(identity, dict):
            raise SprintError(
                "controller launch evidence has no execution-unit identity"
            )
        identity.update(
            {
                "kind": "execution_unit",
                "invocation_id": evidence.get("invocation_id", ""),
                "containment": evidence.get("containment", "cooperative-session"),
                "unit_name": evidence.get("unit_name", ""),
                "tombstone_path": evidence.get("tombstone_path", ""),
            }
        )
        unit_status = execution_unit_status(identity)
        if unit_status == "unknown":
            raise SprintError("controller cannot verify the launched execution unit")
        ticket["worker_identity"] = identity
        ticket["attached_at"] = now()
        ticket["attach_capability"] = ""
        # Consume only the bearer token.  The attempt-scoped launch record is
        # still required after attach to bind cooperative terminal cleanup,
        # recovery policy, and the implementation baseline to this invocation.
        # `attached_at` makes the transition one-use; requeue/reserve clear the
        # complete record before a later attempt begins.
        evidence["token"] = ""
        ticket["launch_evidence"] = evidence
        ticket["history"].append(
            {"at": now(), "event": "attached", "worker_identity": identity}
        )
        save(path, state)
    emit({"ticket": key, "state": "running", "worker_identity": identity})


def finish(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.summary.strip():
        raise SprintError("finish summary must not be empty")
    if args.outcome == "completed" and (not args.pr.strip() or not args.branch.strip()):
        raise SprintError("completed outcome requires both PR and branch identity")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] != "running":
            raise SprintError(f"ticket {key} is not running")
        require_attempt(ticket, args.attempt_token)
        ticket["state"] = args.outcome
        ticket["reason"] = args.summary.strip()
        ticket["branch"] = args.branch.strip()
        ticket["pr"] = args.pr.strip()
        ticket["history"].append(
            {"at": now(), "event": "finished", "outcome": args.outcome}
        )
        save(path, state)
    emit({"ticket": key, "state": args.outcome})


def requeue(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.reason.strip():
        raise SprintError("requeue reason must not be empty")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] == "completed" or ticket["state"] == "pending":
            current = ticket["state"] if ticket else "missing"
            raise SprintError(f"ticket {key} cannot be requeued from state {current}")
        require_worker_stopped(ticket, args.operator_capability, cfg)
        require_attempt(ticket, args.attempt_token)
        ticket["state"] = "pending"
        ticket["reason"] = args.reason.strip()
        ticket["run_ref"] = ""
        receipt = current_startup_failure(ticket, cfg)
        if receipt and receipt not in ticket.get("startup_retry_receipts", []):
            ticket.setdefault("startup_retry_receipts", []).append(receipt)
        last_attempt = int(ticket.get("attempts") or 0)
        made_progress = any(
            item.get("verified") and int(item.get("attempt") or 0) == last_attempt
            for item in ticket.get("progress", [])
        )
        terminal = ticket.get("last_terminal") or {}
        identity = ticket.get("worker_identity") or {}
        terminal_matches_attempt = bool(
            int(terminal.get("attempt") or 0) == last_attempt
            and terminal.get("invocation_id")
            and terminal.get("invocation_id") == identity.get("invocation_id")
        )
        stop_reason = (
            str(terminal.get("stop_reason") or "") if terminal_matches_attempt else ""
        )
        ticket["next_launch_continuation"] = bool(
            made_progress
            and stop_reason
            in {
                "max_worker_idle_seconds",
                "max_worker_lifetime_seconds",
                "max_worker_seconds",
            }
        )
        # Preserve the resumable work identity across execution attempts.
        ticket["attempt_token"] = ""
        ticket["attempt_capability"] = {}
        ticket["worker_identity"] = ""
        ticket["attach_capability"] = ""
        ticket["attached_at"] = ""
        ticket["launch_evidence"] = {}
        ticket["history"].append(
            {
                "at": now(),
                "event": "requeued",
                "reason": args.reason.strip(),
                "continuation_eligible": ticket["next_launch_continuation"],
            }
        )
        save(path, state)
    emit({"ticket": key, "state": "pending"})


def outstanding_reservation_details(cfg: dict[str, Any], key: str) -> str:
    """Name each open reservation blocking a ticket so the operator can look it up."""
    blocking = sorted(
        (
            item
            for item in UsageLedger(cfg["shared_root"]).summary()["open_reservations"]
            if str(item.get("ticket") or "").strip().upper() == key
        ),
        key=lambda item: (str(item.get("timestamp") or ""), str(item.get("reservation_id") or "")),
    )
    listed = "; ".join(
        f"{item.get('reservation_id')} (run {item.get('run_id')}, "
        f"${item.get('projected_cost_usd')}, reserved {item.get('timestamp')}, "
        f"origin {item.get('origin') or 'unmarked'})"
        for item in blocking
    )
    return (
        (listed or "see api_agent.py usage")
        + ". Check each request with the provider, then use api_agent.py "
        "reservation-migration-plan and reconcile-reservations"
    )


def restart_ticket(args, cfg):
    """Activate an operator allowance without deleting work or counters."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] in {"running", "completed", "decomposed"}:
            raise SprintError("restart requires an existing stopped, incomplete ticket")
        identity = ticket.get("worker_identity")
        if identity and not automatic_recovery_available(ticket, cfg):
            raise SprintError(
                "restart requires verified stopped execution evidence; recover legacy identity first"
            )
        if not identity and ticket.get("attempts"):
            events = [item.get("event") for item in ticket.get("history", [])]
            recoveries = [
                i
                for i, event in enumerate(events)
                if event in {"requeued", "terminal-recovered", "legacy-recovered"}
            ]
            launches = [
                i
                for i, event in enumerate(events)
                if event in {"reserved", "worker-launched"}
            ]
            if not recoveries or max(recoveries) <= max(launches, default=-1):
                raise SprintError(
                    "restart requires recovery of the previous unverified attempt"
                )
        usage = usage_snapshots(cfg).get(key, {})
        if usage.get("reserved_usd", 0):
            raise SprintError(
                "restart requires reconciliation of outstanding provider reservations: "
                + outstanding_reservation_details(cfg, key)
            )
        try:
            token = operator_capability(args)
            grant = (
                authorized_restart_grant(cfg["shared_root"], key, token)
                if token
                else authorized_restart_grant(cfg["shared_root"], key)
            )
        except AuthorityError as exc:
            raise SprintError(str(exc)) from exc
        if not grant:
            raise SprintError("restart requires an active root-issued allowance")
        if ticket.get("restart_grant_id") == grant["grant_id"]:
            emit({"ticket": key, "already_applied": True, "state": ticket["state"]})
            return
        if float(grant["allowances"]["progress_baseline_usd"]) > float(
            usage.get("spent_usd", 0)
        ):
            raise SprintError("restart baseline exceeds recorded spending")
        old_state, old_reason = ticket["state"], ticket.get("reason", "")
        jira_state, jira_reason = initial_state(ticket.get("raw_status", ""), cfg)
        if jira_state in {"completed", "blocked"}:
            ticket["state"], ticket["reason"] = jira_state, jira_reason
        elif ticket.get("pr") and ticket.get("attempt_token"):
            ticket["state"] = "needs_repair"
            ticket["reason"] = "operator restart resumes the preserved pull request"
        elif ticket.get("scope_assessment", {}).get("verdict") == "decompose":
            ticket["state"] = "needs_decomposition"
            ticket["reason"] = "operator restart resumes approved decomposition"
        else:
            # Jira workflow states such as In Progress are orchestration-owned
            # while a recovered attempt is being resumed. A root-issued restart
            # must not turn them into an unrelated readiness decision.
            ticket["state"] = "pending"
            ticket["reason"] = "operator restart resumes preserved ticket work"
        legacy_classification = next(
            (
                item
                for item in reversed(ticket.get("history", []))
                if item.get("event") == "legacy-classified"
            ),
            {},
        )
        if (
            old_state == "operator_decision"
            and legacy_classification.get("state") == "operator_decision"
        ):
            ticket["state"], ticket["reason"] = old_state, old_reason
        elif ticket.get("scope_assessment", {}).get("verdict") == "operator_decision":
            ticket["state"] = "operator_decision"
            ticket["reason"] = (
                "restart allowance does not resolve the preserved product/scoping decision"
            )
        ticket["restart_grant_id"] = grant["grant_id"]
        ticket.setdefault("history", []).append(
            {
                "at": now(),
                "event": "operator-restart",
                "grant_id": grant["grant_id"],
                "previous_state": old_state,
                "previous_reason": old_reason,
                "reason": grant["reason"],
                "allowances": grant["allowances"],
            }
        )
        # Execution fences, prior work, scope decisions and findings remain intact.
        save(path, state)
    emit({"ticket": key, "state": ticket["state"], "grant_id": grant["grant_id"]})


def grant_budget(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Activate a root-issued absolute ticket ceiling and record it in state."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] == "completed":
            current = ticket["state"] if ticket else "missing"
            raise SprintError(
                f"ticket {key} cannot receive a budget grant from state {current}"
            )
        try:
            ceiling = activate_budget(
                cfg["shared_root"], key, operator_capability(args)
            )
        except AuthorityError as exc:
            raise SprintError(str(exc)) from exc
        ticket["history"].append(
            {
                "at": now(),
                "event": "operator-budget-granted",
                "ceiling_usd": str(ceiling),
            }
        )
        save(path, state)
    emit({"ticket": key, "budget_ceiling_usd": str(ceiling), "state": ticket["state"]})


def grant_relaunch(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Activate a root-issued absolute total-attempt ceiling for one ticket."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if not ticket or ticket["state"] == "completed":
            current = ticket["state"] if ticket else "missing"
            raise SprintError(
                f"ticket {key} cannot receive a relaunch grant from state {current}"
            )
        try:
            ceiling = activate_relaunch(
                cfg["shared_root"], key, operator_capability(args)
            )
        except AuthorityError as exc:
            raise SprintError(str(exc)) from exc
        ticket["history"].append(
            {
                "at": now(),
                "event": "operator-relaunch-granted",
                "ceiling_attempts": ceiling,
            }
        )
        save(path, state)
    emit({"ticket": key, "attempt_ceiling": ceiling, "state": ticket["state"]})


def recover_terminal(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Requeue a terminal lane using a separately issued recovery capability."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.reason.strip():
        raise SprintError("terminal recovery reason must not be empty")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        current = ticket.get("state") if ticket else "missing"
        tokenless_preserved_repair = bool(
            ticket
            and current == "needs_repair"
            and not ticket.get("attempt_token")
        )
        if not ticket or (
            current not in {
                "blocked",
                "external_blocked",
                "operator_decision",
                "user_action",
            }
            and not tokenless_preserved_repair
        ):
            raise SprintError(
                f"ticket {key} cannot be terminal-recovered from state {current}"
            )
        if tokenless_preserved_repair:
            if not ticket.get("pr") or not ticket.get("branch"):
                raise SprintError(
                    "tokenless needs_repair recovery requires a preserved PR and branch"
                )
            stale_execution_fields = [
                name
                for name in (
                    "run_ref",
                    "worker_identity",
                    "launch_evidence",
                    "attach_capability",
                    "attached_at",
                    "attempt_capability",
                    "recovery_binding",
                )
                if ticket.get(name)
            ]
            if stale_execution_fields:
                raise SprintError(
                    "tokenless needs_repair recovery requires an empty execution unit; "
                    "stale fields: " + ", ".join(stale_execution_fields)
                )
        try:
            consume_recovery(
                cfg["shared_root"],
                key,
                int(ticket.get("attempts") or 0),
                operator_capability(args),
            )
        except AuthorityError as exc:
            raise SprintError(str(exc)) from exc
        ticket["state"] = "pending"
        ticket["reason"] = args.reason.strip()
        ticket["run_ref"] = ""
        # The terminal execution identity is gone, but its durable work is not.
        # Preserve branch/PR bindings while leaving the ticket pending; reserve
        # will mint the only valid token for the next repair attempt.
        ticket["attempt_token"] = ""
        ticket["attempt_capability"] = {}
        ticket["worker_identity"] = ""
        ticket["attach_capability"] = ""
        ticket["attached_at"] = ""
        ticket["launch_evidence"] = {}
        ticket["next_launch_continuation"] = False
        ticket["legacy_recovery_pending"] = False
        ticket["history"].append(
            {
                "at": now(),
                "event": "terminal-recovered",
                "previous_state": current,
                "reason": args.reason.strip(),
            }
        )
        save(path, state)
    emit({"ticket": key, "state": "pending", "recovered": True})


def operator_capability(args: argparse.Namespace) -> str:
    if getattr(args, "operator_capability_stdin", False):
        value = sys.stdin.readline().strip()
        if not value:
            raise SprintError("operator capability stdin was empty")
        return value
    return str(getattr(args, "operator_capability", ""))


def recover_legacy(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    """Requeue a fenced schema-v1 lane after external process verification."""
    path = state_path(cfg["state_dir"], str(args.sprint))
    key = normalize_key(args.ticket)
    if not args.reason.strip():
        raise SprintError("legacy recovery reason must not be empty")
    with locked(path):
        state = load(path)
        ticket = state["tickets"].get(key)
        if (
            not ticket
            or ticket.get("state") != "user_action"
            or not ticket.get("legacy_recovery_pending")
        ):
            raise SprintError(f"ticket {key} is not a fenced legacy running lane")
        require_worker_stopped(ticket, args.operator_capability, cfg)
        ticket["state"] = "pending"
        ticket["reason"] = args.reason.strip()
        ticket["run_ref"] = ""
        ticket["attempt_token"] = ""
        ticket["attempt_capability"] = {}
        ticket["legacy_recovery_pending"] = False
        ticket["history"].append(
            {"at": now(), "event": "legacy-recovered", "reason": args.reason.strip()}
        )
        save(path, state)
    emit({"ticket": key, "state": "pending", "recovered": True})


def automatic_recovery_available(ticket, cfg, status=None):
    """Honor strict containment or an explicitly configured cooperative contract."""
    identity = ticket.get("worker_identity")
    if not isinstance(identity, dict) or identity.get("kind") != "execution_unit":
        return False
    if (status or execution_unit_status(identity)) != "absent":
        return False
    if identity.get("containment") in {"cgroup-v2-systemd-scope", "test-supervisor"}:
        return True
    launch = ticket.get("launch_evidence") or {}
    if (
        identity.get("containment") != "cooperative-session"
        or not cfg.get("cooperative_auto_recovery")
        or not launch.get("cooperative_auto_recovery")
        or launch.get("invocation_id") != identity.get("invocation_id")
    ):
        return False
    try:
        receipt = read_json(
            Path(identity["tombstone_path"]), label="cooperative cleanup receipt"
        )
        cleanup = receipt.get("cooperative_cleanup", {})
        pgid = cleanup.get("worker_pgid")
        if (
            receipt.get("phase") != "terminal"
            or (
                receipt.get("error")
                and (
                    not isinstance(receipt.get("returncode"), int)
                    or isinstance(receipt.get("returncode"), bool)
                )
            )
            or receipt.get("invocation_id") != identity["invocation_id"]
            or cleanup.get("gateway_closed") is not True
            or not isinstance(pgid, int)
            or isinstance(pgid, bool)
            or pgid <= 1
        ):
            return False
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except (KeyError, OSError, SprintError):
        return False
    return False


def require_worker_stopped(
    ticket: dict[str, Any], operator_token: str, cfg: dict[str, Any]
) -> None:
    """Prove containment or the configured cooperative cleanup contract."""
    if automatic_recovery_available(ticket, cfg):
        return
    # Missing cleanup receipts and legacy identities require host authority.
    if not consume_operator_recovery(operator_token, ticket, cfg):
        raise SprintError(
            "worker-unit absence is not mechanically verifiable; external operator recovery authority is unavailable or denied"
        )


def consume_operator_recovery(
    operator_token: str, ticket: dict[str, Any], cfg: dict[str, Any]
) -> bool:
    try:
        consume_recovery(
            cfg["shared_root"],
            str(ticket.get("key") or ""),
            int(ticket.get("attempts") or 0),
            operator_token,
        )
    except AuthorityError:
        return False
    return True


def process_identity(raw_pid: str) -> dict[str, Any]:
    """Read a stable kernel process-start identity, distinguishing unknown from gone."""
    try:
        pid = int(str(raw_pid))
    except ValueError as exc:
        raise SprintError("worker PID must be a positive integer") from exc
    if pid < 1:
        raise SprintError("worker PID must be a positive integer")
    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:
        raise ProcessAbsent(f"worker PID {pid} does not exist") from exc
    except PermissionError as exc:
        raise SprintError(
            f"permission denied while inspecting worker PID {pid}"
        ) from exc
    except OSError as exc:
        raise SprintError(f"cannot inspect worker PID {pid}: {exc}") from exc

    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            tail = raw[raw.rindex(")") + 2 :].split()
            started = tail[19]
        except FileNotFoundError as exc:
            raise ProcessAbsent(f"worker PID {pid} exited during inspection") from exc
        except (PermissionError, OSError, ValueError, IndexError) as exc:
            raise SprintError(
                f"cannot verify Linux start identity for PID {pid}"
            ) from exc
        try:
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="utf-8")
                .strip()
            )
        except (PermissionError, OSError) as exc:
            raise SprintError("cannot verify the Linux boot identity") from exc
        try:
            cgroup_lines = (
                Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
            )
            cgroup = next(
                line.split(":", 2)[2] for line in cgroup_lines if line.startswith("0::")
            )
        except (OSError, StopIteration, IndexError) as exc:
            raise SprintError(
                f"cannot verify Linux cgroup identity for PID {pid}"
            ) from exc
        marker = f"linux:{boot_id}:{started}"
    elif sys.platform == "darwin":

        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("flags", ctypes.c_uint32),
                ("status", ctypes.c_uint32),
                ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32),
                ("ppid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32),
                ("ruid", ctypes.c_uint32),
                ("rgid", ctypes.c_uint32),
                ("svuid", ctypes.c_uint32),
                ("svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32),
                ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32),
                ("pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("nice", ctypes.c_int32),
                ("start_tvsec", ctypes.c_uint64),
                ("start_tvusec", ctypes.c_uint64),
            ]

        info = ProcBsdInfo()
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            size = libproc.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
            )
        except (OSError, AttributeError) as exc:
            raise SprintError(
                f"cannot inspect macOS worker PID {pid} with proc_pidinfo"
            ) from exc
        if size != ctypes.sizeof(info) or info.pid != pid or not info.start_tvsec:
            try:
                os.kill(pid, 0)
            except ProcessLookupError as exc:
                raise ProcessAbsent(
                    f"worker PID {pid} exited during inspection"
                ) from exc
            except (PermissionError, OSError) as exc:
                raise SprintError(f"cannot verify macOS worker PID {pid}") from exc
            raise SprintError(
                f"proc_pidinfo did not return an exact birth identity for live PID {pid}"
            )
        marker = f"darwin:{info.start_tvsec}:{info.start_tvusec}"
        cgroup = ""
    else:
        raise SprintError(
            f"process start identity is unsupported on platform {sys.platform}"
        )
    fingerprint = hashlib.sha256(f"{pid}:{marker}".encode("utf-8")).hexdigest()
    return {
        "kind": "process",
        "pid": pid,
        "start_fingerprint": fingerprint,
        "start_identity": marker,
        "boot_id": boot_id if sys.platform.startswith("linux") else "",
        "cgroup": cgroup,
    }


def summary_value(state: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    spend = usage_snapshots(cfg)
    cycles = find_cycles(state["tickets"])
    result: dict[str, Any] = {
        "sprint": state["sprint"],
        "completed": [],
        "blocked": [],
        "decomposed": [],
        "decomposition": [],
        "external_blocked": [],
        "operator_decision": [],
        "repair": [],
        "recovery": [],
        "user_action": [],
        "running": [],
    }
    for key, ticket in sorted(state["tickets"].items()):
        item = {
            "key": key,
            "priority": ticket.get("priority"),
            "summary": ticket["summary"],
            "reason": ticket["reason"],
            "pr": ticket["pr"],
            "branch": ticket["branch"],
            "run_ref": ticket["run_ref"],
            "attempts": ticket.get("attempts", 0),
            "spend": spend.get(
                key,
                {"spent_usd": 0.0, "reserved_usd": 0.0, "run_count": 0, "state": "ok"},
            ),
        }
        if ticket["state"] == "completed":
            result["completed"].append(item)
        elif ticket["state"] == "decomposed":
            item["children"] = ticket.get("decomposition_children", [])
            item["dependency_complete"] = dependency_complete(state, key, cfg)
            result["decomposed"].append(item)
        elif ticket["state"] == "needs_decomposition":
            result["decomposition"].append(item)
        elif ticket["state"] == "needs_repair":
            result["repair"].append(item)
        elif ticket["state"] == "recoverable":
            result["recovery"].append(item)
        elif ticket["state"] == "external_blocked":
            result["external_blocked"].append(item)
        elif ticket["state"] == "operator_decision":
            result["operator_decision"].append(item)
        elif ticket["state"] == "user_action":
            result["user_action"].append(item)
        elif ticket["state"] == "blocked":
            result["blocked"].append(item)
        elif ticket["state"] == "running":
            result["running"].append(item)
        else:
            reasons = blockers(state, key, cfg, cycles)
            if reasons:
                item["reason"] = "; ".join(reasons)
                result["blocked"].append(item)
            else:
                limit_reason = attempt_limit_reason(ticket, cfg)
                if limit_reason:
                    item["reason"] = limit_reason
                elif item["spend"].get("state") == "operator_action":
                    item["reason"] = (
                        "ticket spend pause requires durable human approval"
                    )
                else:
                    item["reason"] = "ready but not launched"
                result["user_action"].append(item)
    plan = plan_value(state, cfg)
    result["finished"] = not plan[
        "autonomous_work_remaining"
    ]  # compatibility: controller drained
    result["autonomous_work_exhausted"] = result["finished"]
    result["sprint_complete"] = bool(state["tickets"]) and all(
        dependency_complete(state, key, cfg) for key in state["tickets"]
    )
    result["decision_queue"] = plan["decision_queue"]
    result["legacy_reconciliation"] = plan["legacy_reconciliation"]
    result["provider_holds"] = plan["provider_holds"]
    result["health_probes"] = plan["health_probes"]
    result["retry_waiting"] = plan["retry_waiting"]
    result["spend"] = spend
    from sprint_metrics import summarize

    result["outcome_metrics"] = summarize(state, spend)
    return result


def summary(args: argparse.Namespace, cfg: dict[str, Any]) -> None:
    path = state_path(cfg["state_dir"], str(args.sprint))
    with locked(path):
        emit(summary_value(load(path), cfg))


def report_outcomes(args, cfg):
    from sprint_metrics import summarize, verify_merges

    path = state_path(cfg["state_dir"], str(args.sprint))
    with locked(path):
        state = load(path)
    metrics = summarize(state, usage_snapshots(cfg))
    if args.verify_merges:
        from github_progress import ProgressError

        try:
            metrics = verify_merges(cfg["shared_root"], state, metrics)
        except ProgressError as exc:
            raise SprintError(str(exc)) from exc
    repeated = {}
    directory = cfg["shared_root"] / str(
        load_yaml(cfg["config"]).get(
            "review_ledger_dir", ".orchestration/.review-ledger"
        )
    )
    for ledger in directory.glob("*.json"):
        with locked(ledger):
            review = read_json(ledger, label="review outcome ledger")
        subject = review.get("work_subject") or {}
        key = subject.get("id")
        if key in state["tickets"] and subject.get("repository") == str(
            cfg["shared_root"].resolve()
        ):
            repeated.setdefault(key, []).extend(
                dict(finding=name, strikes=item.get("strikes", 0))
                for name, item in review.get("components", {}).items()
                if item.get("strikes", 0) > 1
            )
    metrics["repeated_findings"] = repeated
    metrics["review_coverage"] = (
        "Only matching canonical review ledgers are included; absent tickets have unknown coverage."
    )
    emit(metrics)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--config",
        help="repo orchestration config (default: .orchestration/config.yaml)",
    )
    result.add_argument("--state-dir", help="checkpoint directory override")
    commands = result.add_subparsers(dest="command", required=True)
    outcomes = commands.add_parser(
        "report-outcomes",
        help="report completion, blocking, and verified merge metrics",
    )
    outcomes.add_argument("--sprint", required=True)
    outcomes.add_argument("--verify-merges", action="store_true")
    outcomes.set_defaults(func=report_outcomes)
    sync_parser = commands.add_parser(
        "sync", help="normalize Jira inventory into a durable checkpoint"
    )
    inventory_source = sync_parser.add_mutually_exclusive_group(required=True)
    inventory_source.add_argument("--inventory")
    inventory_source.add_argument("--inventory-template")
    sync_parser.set_defaults(func=sync)
    for name, func in (("plan", plan), ("summary", summary)):
        command = commands.add_parser(name)
        command.add_argument("--sprint", required=True)
        command.set_defaults(func=func)
    batch_parser = commands.add_parser(
        "prepare-batch",
        help="serialize and reserve non-interactive background Message Batch jobs",
    )
    batch_parser.add_argument("--sprint", required=True)
    batch_parser.add_argument("--jobs", required=True)
    batch_parser.set_defaults(func=prepare_batch)
    submit_batch_parser = commands.add_parser("submit-batch")
    submit_batch_parser.add_argument("--batch", required=True)
    submit_batch_parser.set_defaults(func=submit_batch)
    inspect_batch_parser = commands.add_parser("inspect-batch")
    inspect_batch_parser.add_argument("--batch", required=True)
    inspect_batch_parser.set_defaults(func=inspect_batch)
    recover_batch_parser = commands.add_parser("recover-legacy-batch")
    recover_batch_parser.add_argument("--batch", required=True)
    recover_batch_parser.add_argument("--reason", required=True)
    recover_batch_parser.set_defaults(func=recover_legacy_batch)
    reconcile_batch_parser = commands.add_parser("reconcile-batch")
    reconcile_batch_parser.add_argument("--batch", required=True)
    reconcile_batch_parser.add_argument(
        "--outcome", required=True, choices=("completed", "failed")
    )
    reconcile_batch_parser.add_argument("--results")
    reconcile_batch_parser.add_argument("--provider-evidence")
    reconcile_batch_parser.add_argument("--provider-batch-id")
    reconcile_batch_parser.set_defaults(func=reconcile_batch)
    reserve_parser = commands.add_parser("reserve")
    reserve_parser.add_argument("--sprint", required=True)
    reserve_parser.add_argument("--ticket", required=True)
    reserve_parser.add_argument("--run-ref", required=True)
    reserve_parser.add_argument("--run-id")
    reserve_parser.add_argument(
        "--role", default="sprint-worker", choices=("implementer", "sprint-worker")
    )
    reserve_parser.add_argument("--worker-ref", default="")
    reserve_parser.set_defaults(func=reserve)
    attach_parser = commands.add_parser("attach")
    attach_parser.add_argument("--sprint", required=True)
    attach_parser.add_argument("--ticket", required=True)
    attach_parser.add_argument("--launch-evidence", required=True)
    attach_parser.set_defaults(func=attach)
    launch_parser = commands.add_parser("launch-local")
    launch_parser.add_argument("--sprint", required=True)
    launch_parser.add_argument("--ticket", required=True)
    launch_parser.add_argument("--attach-capability", required=True)
    launch_parser.add_argument("--output", required=True)
    launch_parser.add_argument("--stdin-file")
    launch_parser.add_argument("command", nargs=argparse.REMAINDER)
    launch_parser.set_defaults(func=launch_local)
    supervisor_parser = commands.add_parser("supervise-local", help=argparse.SUPPRESS)
    supervisor_parser.add_argument("--ticket", required=True)
    supervisor_parser.add_argument("--sprint", required=True)
    supervisor_parser.add_argument("--invocation-id", required=True)
    supervisor_parser.add_argument("--ready", required=True)
    supervisor_parser.add_argument("--ack", required=True)
    supervisor_parser.add_argument("--tombstone", required=True)
    supervisor_parser.add_argument("--output", required=True)
    supervisor_parser.add_argument("--worker-cwd", required=True)
    supervisor_parser.add_argument("--supervisor-capability", required=True)
    supervisor_parser.add_argument("--stdin-file")
    supervisor_parser.add_argument("--subscription-route", action="store_true")
    supervisor_parser.add_argument("command", nargs=argparse.REMAINDER)
    supervisor_parser.set_defaults(func=supervise_local)
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--sprint", required=True)
    finish_parser.add_argument("--ticket", required=True)
    finish_parser.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    finish_parser.add_argument("--summary", required=True)
    finish_parser.add_argument("--branch", default="")
    finish_parser.add_argument("--pr", default="")
    finish_parser.add_argument("--attempt-token", required=True)
    finish_parser.set_defaults(func=finish)
    scope_parser = commands.add_parser(
        "record-scope", help="record a structured readiness/decomposition assessment"
    )
    scope_parser.add_argument("--sprint", required=True)
    scope_parser.add_argument("--ticket", required=True)
    scope_parser.add_argument("--assessment", required=True)
    scope_parser.set_defaults(func=record_scope)
    scope_context_parser = commands.add_parser(
        "scope-context", help="emit one synchronized Jira body for ephemeral scoping"
    )
    scope_context_parser.add_argument("--sprint", required=True)
    scope_context_parser.add_argument("--ticket", required=True)
    scope_context_parser.set_defaults(func=scope_context)
    decomposition_parser = commands.add_parser(
        "record-decomposition",
        help="bind a decomposed parent to freshly synchronized Jira children",
    )
    decomposition_parser.add_argument("--sprint", required=True)
    decomposition_parser.add_argument("--ticket", required=True)
    decomposition_parser.add_argument("--children", required=True)
    decomposition_parser.set_defaults(func=record_decomposition)
    progress_parser = commands.add_parser(
        "record-progress", help="record a spend-resetting ticket milestone"
    )
    progress_parser.add_argument("--sprint", required=True)
    progress_parser.add_argument("--ticket", required=True)
    progress_parser.add_argument(
        "--milestone", required=True, choices=sorted(PROGRESS_MILESTONES)
    )
    progress_parser.add_argument(
        "--evidence",
        required=True,
        help="PR/CI: positive PR number or canonical URL; other milestones: documented artifact/receipt",
    )
    progress_parser.add_argument("--attempt-token", required=True)
    progress_parser.set_defaults(func=record_progress)
    requeue_parser = commands.add_parser("requeue")
    requeue_parser.add_argument("--sprint", required=True)
    requeue_parser.add_argument("--ticket", required=True)
    requeue_parser.add_argument("--reason", required=True)
    requeue_parser.add_argument("--attempt-token", required=True)
    requeue_parser.add_argument("--operator-capability", default="")
    requeue_parser.add_argument(
        "--worker-stopped", action="store_true", help=argparse.SUPPRESS
    )
    requeue_parser.set_defaults(func=requeue)
    legacy_parser = commands.add_parser(
        "reconcile-legacy", help="classify an opaque legacy hold without launching work"
    )
    legacy_parser.add_argument("--sprint", required=True)
    legacy_parser.add_argument("--ticket", required=True)
    legacy_parser.add_argument(
        "--classification",
        choices=("operator_decision", "external_blocked"),
        required=True,
    )
    legacy_parser.add_argument("--reason", required=True)
    legacy_parser.set_defaults(func=reconcile_legacy)
    restart_parser = commands.add_parser(
        "restart-ticket", help="apply a bounded root-issued restart allowance"
    )
    restart_parser.add_argument("--sprint", required=True)
    restart_parser.add_argument("--ticket", required=True)
    restart_parser.add_argument("--operator-capability", default="")
    restart_parser.add_argument("--operator-capability-stdin", action="store_true")
    restart_parser.set_defaults(func=restart_ticket)
    budget_parser = commands.add_parser("grant-budget")
    budget_parser.add_argument("--sprint", required=True)
    budget_parser.add_argument("--ticket", required=True)
    budget_capability = budget_parser.add_mutually_exclusive_group(required=True)
    budget_capability.add_argument("--operator-capability")
    budget_capability.add_argument("--operator-capability-stdin", action="store_true")
    budget_parser.set_defaults(func=grant_budget)
    relaunch_parser = commands.add_parser("grant-relaunch")
    relaunch_parser.add_argument("--sprint", required=True)
    relaunch_parser.add_argument("--ticket", required=True)
    relaunch_capability = relaunch_parser.add_mutually_exclusive_group(required=True)
    relaunch_capability.add_argument("--operator-capability")
    relaunch_capability.add_argument("--operator-capability-stdin", action="store_true")
    relaunch_parser.set_defaults(func=grant_relaunch)
    terminal_recovery_parser = commands.add_parser("recover-terminal")
    terminal_recovery_parser.add_argument("--sprint", required=True)
    terminal_recovery_parser.add_argument("--ticket", required=True)
    terminal_recovery_parser.add_argument("--reason", required=True)
    terminal_capability = terminal_recovery_parser.add_mutually_exclusive_group(
        required=True
    )
    terminal_capability.add_argument("--operator-capability")
    terminal_capability.add_argument("--operator-capability-stdin", action="store_true")
    terminal_recovery_parser.set_defaults(func=recover_terminal)
    recover_parser = commands.add_parser("recover-legacy")
    recover_parser.add_argument("--sprint", required=True)
    recover_parser.add_argument("--ticket", required=True)
    recover_parser.add_argument("--reason", required=True)
    recover_parser.add_argument("--operator-capability", default="")
    recover_parser.set_defaults(func=recover_legacy)
    preserved_pr_parser = commands.add_parser(
        "reconcile-preserved-pr",
        help="verify and resume one stopped preserved PR as bounded repair",
    )
    preserved_pr_parser.add_argument("--sprint", required=True)
    preserved_pr_parser.add_argument("--ticket", required=True)
    preserved_pr_capability = preserved_pr_parser.add_mutually_exclusive_group()
    preserved_pr_capability.add_argument("--operator-capability", default="")
    preserved_pr_capability.add_argument(
        "--operator-capability-stdin", action="store_true"
    )
    preserved_pr_parser.set_defaults(func=reconcile_preserved_pr)
    health_parser = commands.add_parser("health-check")
    health_parser.add_argument("--role", default="sprint-worker")
    health_parser.add_argument("--after-repair", action="store_true")
    health_parser.set_defaults(func=health_check)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        cfg = settings(args)
        args.func(args, cfg)
        return 0
    except (SprintError, AgentError, HealthError) as exc:
        print(f"sprint-controller: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
