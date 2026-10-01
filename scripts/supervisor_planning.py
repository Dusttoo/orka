#!/usr/bin/env python3
"""Deterministic synchronization and planning support for the Orka supervisor.

This module deliberately delegates authority to ``sprint-controller.py``.  It
does not infer work, reserve tickets, or launch workers.  Its job is to invoke
the authenticated adapters, normalize their durable outputs, and calculate the
next event/deadline on which the supervisor should wake.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from api_agent import AgentError, UsageLedger, budgets_from_config, load_yaml
from breaker_runtime import BreakerRuntime
from runtime_state import canonical_config_path
from controller_runtime import execute_request, supervisor_request


class PlanningError(RuntimeError):
    pass


CONTROLLER = Path(__file__).with_name("sprint-controller.py")
ACTION_KEYS = (
    "launch",
    "scope",
    "decomposition",
    "repair",
    "recovery",
    "pr_reconciliation",
)
BREAKER_RUNTIME = BreakerRuntime()


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_json_output(result: subprocess.CompletedProcess[str], action: str) -> dict:
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "unknown failure").strip()
        raise PlanningError(f"controller {action} failed: {diagnostic}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        value = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise PlanningError(f"controller {action} returned malformed JSON") from exc
    if not isinstance(value, dict):
        raise PlanningError(f"controller {action} did not return an object")
    return value


class ControllerAdapter:
    """Run only the existing controller's read/sync/plan operations."""

    def __init__(self, repository: Path, runtime_directory: Path):
        self.repository = repository
        self.template = runtime_directory / "inventory-template.json"
        if self.template.exists():
            metadata = self.template.lstat()
            if not stat.S_ISREG(metadata.st_mode) or self.template.is_symlink():
                raise PlanningError(
                    "supervisor inventory template is not a regular file"
                )
            if self.template.read_text(encoding="utf-8") != "{}\n":
                raise PlanningError("supervisor inventory template was modified")
            self.template.chmod(0o600)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.template, flags, 0o600)
            except OSError as exc:
                raise PlanningError(
                    f"cannot create supervisor inventory template: {exc}"
                ) from exc
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(b"{}\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _run(self, *arguments: str) -> dict:
        try:
            result = subprocess.run(
                [sys.executable, str(CONTROLLER), *arguments],
                cwd=self.repository,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PlanningError(
                f"cannot run controller {' '.join(arguments)}: {exc}"
            ) from exc
        return parse_json_output(result, arguments[0])

    def synchronize(self) -> dict:
        return self._run("sync", "--inventory-template", str(self.template))

    def plan(self, sprint: str) -> dict:
        return self._run("plan", "--sprint", sprint)

    def summary(self, sprint: str) -> dict:
        return self._run("summary", "--sprint", sprint)

    def health_probe(self, role: str) -> dict:
        return self._run("health-check", "--role", role)

    def reconcile_preserved_pr(self, sprint: str, ticket: str) -> dict:
        return self._run(
            "reconcile-preserved-pr",
            "--sprint",
            sprint,
            "--ticket",
            ticket,
        )


class TransactionalControllerAdapter(ControllerAdapter):
    """Run controller operations inside the elected supervisor after cutover."""

    def __init__(
        self,
        repository: Path,
        runtime_directory: Path,
        *,
        supervisor_fence: str,
        writer_identity: str,
    ) -> None:
        super().__init__(repository, runtime_directory)
        self.supervisor_fence = supervisor_fence
        self.writer_identity = writer_identity
        self.private_root = runtime_directory / "controller-materializations"

    def _run(self, *arguments: str) -> dict:
        request = supervisor_request(
            self.repository,
            arguments,
            self.supervisor_fence,
        )
        response = execute_request(
            self.repository,
            request,
            supervisor_fence=self.supervisor_fence,
            writer_identity=self.writer_identity,
            private_root=self.private_root,
            controller_path=CONTROLLER,
        )
        result = subprocess.CompletedProcess(
            args=list(arguments),
            returncode=int(response["returncode"]),
            stdout=str(response.get("stdout") or ""),
            stderr=str(response.get("stderr") or ""),
        )
        return parse_json_output(result, arguments[0])


def _open_reservations(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    open_items: dict[str, dict[str, Any]] = {}
    for event in events:
        reservation_id = str(event.get("reservation_id") or "")
        if event.get("kind") == "reservation" and reservation_id:
            open_items[reservation_id] = event
        elif event.get("kind") in {"usage", "release"} and reservation_id:
            open_items.pop(reservation_id, None)
    return open_items


def sprint_budget(repository: Path, sprint: str) -> dict[str, Any]:
    """Return an evidence-bearing view of the hard sprint budget."""

    try:
        config = load_yaml(canonical_config_path(repository))
        limit = budgets_from_config(config, repository)["max_usd_per_sprint"]
        events = UsageLedger(repository).snapshot()
    except AgentError as exc:
        raise PlanningError(str(exc)) from exc
    spent = sum(
        (
            Decimal(str(event.get("cost_usd") or "0"))
            for event in events
            if event.get("kind") == "usage" and str(event.get("sprint") or "") == sprint
        ),
        Decimal("0"),
    )
    reserved = sum(
        (
            Decimal(str(event.get("projected_cost_usd") or "0"))
            for event in _open_reservations(events).values()
            if str(event.get("sprint") or "") == sprint
        ),
        Decimal("0"),
    )
    total = spent + reserved
    receipt = {
        "sprint": sprint,
        "spent_usd": str(spent),
        "reserved_usd": str(reserved),
        "projected_usd": str(total),
        "absolute_ceiling_usd": str(limit),
    }
    receipt["digest"] = canonical_digest(receipt)
    receipt["exhausted"] = bool(limit > 0 and total >= limit)
    return receipt


def durable_deadlines(plan: dict[str, Any]) -> list[float]:
    deadlines: list[float] = []
    for item in plan.get("health_probes") or []:
        value = item.get("retry_at")
        if isinstance(value, (int, float)) and value > 0:
            deadlines.append(float(value))
    for item in plan.get("retry_waiting") or []:
        value = item.get("retry_at")
        if isinstance(value, (int, float)) and value > 0:
            deadlines.append(float(value))
    for item in plan.get("provider_holds") or []:
        values = [item.get("retry_at"), item.get("probe_until")]
        numeric = [
            float(value)
            for value in values
            if isinstance(value, (int, float)) and value > 0
        ]
        if numeric:
            deadlines.append(max(numeric))
    return sorted(set(deadlines))


def due_health_roles(previous_plan: dict[str, Any], current_time: float) -> list[str]:
    roles = []
    for item in previous_plan.get("health_probes") or []:
        retry_at = item.get("retry_at")
        role = item.get("role")
        if (
            isinstance(retry_at, (int, float))
            and retry_at <= current_time
            and isinstance(role, str)
        ):
            roles.append(role)
    return sorted(set(roles))


def classify_cycle(
    plan: dict[str, Any],
    summary: dict[str, Any],
    budget: dict[str, Any],
    *,
    current_time: float | None = None,
    sync_interval: float = 60.0,
) -> dict[str, Any]:
    current_time = time.time() if current_time is None else current_time
    all_deadlines = durable_deadlines(plan)
    deadlines = [value for value in all_deadlines if value > current_time]
    sync_deadline = current_time + sync_interval
    overdue_work = bool(
        any(value <= current_time for value in all_deadlines)
        or any(
            isinstance(item.get("retry_at"), (int, float))
            and item.get("retry_at", 0) <= current_time
            for item in plan.get("health_probes") or []
        )
    )
    next_wake = min(
        [sync_deadline, *deadlines, *([current_time + 0.1] if overdue_work else [])]
    )
    planned = {key: list(plan.get(key) or []) for key in ACTION_KEYS}
    required_roles = set(plan.get("required_roles") or [])
    route_blocked_roles = set(plan.get("route_blocked_roles") or [])
    all_routes_unavailable = (
        bool(required_roles)
        and required_roles <= route_blocked_roles
        and not any(planned.values())
        and not plan.get("running")
    )
    sprint = str((plan.get("sprint") or summary.get("sprint") or {}).get("id") or "")
    if not sprint:
        sprint = "unknown-sprint"
    pressure_breakers = []
    work_in_progress = plan.get("work_in_progress") or {}
    if work_in_progress.get("fresh_launch_paused"):
        pressure_breakers.append(
            BREAKER_RUNTIME.sprint_record(
                "unfinished_pr_pressure",
                sprint=sprint,
                evidence={
                    "pressure_class": "unfinished_prs",
                    "capacity_snapshot": work_in_progress,
                },
            )
        )
    concurrency = int(plan.get("concurrency_max") or 1)
    running_count = len(plan.get("running") or [])
    if running_count >= concurrency or int(plan.get("over_capacity") or 0) > 0:
        pressure_breakers.append(
            BREAKER_RUNTIME.sprint_record(
                "lane_capacity_pressure",
                sprint=sprint,
                evidence={
                    "pressure_class": "lane_capacity",
                    "capacity_snapshot": {
                        "running": running_count,
                        "capacity": concurrency,
                        "over_capacity": int(plan.get("over_capacity") or 0),
                    },
                },
            )
        )
    global_breakers = []
    if all_routes_unavailable:
        global_breakers.append(
            BREAKER_RUNTIME.sprint_record(
                "all_routes_unavailable",
                sprint=sprint,
                evidence={
                    "route_incidents": list(plan.get("route_breakers") or []),
                    "next_probe_at": next_wake,
                },
            )
        )
    if budget.get("exhausted"):
        global_breakers.append(
            BREAKER_RUNTIME.sprint_record(
                "max_usd_per_sprint",
                sprint=sprint,
                evidence={"budget_receipt": budget},
            )
        )
    snapshot = {
        "sprint": plan.get("sprint") or summary.get("sprint") or {},
        "plan": planned,
        "running": list(plan.get("running") or []),
        "waiting_count": len(plan.get("waiting") or []),
        "retry_waiting": list(plan.get("retry_waiting") or []),
        "health_probes": list(plan.get("health_probes") or []),
        "provider_holds": list(plan.get("provider_holds") or []),
        "route_breakers": list(plan.get("route_breakers") or []),
        "ticket_breakers": list(plan.get("ticket_breakers") or []),
        "pressure_breakers": pressure_breakers,
        "global_breakers": global_breakers,
        "required_roles": sorted(required_roles),
        "route_blocked_roles": sorted(route_blocked_roles),
        "decision_queue_count": len(plan.get("decision_queue") or []),
        "legacy_reconciliation_count": len(plan.get("legacy_reconciliation") or []),
        "autonomous_work_remaining": bool(plan.get("autonomous_work_remaining")),
        "autonomous_work_exhausted": not bool(plan.get("autonomous_work_remaining")),
        "sprint_complete": bool(summary.get("sprint_complete")),
        "all_routes_unavailable": all_routes_unavailable,
        "budget": budget,
        "resource_claims": dict(plan.get("resource_claims") or {}),
        "allocation_candidates": {
            key: list(value or [])
            for key, value in (plan.get("allocation_candidates") or {}).items()
        },
        "next_wake_epoch": next_wake,
        "wait_reason": (
            "durable-deadline"
            if (deadlines and next_wake in deadlines) or overdue_work
            else "authoritative-sync"
        ),
    }
    snapshot["plan_digest"] = canonical_digest(
        {
            key: value
            for key, value in snapshot.items()
            if key not in {"plan_digest", "next_wake_epoch", "wait_reason"}
        }
    )
    return snapshot


def planning_cycle(
    adapter: ControllerAdapter,
    repository: Path,
    previous_plan: dict[str, Any] | None,
    *,
    current_time: float | None = None,
    sync_interval: float = 60.0,
) -> dict[str, Any]:
    current_time = time.time() if current_time is None else current_time
    for role in due_health_roles(previous_plan or {}, current_time):
        try:
            adapter.health_probe(role)
        except PlanningError:
            # The provider-health adapter records its authoritative incident.
            # Planning must still continue so the resulting hold gets a durable
            # retry deadline instead of terminating the whole sprint.
            pass
    synchronized = adapter.synchronize()
    sprint_value = synchronized.get("sprint") or {}
    sprint = str(sprint_value.get("id") or "")
    if not sprint:
        raise PlanningError("controller synchronization returned no sprint identity")
    plan = adapter.plan(sprint)
    reconciled_preserved_prs = []
    for ticket in plan.get("pr_reconciliation") or []:
        result = adapter.reconcile_preserved_pr(sprint, str(ticket))
        reconciled_preserved_prs.append(
            {
                "ticket": str(ticket),
                "state": result.get("state"),
                "recovery_binding_digest": canonical_digest(
                    result.get("recovery_binding") or {}
                ),
            }
        )
    if reconciled_preserved_prs:
        plan = adapter.plan(sprint)
    summary = adapter.summary(sprint)
    budget = sprint_budget(repository, sprint)
    result = classify_cycle(
        plan,
        summary,
        budget,
        current_time=current_time,
        sync_interval=sync_interval,
    )
    result["synchronized_ticket_count"] = int(synchronized.get("tickets") or 0)
    result["checkpoint"] = str(synchronized.get("checkpoint") or "")
    result["sync_receipt_digest"] = canonical_digest(synchronized)
    result["reconciled_preserved_prs"] = reconciled_preserved_prs
    return result
