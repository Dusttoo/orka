#!/usr/bin/env python3
"""Run and control Orka's host-owned repository supervisor process.

The supervisor owns the repository lease and a deterministic synchronization /
planning loop. It reserves, launches, and reconciles controller-authorized
workers without requiring a recurring AI captain turn.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_state import (
    RuntimeStateError,
    assert_cutover_runtime_compatible,
    canonical_config_path,
    runtime_cutover_marker,
    shared_repository_root,
    shared_runtime_path,
    working_repository_root,
)
from api_agent import AgentError, load_yaml
from breaker_runtime import BreakerRuntime
from supervisor_planning import (
    ControllerAdapter,
    PlanningError,
    TransactionalControllerAdapter,
    canonical_digest,
    planning_cycle,
)
from controller_runtime import (
    ControllerRuntimeError,
    execute_request as execute_controller_request,
)
from supervisor_dispatch import DispatchError, StaleResultError, SupervisorDispatcher
from supervisor_admission import AdmissionError, validate_persisted_jobs
from supervisor_allocation import AllocationError, allocate_lanes
from supervisor_state import (
    CURRENT_SCHEMA_VERSION,
    SupervisorStateError,
    migrate_supervisor_state,
    migration_authorizes_runtime,
)


class SupervisorError(RuntimeError):
    pass


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = PLUGIN_ROOT / "contracts/supervisor-lifecycle-v1.json"
BREAKER_CONTRACT_PATH = PLUGIN_ROOT / "contracts/breaker-classification-v1.json"
RUNTIME_RELATIVE = Path(".orchestration/.supervisor")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_REASON = 2000
MAX_REQUEST = 1024 * 1024
SUPERVISOR_STATES = {
    "starting",
    "active",
    "paused",
    "draining",
    "degraded",
    "takeover_pending",
    "stopped",
}
ACTIVE_JOB_STATES = {"running", "reserved", "launch_uncertain"}
TERMINAL_JOB_STATES = {"completed", "decomposed", "cancelled"}
BREAKER_RUNTIME = BreakerRuntime(BREAKER_CONTRACT_PATH)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def bounded_timeout(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if value < 0.1 or value > 300:
        raise argparse.ArgumentTypeError("timeout must be between 0.1 and 300 seconds")
    return value


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SupervisorError(f"{label} is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisorError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise SupervisorError(f"{label} must contain a JSON object")
    return value


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def open_private_file(path: Path, *, append: bool = False) -> Any:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SupervisorError(
            f"cannot open private runtime file {path}: {exc}"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise SupervisorError(f"private runtime path is not a regular file: {path}")
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "ab" if append else "wb", buffering=0)


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SupervisorError("supervisor runtime directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise SupervisorError("supervisor runtime path is not a private directory")
    path.chmod(0o700)


def resolve_repository(raw: str | None) -> Path:
    requested = Path(raw).expanduser() if raw else Path.cwd()
    working = working_repository_root(requested)
    try:
        shared = shared_repository_root(working)
    except RuntimeStateError as exc:
        raise SupervisorError(str(exc)) from exc
    try:
        subprocess.run(
            ["git", "-C", str(shared), "rev-parse", "--git-dir"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SupervisorError(f"repository is not a Git checkout: {shared}") from exc
    return shared.resolve()


def runtime_paths(repository: Path) -> dict[str, Path]:
    try:
        directory = shared_runtime_path(repository, RUNTIME_RELATIVE)
    except RuntimeStateError as exc:
        raise SupervisorError(str(exc)) from exc
    repository_hash = digest_bytes(str(repository).encode("utf-8"))[:24]
    socket_path = Path(f"/tmp/orka-supervisor-{os.getuid()}-{repository_hash}.sock")
    return {
        "directory": directory,
        "state": directory / "state.json",
        "state_digest": directory / "state.sha256.json",
        "lock": directory / "lease.lock",
        "log": directory / "supervisor.log",
        "socket": socket_path,
    }


def process_identity(pid: int) -> dict[str, Any]:
    if pid < 1:
        raise SupervisorError("supervisor PID must be positive")
    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:
        raise SupervisorError(f"supervisor PID {pid} is absent") from exc
    except (PermissionError, OSError) as exc:
        raise SupervisorError(f"cannot inspect supervisor PID {pid}: {exc}") from exc

    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            started = raw[raw.rindex(")") + 2 :].split()[19]
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="utf-8")
                .strip()
            )
        except (OSError, ValueError, IndexError) as exc:
            raise SupervisorError("cannot verify Linux supervisor identity") from exc
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
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            size = library.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
            )
        except (OSError, AttributeError) as exc:
            raise SupervisorError("cannot inspect macOS supervisor identity") from exc
        if size != ctypes.sizeof(info) or info.pid != pid or not info.start_tvsec:
            raise SupervisorError("macOS did not return an exact supervisor identity")
        marker = f"darwin:{info.start_tvsec}:{info.start_tvusec}"
        boot_id = ""
    else:
        raise SupervisorError(
            f"supervisor process identity is unsupported on {sys.platform}"
        )
    return {
        "pid": pid,
        "start_identity": marker,
        "start_fingerprint": digest_bytes(f"{pid}:{marker}".encode("utf-8")),
        "boot_id": boot_id,
        "session_id": os.getsid(pid),
    }


def process_status(identity: Any) -> str:
    if not isinstance(identity, dict) or not isinstance(identity.get("pid"), int):
        return "unknown"
    try:
        current = process_identity(identity["pid"])
    except SupervisorError:
        try:
            os.kill(identity["pid"], 0)
        except ProcessLookupError:
            return "absent"
        except (PermissionError, OSError):
            return "unknown"
        return "unknown"
    if current["start_fingerprint"] != identity.get("start_fingerprint"):
        return "absent"
    return "live"


class Lifecycle:
    def __init__(self, contract: dict[str, Any]):
        self.contract = contract
        self.events = contract.get("events") or {}
        self.transitions: dict[tuple[str, str], str] = {}
        for transition in contract.get("transitions") or []:
            if transition.get("entity") != "supervisor":
                continue
            key = (str(transition.get("from")), str(transition.get("event")))
            if key in self.transitions:
                raise SupervisorError(f"ambiguous supervisor transition: {key}")
            self.transitions[key] = str(transition.get("to"))

    def transition(
        self, state: dict[str, Any], event: str, evidence: dict[str, Any]
    ) -> None:
        source = str(state.get("lifecycle_state") or "")
        target = self.transitions.get((source, event))
        if not target:
            raise SupervisorError(f"event {event} is not allowed from {source}")
        definition = self.events.get(event)
        if not isinstance(definition, dict) or definition.get("entity") != "supervisor":
            raise SupervisorError(f"undefined supervisor event: {event}")
        required = definition.get("required_evidence") or []
        missing = [
            key
            for key in required
            if evidence.get(key) is None or evidence.get(key) == ""
        ]
        if missing:
            raise SupervisorError(
                f"event {event} is missing evidence: {', '.join(missing)}"
            )
        occurred = now()
        state["lifecycle_state"] = target
        state["updated_at"] = occurred
        state["last_event"] = event
        state.setdefault("history", []).append(
            {
                "at": occurred,
                "event": event,
                "from": source,
                "to": target,
                "evidence": evidence,
            }
        )
        state["history"] = state["history"][-256:]


def contract() -> tuple[dict[str, Any], Lifecycle, str]:
    value = load_json(CONTRACT_PATH, "supervisor lifecycle contract")
    if value.get("schema_version") != 1 or value.get("contract_id") != (
        "orka.supervisor-lifecycle"
    ):
        raise SupervisorError("unsupported supervisor lifecycle contract")
    return value, Lifecycle(value), digest_bytes(CONTRACT_PATH.read_bytes())


def bind_global_breaker(
    state: dict[str, Any],
    breaker: dict[str, Any],
    transition_evidence: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Bind one sprint breaker to a durable generation without replay churn."""

    if (
        breaker.get("scope") != "sprint"
        or not breaker.get("global_transition")
        or breaker.get("durable_state") not in {"degraded", "paused", "stopped"}
        or not breaker.get("record_digest")
    ):
        raise SupervisorError(
            "only a contract-declared sprint breaker may change global state"
        )
    planning = state.setdefault("planning", {})
    active = planning.get("active_global_breaker") or {}
    if active.get("record_digest") == breaker["record_digest"]:
        return active, False
    if active:
        archived = copy.deepcopy(active)
        archived["superseded_at"] = now()
        planning.setdefault("global_breaker_history", []).append(archived)
        planning["global_breaker_history"] = planning["global_breaker_history"][-64:]
    sequence = int(planning.get("global_breaker_sequence") or 0) + 1
    bound = copy.deepcopy(breaker)
    bound["generation"] = (
        f"{int((state.get('lease') or {}).get('generation') or 0)}:"
        f"{sequence}:{breaker['record_digest'][:16]}"
    )
    bound["transition_evidence"] = copy.deepcopy(transition_evidence)
    bound["activated_at"] = now()
    planning["global_breaker_sequence"] = sequence
    planning["active_global_breaker"] = bound
    return bound, True


def transition_global_breaker(
    state: dict[str, Any],
    lifecycle: Lifecycle,
    breaker: dict[str, Any],
    event: str,
    evidence: dict[str, Any],
) -> bool:
    previous_planning = copy.deepcopy(state.get("planning") or {})
    bound, changed = bind_global_breaker(state, breaker, evidence)
    if not changed and state.get("lifecycle_state") == bound["durable_state"]:
        return False
    supplied = {**evidence, "breaker_generation": bound["generation"]}
    try:
        lifecycle.transition(state, event, supplied)
    except Exception:
        state["planning"] = previous_planning
        raise
    return True


def clear_global_breaker(
    state: dict[str, Any],
    lifecycle: Lifecycle,
    event: str,
    evidence: dict[str, Any],
) -> bool:
    planning = state.setdefault("planning", {})
    active = planning.get("active_global_breaker") or {}
    generation = str(active.get("generation") or "")
    if not generation:
        raise SupervisorError("global breaker resolution lacks an exact generation")
    lifecycle.transition(state, event, {**evidence, "breaker_generation": generation})
    archived = copy.deepcopy(active)
    archived["resolved_at"] = now()
    archived["resolution_event"] = event
    planning.setdefault("global_breaker_history", []).append(archived)
    planning["global_breaker_history"] = planning["global_breaker_history"][-64:]
    planning["active_global_breaker"] = {}
    return True


def runtime_fingerprint() -> str:
    """Bind restart authority to the exact runtime that wrote durable state."""

    return digest_bytes(
        Path(__file__).read_bytes()
        + (PLUGIN_ROOT / "scripts/supervisor_dispatch.py").read_bytes()
        + (PLUGIN_ROOT / "scripts/supervisor_admission.py").read_bytes()
        + CONTRACT_PATH.read_bytes()
        + BREAKER_CONTRACT_PATH.read_bytes()
        + (PLUGIN_ROOT / "scripts/breaker_contract.py").read_bytes()
        + (PLUGIN_ROOT / "scripts/breaker_runtime.py").read_bytes()
        + (PLUGIN_ROOT / "scripts/supervisor_state.py").read_bytes()
        + (PLUGIN_ROOT / "contracts/resource-claims-v1.json").read_bytes()
        + (PLUGIN_ROOT / "scripts/runtime_state.py").read_bytes()
        + (PLUGIN_ROOT / ".codex-plugin/plugin.json").read_bytes()
    )


def new_lease(
    lock_handle: Any, paths: dict[str, Path], generation: int
) -> dict[str, Any]:
    held = os.fstat(lock_handle.fileno())
    return {
        "id": str(uuid.uuid4()),
        "generation": generation,
        "lock_path": str(paths["lock"]),
        "lock_device": held.st_dev,
        "lock_inode": held.st_ino,
        "acquired_at": now(),
        "released_at": "",
        "release_count": 0,
    }


def takeover_state(
    previous: dict[str, Any],
    *,
    identity: dict[str, Any],
    lease: dict[str, Any],
    lifecycle: Lifecycle,
    config_digest: str,
    runtime_digest: str,
    contract_digest: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Recover one unclean supervisor only from exact, absent predecessor proof."""

    predecessor_status = process_status(previous.get("process"))
    if predecessor_status != "absent":
        raise SupervisorError(
            "prior supervisor absence is not mechanically verified; takeover refused"
        )
    if previous.get("config_digest") != config_digest:
        raise SupervisorError(
            "repository config changed since the unclean supervisor stop"
        )
    migration_authorized = migration_authorizes_runtime(
        previous,
        runtime_fingerprint=runtime_digest,
        contract_digest=contract_digest,
    )
    if (
        previous.get("runtime_fingerprint") != runtime_digest
        and not migration_authorized
    ):
        raise SupervisorError("Orka runtime changed since the unclean supervisor stop")
    if previous.get("contract_digest") != contract_digest and not migration_authorized:
        raise SupervisorError(
            "supervisor lifecycle contract changed since the unclean stop"
        )

    old_lease = copy.deepcopy(previous.get("lease") or {})
    if (
        old_lease.get("lock_path") != lease.get("lock_path")
        or old_lease.get("lock_device") != lease.get("lock_device")
        or old_lease.get("lock_inode") != lease.get("lock_inode")
    ):
        raise SupervisorError(
            "repository supervisor lease identity changed after the crash"
        )

    previous_state = str(previous.get("lifecycle_state") or "")
    resume_state = previous_state
    if previous_state == "takeover_pending":
        resume_state = str(
            ((previous.get("takeover") or {}).get("resume_state") or "active")
        )
    if resume_state not in {"starting", "active", "degraded", "paused", "draining"}:
        raise SupervisorError(f"unsupported takeover resume state: {resume_state}")
    try:
        validate_persisted_jobs((previous.get("dispatch") or {}).get("jobs", {}))
    except AdmissionError as exc:
        raise SupervisorError(f"durable resource state is invalid: {exc}") from exc

    state = copy.deepcopy(previous)
    absence_receipt = {
        "status": predecessor_status,
        "process_fingerprint": (previous.get("process") or {}).get(
            "start_fingerprint", ""
        ),
        "lease_id": old_lease.get("id", ""),
        "lease_generation": old_lease.get("generation", 0),
    }
    absence_receipt["digest"] = digest_bytes(
        json.dumps(absence_receipt, sort_keys=True, separators=(",", ":")).encode()
    )
    state.update(
        {
            "process": identity,
            "lease": lease,
            "started_at": now(),
            "updated_at": now(),
            "stopped_at": "",
            "control_socket": str(runtime_paths(Path(state["repository"]))["socket"]),
            "config_digest": config_digest,
            "runtime_fingerprint": runtime_digest,
            "contract_digest": contract_digest,
            "failure": "",
            "takeover": {
                "resume_state": resume_state,
                "predecessor_process": copy.deepcopy(previous.get("process") or {}),
                "predecessor_lease": old_lease,
                "absence_receipt": absence_receipt,
            },
        }
    )
    lifecycle.transition(
        state,
        "takeover_requested",
        {
            "claimant_identity": identity["start_fingerprint"],
            "observed_lease": old_lease.get("id") or "unknown-prior-lease",
        },
    )
    lifecycle.transition(
        state,
        "predecessor_absent",
        {
            "absence_receipt": absence_receipt["digest"],
            "new_lease_id": lease["id"],
        },
    )
    planning = state.setdefault("planning", {})
    planning.update(settings)
    planning["next_wake_epoch"] = 0.0
    state.setdefault("dispatch", {"jobs": {}, "launch_count": 0, "terminal_count": 0})
    state.setdefault("requests", [])

    active_breaker = planning.get("active_global_breaker") or {}
    if resume_state in {"paused", "degraded"} and active_breaker:
        event = {
            "sprint_pressure": "sprint_pressure_applied",
            "sprint_wait": "all_routes_unavailable",
            "sprint_hard_budget": "hard_sprint_budget_exhausted",
        }.get(str(active_breaker.get("class_id") or ""))
        if not event:
            raise SupervisorError(
                "persisted global breaker cannot restore supervisor state"
            )
        evidence = dict(active_breaker.get("transition_evidence") or {})
        evidence["breaker_generation"] = active_breaker.get("generation")
        lifecycle.transition(state, event, evidence)
    elif resume_state == "paused":
        lifecycle.transition(
            state,
            "operator_paused",
            {
                "operator_request_id": f"takeover:{lease['id']}:restore-pause",
                "pause_mode": str(planning.get("pause_cause") or "hold"),
            },
        )
    elif resume_state == "draining":
        lifecycle.transition(
            state,
            "drain_requested",
            {"operator_request_id": f"takeover:{lease['id']}:restore-drain"},
        )
    return state


def state_snapshot(
    path: Path,
    repository: Path | None = None,
    *,
    migrate: bool = True,
    target_runtime_fingerprint: str | None = None,
    target_contract_digest: str | None = None,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise SupervisorError("supervisor state must not be a symlink")
    value = load_json(path, "supervisor state")
    if migrate:
        try:
            value, _changed = migrate_supervisor_state(
                value,
                target_runtime_fingerprint=(
                    target_runtime_fingerprint or runtime_fingerprint()
                ),
                target_contract_digest=(target_contract_digest or contract()[2]),
            )
        except SupervisorStateError as exc:
            raise SupervisorError(str(exc)) from exc
    if (
        value.get("schema_version")
        not in ({CURRENT_SCHEMA_VERSION} if migrate else {1, CURRENT_SCHEMA_VERSION})
        or value.get("contract_id") != "orka.supervisor-lifecycle"
        or value.get("lifecycle_state") not in SUPERVISOR_STATES
        or not isinstance(value.get("process"), dict)
        or not isinstance((value.get("process") or {}).get("pid"), int)
        or not isinstance(value.get("lease"), dict)
        or not isinstance((value.get("lease") or {}).get("id"), str)
        or not isinstance((value.get("lease") or {}).get("generation"), int)
        or not isinstance(value.get("history"), list)
    ):
        raise SupervisorError("unsupported supervisor state schema")
    if repository is not None and value.get("repository") != str(repository):
        raise SupervisorError("supervisor state belongs to another repository")
    return value


def verify_state_digest(state_path: Path, digest_path: Path, *, required: bool) -> str:
    """Authenticate the last fully persisted state before restart."""

    if not digest_path.is_file():
        if required:
            raise SupervisorError(
                "unclean supervisor state has no durable state-digest receipt"
            )
        return ""
    receipt = load_json(digest_path, "supervisor state digest")
    expected = str(receipt.get("sha256") or "")
    try:
        observed = digest_bytes(state_path.read_bytes())
    except OSError as exc:
        raise SupervisorError("cannot authenticate durable supervisor state") from exc
    if not expected or expected != observed:
        raise SupervisorError(
            "durable supervisor state digest does not match its receipt"
        )
    return observed


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SupervisorError("control request must be an object")
    command = value.get("command")
    if command == "controller":
        required = {
            "activation_id",
            "argv",
            "command_id",
            "expected_controller_generations",
            "repository_id",
            "supervisor_fence",
        }
        if required - value.keys():
            raise SupervisorError("controller request is missing authenticated bindings")
        return dict(value)
    if command not in {"pause", "resume", "drain", "stop", "status"}:
        raise SupervisorError("unsupported supervisor command")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise SupervisorError("request_id has an invalid format")
    reason = value.get("reason", "")
    if not isinstance(reason, str) or len(reason) > MAX_REASON:
        raise SupervisorError("reason must be a bounded string")
    return {"command": command, "request_id": request_id, "reason": reason}


def response_for(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "repository": state["repository"],
        "lifecycle_state": state["lifecycle_state"],
        "lease_id": state["lease"]["id"],
        "lease_generation": state["lease"]["generation"],
        "process": state["process"],
        "updated_at": state["updated_at"],
        "last_event": state["last_event"],
    }


def status_response(state: dict[str, Any]) -> dict[str, Any]:
    result = response_for(state)
    result["process_status"] = process_status(state.get("process"))
    result["lease_released_at"] = (state.get("lease") or {}).get("released_at", "")
    result["lease_release_count"] = (state.get("lease") or {}).get("release_count", 0)
    result["planning"] = state.get("planning") or {"enabled": False}
    dispatch = state.get("dispatch") or {}
    jobs = dispatch.get("jobs") or {}
    job_states: dict[str, list[str]] = {}
    for job in jobs.values():
        job_states.setdefault(str(job.get("state") or "unknown"), []).append(
            str(job.get("ticket") or "")
        )
    for tickets in job_states.values():
        tickets.sort()
    planning = state.get("planning") or {"enabled": False}
    active = sorted(
        str(job.get("ticket") or "")
        for job in jobs.values()
        if job.get("state") in ACTIVE_JOB_STATES
    )
    terminal = sorted(
        str(job.get("ticket") or "")
        for job in jobs.values()
        if job.get("state") in TERMINAL_JOB_STATES
    )
    queued = sorted(
        {
            ticket
            for action in ("launch", "scope", "decomposition", "repair", "recovery")
            for ticket in ((planning.get("plan") or {}).get(action) or [])
        }
        | {
            str(job.get("ticket") or "")
            for job in jobs.values()
            if job.get("state")
            in {"queued", "repair_ready", "recovery_ready", "decomposition_ready"}
        }
    )
    retrying = sorted(
        {str(item.get("key") or "") for item in planning.get("retry_waiting") or []}
        | {
            str(job.get("ticket") or "")
            for job in jobs.values()
            if job.get("state") == "retry_wait"
        }
    )
    parked = sorted(
        {str(item.get("key") or "") for item in planning.get("decision_queue") or []}
        | {
            str(job.get("ticket") or "")
            for job in jobs.values()
            if job.get("state") in {"parked_decision", "parked_external"}
        }
    )
    blocked = sorted(
        {str(item.get("key") or "") for item in planning.get("waiting") or []}
        | {
            str(job.get("ticket") or "")
            for job in jobs.values()
            if job.get("state") == "blocked"
        }
    )
    route_held = sorted(
        str(item.get("subject") or "") for item in planning.get("route_breakers") or []
    )
    pressure_limited = sorted(
        str(item.get("source_id") or "")
        for item in planning.get("pressure_breakers") or []
    )
    active_global = dict(planning.get("active_global_breaker") or {})
    globally_paused = (
        active_global if state.get("lifecycle_state") in {"paused", "stopped"} else {}
    )
    categories = {
        "queued": queued,
        "active": active,
        "retrying": retrying,
        "parked": parked,
        "route_held": route_held,
        "pressure_limited": pressure_limited,
        "globally_paused": globally_paused,
        "blocked": blocked,
        "terminal": terminal,
    }
    result["dispatch"] = {
        "active_jobs": sum(
            1 for job in jobs.values() if job.get("state") in ACTIVE_JOB_STATES
        ),
        "terminal_jobs": sum(
            1 for job in jobs.values() if job.get("state") in TERMINAL_JOB_STATES
        ),
        "launch_count": int(dispatch.get("launch_count") or 0),
        "terminal_count": int(dispatch.get("terminal_count") or 0),
        "job_states": job_states,
        "queued": queued,
        "active": active,
        "retrying": retrying,
        "parked": parked,
        "route_held": route_held,
        "pressure_limited": pressure_limited,
        "globally_paused": globally_paused,
        "blocked": blocked,
        "terminal": terminal,
        "categories": categories,
        "ticket_breakers": list(planning.get("ticket_breakers") or []),
        "route_breakers": list(planning.get("route_breakers") or []),
        "pressure_breakers": list(planning.get("pressure_breakers") or []),
        "active_global_breaker": dict(planning.get("active_global_breaker") or {}),
        "lane_allocation": planning.get("lane_allocation")
        or {
            "selections": [],
            "next_cursor": int(planning.get("allocation_cursor") or 0),
        },
    }
    return result


def planning_settings(repository: Path) -> dict[str, Any]:
    """Resolve optional planning settings without breaking lifecycle-only repos."""

    try:
        config = load_yaml(canonical_config_path(repository))
    except (AgentError, RuntimeStateError) as exc:
        raise SupervisorError(str(exc)) from exc
    configured = str(config.get("sprint_id") or "").strip()
    raw_interval = config.get("supervisor_sync_interval_seconds", 60)
    try:
        interval = float(raw_interval)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(
            "supervisor_sync_interval_seconds must be numeric"
        ) from exc
    if interval < 5 or interval > 3600:
        raise SupervisorError(
            "supervisor_sync_interval_seconds must be from 5 through 3600"
        )
    raw_concurrency = config.get("concurrency_max", 1)
    try:
        concurrency = int(raw_concurrency)
    except (TypeError, ValueError) as exc:
        raise SupervisorError("concurrency_max must be a positive integer") from exc
    if (
        isinstance(raw_concurrency, bool)
        or str(concurrency) != str(raw_concurrency)
        or concurrency < 1
    ):
        raise SupervisorError("concurrency_max must be a positive integer")
    raw_heavy = config.get("max_heavy_processes", concurrency)
    try:
        heavy_capacity = int(raw_heavy)
    except (TypeError, ValueError) as exc:
        raise SupervisorError("max_heavy_processes must be a positive integer") from exc
    if (
        isinstance(raw_heavy, bool)
        or str(heavy_capacity) != str(raw_heavy)
        or heavy_capacity < 1
    ):
        raise SupervisorError("max_heavy_processes must be a positive integer")
    raw_retry_delay = config.get("supervisor_ticket_retry_seconds", 30)
    try:
        retry_delay = float(raw_retry_delay)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(
            "supervisor_ticket_retry_seconds must be numeric"
        ) from exc
    if retry_delay < 5 or retry_delay > 3600:
        raise SupervisorError(
            "supervisor_ticket_retry_seconds must be from 5 through 3600"
        )
    return {
        "enabled": bool(configured),
        "requested_sprint": configured,
        "sync_interval_seconds": interval,
        "concurrency_max": concurrency,
        "max_heavy_processes": heavy_capacity,
        "ticket_retry_seconds": retry_delay,
    }


def prior_response(
    state: dict[str, Any], request: dict[str, str]
) -> dict[str, Any] | None:
    for item in reversed(state.get("requests", [])):
        if item.get("request_id") != request["request_id"]:
            continue
        if item.get("command") != request["command"]:
            raise SupervisorError("request_id was already used for another command")
        return item.get("response")
    return None


def apply_control(
    state: dict[str, Any], lifecycle: Lifecycle, request: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    prior = prior_response(state, request)
    if isinstance(prior, dict):
        return prior, request["command"] == "stop"

    command = request["command"]
    request_id = request["request_id"]
    reason = request["reason"]
    should_stop = False
    if command == "status":
        pass
    elif command == "pause":
        lifecycle.transition(
            state,
            "operator_paused",
            {"operator_request_id": request_id, "pause_mode": "hold"},
        )
        planning = state.setdefault("planning", {})
        planning["pause_cause"] = "operator_paused"
        planning["operator_pause_generation"] = canonical_digest(
            {
                "request_id": request_id,
                "lease_id": (state.get("lease") or {}).get("id"),
                "reason": reason,
            }
        )
    elif command == "resume":
        planning = state.setdefault("planning", {})
        pause_cause = planning.get("pause_cause")
        active_breaker = planning.get("active_global_breaker") or {}
        if pause_cause == "all_routes_unavailable":
            raise SupervisorError(
                f"cannot override global pause condition: {pause_cause}"
            )
        if active_breaker:
            if active_breaker.get(
                "class_id"
            ) != "sprint_hard_budget" or not active_breaker.get("resolution_receipt"):
                raise SupervisorError(
                    f"cannot override global pause condition: {pause_cause or active_breaker.get('source_id')}"
                )
            clear_global_breaker(
                state,
                lifecycle,
                "operator_resumed",
                {"operator_request_id": request_id, "blockers_checked": True},
            )
        else:
            generation = str(planning.get("operator_pause_generation") or "")
            if not generation:
                raise SupervisorError("operator pause has no resumable generation")
            lifecycle.transition(
                state,
                "operator_resumed",
                {
                    "operator_request_id": request_id,
                    "blockers_checked": True,
                    "breaker_generation": generation,
                },
            )
        planning["pause_cause"] = ""
        planning["operator_pause_generation"] = ""
    elif command == "drain":
        lifecycle.transition(
            state, "drain_requested", {"operator_request_id": request_id}
        )
        active = sum(
            1
            for job in ((state.get("dispatch") or {}).get("jobs") or {}).values()
            if job.get("state") in ACTIVE_JOB_STATES
        )
        if active == 0:
            lifecycle.transition(
                state,
                "drain_completed",
                {
                    "active_job_count": 0,
                    "queue_snapshot_digest": digest_bytes(b"[]"),
                },
            )
            state.setdefault("planning", {})["pause_cause"] = "operator_drain"
    elif command == "stop":
        lifecycle.transition(
            state,
            "operator_stopped",
            {"operator_request_id": request_id, "reason": reason or "operator stop"},
        )
        should_stop = True
    response = response_for(state)
    state.setdefault("requests", []).append(
        {
            "at": now(),
            "request_id": request_id,
            "command": command,
            "response": response,
        }
    )
    state["requests"] = state["requests"][-128:]
    return response, should_stop


def lease_is_current(lock_handle: Any, lock_path: Path) -> bool:
    try:
        held = os.fstat(lock_handle.fileno())
        named = lock_path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(named.st_mode) and (held.st_dev, held.st_ino) == (
        named.st_dev,
        named.st_ino,
    )


def read_request(connection: socket.socket) -> dict[str, str]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(4096, MAX_REQUEST + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_REQUEST:
            raise SupervisorError("control request exceeds the size limit")
        if b"\n" in chunk:
            break
    try:
        value = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorError("control request is not valid JSON") from exc
    return validate_request(value)


def send_response(connection: socket.socket, value: dict[str, Any]) -> None:
    connection.sendall(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    )


def write_handshake(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, value)


def run_daemon(repository: Path, handshake: Path) -> int:
    paths = runtime_paths(repository)
    try:
        assert_cutover_runtime_compatible(repository)
        ensure_private_directory(paths["directory"])
        contract_value, lifecycle, contract_digest = contract()
        del contract_value
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_descriptor = os.open(paths["lock"], lock_flags, 0o600)
    except (OSError, RuntimeStateError, SupervisorError) as exc:
        write_handshake(handshake, {"status": "error", "error": str(exc)})
        return 2
    lock_handle = os.fdopen(lock_descriptor, "a+")
    server: socket.socket | None = None
    state: dict[str, Any] | None = None
    expected_state_digest = ""

    def persist_state() -> None:
        nonlocal expected_state_digest
        if state is None:
            raise SupervisorError("supervisor state is not initialized")
        atomic_write(paths["state"], state)
        expected_state_digest = digest_bytes(paths["state"].read_bytes())
        atomic_write(
            paths["state_digest"],
            {"sha256": expected_state_digest, "recorded_at": now()},
        )

    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_handshake(
                handshake,
                {"status": "error", "error": "repository supervisor lease is held"},
            )
            return 2

        previous = state_snapshot(paths["state"], repository, migrate=False)
        previous_lease = (previous or {}).get("lease") or {}
        previous_clean = bool(
            previous
            and previous.get("lifecycle_state") == "stopped"
            and previous_lease.get("released_at")
            and previous_lease.get("release_count") == 1
        )
        if previous:
            verify_state_digest(
                paths["state"],
                paths["state_digest"],
                required=not previous_clean,
            )
            try:
                previous, _migrated = migrate_supervisor_state(
                    previous,
                    target_runtime_fingerprint=runtime_fingerprint(),
                    target_contract_digest=contract_digest,
                )
            except SupervisorStateError as exc:
                write_handshake(handshake, {"status": "error", "error": str(exc)})
                return 2
        generation = int(previous_lease.get("generation") or 0) + 1
        os.fchmod(lock_handle.fileno(), 0o600)
        identity = process_identity(os.getpid())
        lease = new_lease(lock_handle, paths, generation)
        lease_id = lease["id"]
        try:
            config_path = canonical_config_path(repository)
        except RuntimeStateError as exc:
            write_handshake(handshake, {"status": "error", "error": str(exc)})
            return 2
        initial_config_digest = (
            digest_bytes(config_path.read_bytes()) if config_path.is_file() else ""
        )
        initial_runtime_digest = runtime_fingerprint()
        if not previous or previous_clean:
            prior_history = list((previous or {}).get("history") or [])[-255:]
            state = {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "contract_id": "orka.supervisor-lifecycle",
                "contract_schema_version": 1,
                "repository": str(repository),
                "lifecycle_state": "starting",
                "last_event": "supervisor_starting",
                "started_at": now(),
                "updated_at": now(),
                "stopped_at": "",
                "process": identity,
                "lease": lease,
                "control_socket": str(paths["socket"]),
                "history": prior_history
                + [
                    {
                        "at": now(),
                        "event": "supervisor_starting",
                        "from": None,
                        "to": "starting",
                        "evidence": {
                            "lease_id": lease_id,
                            "generation": generation,
                        },
                    }
                ],
                "requests": [],
                "config_digest": initial_config_digest,
                "runtime_fingerprint": initial_runtime_digest,
                "contract_digest": contract_digest,
            }
            persist_state()
        try:
            if not config_path.is_file():
                raise SupervisorError(f"repository config is missing: {config_path}")
            config_digest = initial_config_digest
            runtime_digest = initial_runtime_digest
            settings = planning_settings(repository)
            if previous and not previous_clean:
                state = takeover_state(
                    previous,
                    identity=identity,
                    lease=lease,
                    lifecycle=lifecycle,
                    config_digest=config_digest,
                    runtime_digest=runtime_digest,
                    contract_digest=contract_digest,
                    settings=settings,
                )
            else:
                lifecycle.transition(
                    state,
                    "preflight_succeeded",
                    {
                        "config_digest": config_digest,
                        "runtime_fingerprint": runtime_digest,
                        "lease_id": lease_id,
                    },
                )
                state["config_digest"] = config_digest
                state["runtime_fingerprint"] = runtime_digest
                state["contract_digest"] = contract_digest
                state["planning"] = settings
                state["planning"].update(
                    {
                        "cycle_count": 0,
                        "last_sync_at": "",
                        "last_error": "",
                        "next_wake_epoch": 0.0,
                        "plan_digest": "",
                        "allocation_cursor": 0,
                        "lane_allocation": {"selections": [], "next_cursor": 0},
                    }
                )
                state["dispatch"] = {
                    "jobs": {},
                    "launch_count": 0,
                    "terminal_count": 0,
                    "last_error": "",
                }
            persist_state()
        except (OSError, RuntimeStateError, SupervisorError) as exc:
            diagnostic = str(exc)
            if state is None:
                write_handshake(handshake, {"status": "error", "error": diagnostic})
                return 2
            if state.get("lifecycle_state") == "starting":
                transition_global_breaker(
                    state,
                    lifecycle,
                    BREAKER_RUNTIME.sprint_record(
                        "preflight_failed",
                        sprint=str(repository),
                        evidence={
                            "failure_class": type(exc).__name__,
                            "diagnostic_digest": digest_bytes(
                                diagnostic.encode("utf-8")
                            ),
                        },
                    ),
                    "preflight_failed",
                    {
                        "failure_class": type(exc).__name__,
                        "diagnostic_digest": digest_bytes(diagnostic.encode("utf-8")),
                    },
                )
            else:
                transition_global_breaker(
                    state,
                    lifecycle,
                    BREAKER_RUNTIME.sprint_record(
                        "durable_state_invalid",
                        sprint=str(repository),
                        evidence={
                            "state_digest": expected_state_digest or "unavailable",
                            "validation_error": type(exc).__name__,
                        },
                    ),
                    "durable_state_invalid",
                    {
                        "state_digest": expected_state_digest or "unavailable",
                        "validation_error": type(exc).__name__,
                    },
                )
            state["failure"] = diagnostic
            state["stopped_at"] = now()
            persist_state()
            write_handshake(handshake, {"status": "error", "error": diagnostic})
            return 2

        paths["socket"].unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(paths["socket"]))
        paths["socket"].chmod(0o600)
        server.listen(8)
        server.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(server, selectors.EVENT_READ)
        write_handshake(handshake, response_for(state))

        signal_number = 0

        def stop_from_signal(received: int, _frame: Any) -> None:
            nonlocal signal_number
            signal_number = received

        signal.signal(signal.SIGTERM, stop_from_signal)
        signal.signal(signal.SIGINT, stop_from_signal)

        should_stop = False
        integrity_due = time.time()
        dispatcher = SupervisorDispatcher(
            repository,
            paths["directory"],
            CONTRACT_PATH,
            retry_delay_seconds=float(
                (state.get("planning") or {}).get("ticket_retry_seconds") or 30
            ),
            breaker_contract_path=BREAKER_CONTRACT_PATH,
        )
        while not should_stop:
            if signal_number:
                lifecycle.transition(
                    state,
                    "operator_stopped",
                    {
                        "operator_request_id": f"signal:{signal_number}",
                        "reason": "supervisor received a termination signal",
                    },
                )
                persist_state()
                should_stop = True
                continue
            current_time = time.time()
            if current_time >= integrity_due:
                try:
                    observed_state_digest = digest_bytes(paths["state"].read_bytes())
                except OSError:
                    observed_state_digest = "missing"
                if observed_state_digest != expected_state_digest:
                    breaker_evidence = {
                        "state_digest": observed_state_digest,
                        "validation_error": "durable supervisor state changed outside its owner",
                    }
                    transition_global_breaker(
                        state,
                        lifecycle,
                        BREAKER_RUNTIME.sprint_record(
                            "durable_state_invalid",
                            sprint=str(
                                (
                                    (
                                        (state.get("planning") or {}).get("sprint")
                                        or {}
                                    ).get("id")
                                )
                                or repository
                            ),
                            evidence=breaker_evidence,
                        ),
                        "durable_state_invalid",
                        breaker_evidence,
                    )
                    persist_state()
                    should_stop = True
                    continue
                if not lease_is_current(lock_handle, paths["lock"]):
                    breaker_evidence = {
                        "lease_id": lease_id,
                        "observed_owner": "lease path no longer names the held inode",
                    }
                    transition_global_breaker(
                        state,
                        lifecycle,
                        BREAKER_RUNTIME.sprint_record(
                            "lease_lost",
                            sprint=str(
                                (
                                    (
                                        (state.get("planning") or {}).get("sprint")
                                        or {}
                                    ).get("id")
                                )
                                or repository
                            ),
                            evidence=breaker_evidence,
                        ),
                        "lease_lost",
                        breaker_evidence,
                    )
                    persist_state()
                    should_stop = True
                    continue
                integrity_due = current_time + 1.0

            planning = state.get("planning") or {}
            dispatch = state.get("dispatch") or {}
            jobs = dispatch.setdefault("jobs", {})
            awakened = dispatcher.wake_due_retries(jobs, current_time=current_time)
            if awakened:
                planning["next_wake_epoch"] = 0.0
                for job in awakened:
                    state.setdefault("history", []).append(
                        {
                            "at": now(),
                            "event": "ticket_retry_woken",
                            "from": state["lifecycle_state"],
                            "to": state["lifecycle_state"],
                            "evidence": {
                                "ticket": job["ticket"],
                                "run_ref": job["run_ref"],
                                "timer_id": (job.get("terminal") or {}).get("timer_id"),
                            },
                        }
                    )
                state["history"] = state["history"][-256:]
                persist_state()
            terminal_applied = False
            for run_ref, job in list(jobs.items()):
                if job.get("state") != "running":
                    continue
                try:
                    result = dispatcher.apply_process_exit(job)
                    if result and result.get("applied"):
                        terminal_applied = True
                        dispatch["terminal_count"] = (
                            int(dispatch.get("terminal_count") or 0) + 1
                        )
                        state.setdefault("history", []).append(
                            {
                                "at": now(),
                                "event": "worker_terminal_applied",
                                "from": state["lifecycle_state"],
                                "to": state["lifecycle_state"],
                                "evidence": {
                                    "ticket": job["ticket"],
                                    "run_ref": run_ref,
                                    "result_digest": result["terminal"][
                                        "result_digest"
                                    ],
                                    "contract_event": result["terminal"]["event"],
                                },
                            }
                        )
                except StaleResultError as exc:
                    terminal_applied = True
                    job["state"] = "terminal_rejected"
                    job["terminal_rejection"] = {
                        "at": now(),
                        "reason": str(exc),
                    }
                    dispatch["last_error"] = str(exc)
                    state.setdefault("history", []).append(
                        {
                            "at": now(),
                            "event": "worker_terminal_rejected_stale",
                            "from": state["lifecycle_state"],
                            "to": state["lifecycle_state"],
                            "evidence": {
                                "ticket": job["ticket"],
                                "run_ref": run_ref,
                                "diagnostic_digest": digest_bytes(str(exc).encode()),
                            },
                        }
                    )
                except (DispatchError, OSError, json.JSONDecodeError) as exc:
                    dispatch["last_error"] = str(exc)
                    state.setdefault("history", []).append(
                        {
                            "at": now(),
                            "event": "worker_terminal_processing_failed",
                            "from": state["lifecycle_state"],
                            "to": state["lifecycle_state"],
                            "evidence": {
                                "ticket": job["ticket"],
                                "run_ref": run_ref,
                                "diagnostic_digest": digest_bytes(str(exc).encode()),
                            },
                        }
                    )
            if terminal_applied:
                planning["next_wake_epoch"] = 0.0
                state["history"] = state["history"][-256:]
                persist_state()
            if state.get("lifecycle_state") == "draining":
                active = sum(
                    1 for job in jobs.values() if job.get("state") in ACTIVE_JOB_STATES
                )
                if active == 0:
                    lifecycle.transition(
                        state,
                        "drain_completed",
                        {
                            "active_job_count": 0,
                            "queue_snapshot_digest": canonical_digest([]),
                        },
                    )
                    planning["pause_cause"] = "operator_drain"
                    persist_state()
            pause_cause = planning.get("pause_cause")
            system_pause = state.get("lifecycle_state") == "paused" and pause_cause in {
                "all_routes_unavailable",
                "hard_sprint_budget_exhausted",
            }
            planning_due = (
                bool(planning.get("enabled"))
                and (
                    state.get("lifecycle_state") in {"active", "degraded"}
                    or system_pause
                )
                and current_time >= float(planning.get("next_wake_epoch") or 0)
            )
            if planning_due:
                try:
                    snapshot = planning_cycle(
                        (
                            TransactionalControllerAdapter(
                                repository,
                                paths["directory"],
                                supervisor_fence=(
                                    f"{state['lease']['id']}:{state['lease']['generation']}"
                                ),
                                writer_identity=(
                                    f"supervisor:{state['lease']['id']}:"
                                    f"{state['lease']['generation']}"
                                ),
                            )
                            if runtime_cutover_marker(repository) is not None
                            else ControllerAdapter(repository, paths["directory"])
                        ),
                        repository,
                        planning,
                        current_time=current_time,
                        sync_interval=float(planning["sync_interval_seconds"]),
                    )
                    previous_digest = str(planning.get("plan_digest") or "")
                    planning.update(
                        {
                            **snapshot,
                            "cycle_count": int(planning.get("cycle_count") or 0) + 1,
                            "last_sync_at": now(),
                            "last_error": "",
                        }
                    )
                    active_job_count = sum(
                        1
                        for job in jobs.values()
                        if job.get("state") in ACTIVE_JOB_STATES
                    )
                    heavy_capacity = int(planning.get("max_heavy_processes") or 1)
                    if active_job_count >= heavy_capacity:
                        snapshot["pressure_breakers"].append(
                            BREAKER_RUNTIME.sprint_record(
                                "heavy_process_pressure",
                                sprint=str(
                                    (snapshot.get("sprint") or {}).get("id")
                                    or "unknown-sprint"
                                ),
                                evidence={
                                    "pressure_class": "heavy_process",
                                    "capacity_snapshot": {
                                        "active": active_job_count,
                                        "capacity": heavy_capacity,
                                    },
                                },
                            )
                        )
                        planning["pressure_breakers"] = snapshot["pressure_breakers"]
                    if snapshot["plan_digest"] != previous_digest:
                        state.setdefault("history", []).append(
                            {
                                "at": now(),
                                "event": "controller_plan_updated",
                                "from": state["lifecycle_state"],
                                "to": state["lifecycle_state"],
                                "evidence": {
                                    "plan_digest": snapshot["plan_digest"],
                                    "sync_receipt_digest": snapshot[
                                        "sync_receipt_digest"
                                    ],
                                },
                            }
                        )
                        state["history"] = state["history"][-256:]
                    if snapshot["sprint_complete"] and state["lifecycle_state"] in {
                        "active",
                        "degraded",
                        "draining",
                    }:
                        lifecycle.transition(
                            state,
                            "sprint_completed",
                            {
                                "summary_digest": snapshot["plan_digest"],
                                "authenticated_completion_receipts": snapshot[
                                    "sync_receipt_digest"
                                ],
                            },
                        )
                        should_stop = True
                    elif snapshot["budget"].get("exhausted") and state[
                        "lifecycle_state"
                    ] in {"active", "degraded"}:
                        breaker = next(
                            item
                            for item in snapshot["global_breakers"]
                            if item["source_id"] == "max_usd_per_sprint"
                        )
                        transition_global_breaker(
                            state,
                            lifecycle,
                            breaker,
                            "hard_sprint_budget_exhausted",
                            {
                                "budget_receipt": snapshot["budget"]["digest"],
                                "absolute_ceiling": snapshot["budget"][
                                    "absolute_ceiling_usd"
                                ],
                            },
                        )
                        planning["pause_cause"] = "hard_sprint_budget_exhausted"
                    elif snapshot["all_routes_unavailable"] and state[
                        "lifecycle_state"
                    ] in {"active", "degraded"}:
                        breaker = next(
                            item
                            for item in snapshot["global_breakers"]
                            if item["source_id"] == "all_routes_unavailable"
                        )
                        transition_global_breaker(
                            state,
                            lifecycle,
                            breaker,
                            "all_routes_unavailable",
                            {
                                "route_incidents": canonical_digest(
                                    snapshot["provider_holds"]
                                ),
                                "next_probe_at": snapshot["next_wake_epoch"],
                            },
                        )
                        planning["pause_cause"] = "all_routes_unavailable"
                    elif snapshot["pressure_breakers"] and state["lifecycle_state"] in {
                        "active",
                        "degraded",
                    }:
                        breaker = sorted(
                            snapshot["pressure_breakers"],
                            key=lambda item: (item["source_id"], item["record_digest"]),
                        )[0]
                        transition_global_breaker(
                            state,
                            lifecycle,
                            breaker,
                            "sprint_pressure_applied",
                            {
                                "pressure_class": breaker["source_id"],
                                "capacity_snapshot": canonical_digest(
                                    snapshot["pressure_breakers"]
                                ),
                            },
                        )
                    elif (
                        not snapshot["pressure_breakers"]
                        and state["lifecycle_state"] == "degraded"
                        and (planning.get("active_global_breaker") or {}).get(
                            "class_id"
                        )
                        == "sprint_pressure"
                    ):
                        clear_global_breaker(
                            state,
                            lifecycle,
                            "sprint_pressure_cleared",
                            {
                                "capacity_snapshot": snapshot["plan_digest"],
                            },
                        )
                    elif (
                        system_pause
                        and pause_cause == "all_routes_unavailable"
                        and not snapshot["all_routes_unavailable"]
                    ):
                        clear_global_breaker(
                            state,
                            lifecycle,
                            "routes_available",
                            {"route_health_receipts": snapshot["plan_digest"]},
                        )
                        planning["pause_cause"] = ""
                    elif (
                        system_pause
                        and pause_cause == "hard_sprint_budget_exhausted"
                        and not snapshot["budget"].get("exhausted")
                    ):
                        active_breaker = planning.get("active_global_breaker") or {}
                        if active_breaker.get("class_id") != "sprint_hard_budget":
                            raise SupervisorError(
                                "budget pause lost its exact global breaker generation"
                            )
                        active_breaker["resolution_receipt"] = snapshot["budget"][
                            "digest"
                        ]
                        active_breaker["resolved_at"] = now()
                    if state["lifecycle_state"] in {"active", "degraded"}:
                        sprint_id = str((snapshot.get("sprint") or {}).get("id") or "")
                        action_plan = {
                            name: list(values or [])
                            for name, values in (snapshot.get("plan") or {}).items()
                        }
                        local_retry_holds = {
                            str(job.get("ticket") or "")
                            for job in jobs.values()
                            if job.get("state") == "retry_wait"
                        }
                        action_plan["recovery"] = [
                            ticket
                            for ticket in action_plan.get("recovery", [])
                            if ticket not in local_retry_holds
                        ]
                        capacity = int(planning.get("concurrency_max") or 1)
                        active = sum(
                            1
                            for job in jobs.values()
                            if job.get("state") in ACTIVE_JOB_STATES
                        )
                        candidates = dict(planning.get("allocation_candidates") or {})
                        if not candidates:
                            candidates = {
                                "repair": list(action_plan.get("repair") or []),
                                "recovery": list(action_plan.get("recovery") or []),
                                "continuation": list(action_plan.get("launch") or []),
                                "dependency_unlocking": [],
                                "fresh": [],
                            }
                        candidates["recovery"] = [
                            ticket
                            for ticket in candidates.get("recovery", [])
                            if ticket not in local_retry_holds
                        ]
                        allocation = allocate_lanes(
                            candidates,
                            available=max(0, capacity - active),
                            concurrency=capacity,
                            cursor=int(planning.get("allocation_cursor") or 0),
                        )
                        planning["allocation_cursor"] = allocation["next_cursor"]
                        planning["lane_allocation"] = allocation
                        selected_actions = {
                            "repair": [],
                            "recovery": [],
                        }
                        launch_tickets = []
                        for selection in allocation["selections"]:
                            state.setdefault("history", []).append(
                                {
                                    "at": now(),
                                    "event": "lane_allocated",
                                    "from": state["lifecycle_state"],
                                    "to": state["lifecycle_state"],
                                    "evidence": selection,
                                }
                            )
                            if selection["action"] == "launch":
                                launch_tickets.append(selection["ticket"])
                            else:
                                selected_actions[selection["action"]].append(
                                    selection["ticket"]
                                )
                        dispatcher.last_errors = []
                        prepared = dispatcher.prepare_continuations(
                            sprint_id, selected_actions, jobs
                        )
                        continuation_errors = list(dispatcher.last_errors)
                        if prepared:
                            planning["next_wake_epoch"] = 0.0
                            for job in prepared:
                                state.setdefault("history", []).append(
                                    {
                                        "at": now(),
                                        "event": "ticket_continuation_queued",
                                        "from": state["lifecycle_state"],
                                        "to": state["lifecycle_state"],
                                        "evidence": {
                                            "ticket": job["ticket"],
                                            "run_ref": job["run_ref"],
                                        },
                                    }
                                )
                        launched = dispatcher.fill(
                            sprint_id,
                            launch_tickets,
                            jobs,
                            capacity,
                            heavy_capacity=int(
                                planning.get("max_heavy_processes") or 1
                            ),
                            claims_by_ticket=dict(
                                planning.get("resource_claims") or {}
                            ),
                        )
                        dispatcher.last_errors = (
                            continuation_errors + dispatcher.last_errors
                        )
                        for skipped in dispatcher.last_skips:
                            state.setdefault("history", []).append(
                                {
                                    "at": now(),
                                    "event": "worker_dispatch_skipped_resource",
                                    "from": state["lifecycle_state"],
                                    "to": state["lifecycle_state"],
                                    "evidence": skipped,
                                }
                            )
                        for failure in dispatcher.last_errors:
                            dispatch["last_error"] = failure["error"]
                            state.setdefault("history", []).append(
                                {
                                    "at": now(),
                                    "event": "worker_dispatch_rejected",
                                    "from": state["lifecycle_state"],
                                    "to": state["lifecycle_state"],
                                    "evidence": {
                                        "ticket": failure["ticket"],
                                        "diagnostic_digest": digest_bytes(
                                            failure["error"].encode()
                                        ),
                                    },
                                }
                            )
                        if launched:
                            dispatch["launch_count"] = int(
                                dispatch.get("launch_count") or 0
                            ) + len(launched)
                            for job in launched:
                                state.setdefault("history", []).append(
                                    {
                                        "at": now(),
                                        "event": "worker_dispatch_recorded",
                                        "from": state["lifecycle_state"],
                                        "to": state["lifecycle_state"],
                                        "evidence": {
                                            "ticket": job["ticket"],
                                            "run_ref": job["run_ref"],
                                            "dispatch_state": job["state"],
                                            "attempt_token_digest": digest_bytes(
                                                job["attempt_token"].encode()
                                            ),
                                            "execution_invocation": (
                                                job.get("execution_identity") or {}
                                            ).get("invocation_id", ""),
                                            "resource_claim_digest": job.get(
                                                "resource_claim_digest", ""
                                            ),
                                        },
                                    }
                                )
                            state["history"] = state["history"][-256:]
                    persist_state()
                except (PlanningError, DispatchError, AllocationError) as exc:
                    planning["last_error"] = str(exc)
                    planning["last_sync_at"] = now()
                    planning["next_wake_epoch"] = current_time + float(
                        planning["sync_interval_seconds"]
                    )
                    state.setdefault("history", []).append(
                        {
                            "at": now(),
                            "event": "controller_sync_failed",
                            "from": state["lifecycle_state"],
                            "to": state["lifecycle_state"],
                            "evidence": {
                                "diagnostic_digest": digest_bytes(
                                    str(exc).encode("utf-8")
                                )
                            },
                        }
                    )
                    state["history"] = state["history"][-256:]
                    persist_state()

            deadlines = [integrity_due]
            deadlines.extend(
                float((job.get("terminal") or {}).get("retry_at"))
                for job in jobs.values()
                if job.get("state") == "retry_wait"
                and isinstance(
                    (job.get("terminal") or {}).get("retry_at"), (int, float)
                )
            )
            if planning.get("enabled") and (
                state.get("lifecycle_state") in {"active", "degraded"} or system_pause
            ):
                deadlines.append(float(planning.get("next_wake_epoch") or current_time))
            timeout = max(0.0, min(deadlines) - time.time())
            events = selector.select(timeout)
            if not events:
                continue
            try:
                connection, _ = server.accept()
            except BlockingIOError:
                continue
            with connection:
                connection.setblocking(True)
                try:
                    request = read_request(connection)
                    if request["command"] == "controller":
                        fence = f"{state['lease']['id']}:{state['lease']['generation']}"
                        response = execute_controller_request(
                            repository,
                            request,
                            supervisor_fence=fence,
                            writer_identity=(
                                f"supervisor:{state['lease']['id']}:"
                                f"{state['lease']['generation']}"
                            ),
                            private_root=paths["directory"] / "controller-materializations",
                            controller_path=Path(__file__).with_name("sprint-controller.py"),
                        )
                        request_stop = False
                        send_response(connection, response)
                        continue
                    response, request_stop = apply_control(state, lifecycle, request)
                    if request["command"] == "resume" and state.get("planning", {}).get(
                        "enabled"
                    ):
                        state["planning"]["next_wake_epoch"] = 0.0
                        state["planning"]["pause_cause"] = ""
                    persist_state()
                    send_response(connection, response)
                    should_stop = should_stop or request_stop
                except (ControllerRuntimeError, SupervisorError) as exc:
                    send_response(connection, {"status": "error", "error": str(exc)})
        return 0
    except Exception as exc:  # fail closed and leave a diagnostic handshake
        if state is not None and state.get("lifecycle_state") != "stopped":
            try:
                breaker_evidence = {
                    "state_digest": expected_state_digest or "unavailable",
                    "validation_error": type(exc).__name__,
                }
                transition_global_breaker(
                    state,
                    lifecycle,
                    BREAKER_RUNTIME.sprint_record(
                        "durable_state_invalid",
                        sprint=str(repository),
                        evidence=breaker_evidence,
                    ),
                    "durable_state_invalid",
                    breaker_evidence,
                )
                persist_state()
            except Exception:
                pass
        if not handshake.exists():
            write_handshake(handshake, {"status": "error", "error": str(exc)})
        raise
    finally:
        if state is not None:
            lease = state.get("lease") or {}
            if not lease.get("released_at"):
                lease["released_at"] = now()
                lease["release_count"] = int(lease.get("release_count") or 0) + 1
                state["stopped_at"] = state.get("stopped_at") or now()
                state["updated_at"] = now()
                state.setdefault("history", []).append(
                    {
                        "at": now(),
                        "event": "lease_released",
                        "from": state.get("lifecycle_state"),
                        "to": state.get("lifecycle_state"),
                        "evidence": {"lease_id": lease.get("id")},
                    }
                )
                try:
                    persist_state()
                except OSError:
                    pass
        if server is not None:
            server.close()
            # Only the process that successfully bound the socket owns its
            # pathname. A duplicate starter must never unlink the active
            # supervisor's control channel when lease acquisition fails.
            paths["socket"].unlink(missing_ok=True)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def start(args: argparse.Namespace) -> None:
    repository = resolve_repository(args.repo)
    paths = runtime_paths(repository)
    ensure_private_directory(paths["directory"])
    handshake = paths["directory"] / f"start-{uuid.uuid4().hex}.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_run",
        "--repo",
        str(repository),
        "--handshake",
        str(handshake),
    ]
    with (
        open_private_file(paths["log"], append=True) as output,
        open(os.devnull, "rb") as input_stream,
    ):
        process = subprocess.Popen(
            command,
            stdin=input_stream,
            stdout=output,
            stderr=output,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and not handshake.exists():
        if process.poll() is not None:
            break
        time.sleep(0.05)
    if not handshake.exists():
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        raise SupervisorError(
            "supervisor did not publish startup evidence; inspect the private supervisor log"
        )
    result = load_json(handshake, "supervisor startup handshake")
    handshake.unlink(missing_ok=True)
    if result.get("status") != "ok":
        raise SupervisorError(str(result.get("error") or "supervisor failed to start"))
    emit(result)


def status(args: argparse.Namespace) -> None:
    repository = resolve_repository(args.repo)
    paths = runtime_paths(repository)
    snapshot = state_snapshot(paths["state"], repository)
    if snapshot is None:
        emit({"status": "not_started", "repository": str(repository)})
        return
    emit(status_response(snapshot))


def control(args: argparse.Namespace) -> None:
    repository = resolve_repository(args.repo)
    paths = runtime_paths(repository)
    snapshot = state_snapshot(paths["state"], repository)
    if snapshot is None:
        raise SupervisorError("supervisor has not been started")
    if snapshot.get("lifecycle_state") == "stopped":
        if args.command == "stop":
            emit(status_response(snapshot))
            return
        raise SupervisorError("supervisor is stopped")
    if process_status(snapshot.get("process")) != "live":
        raise SupervisorError("supervisor process identity is not live")
    expected_socket = str(paths["socket"])
    if snapshot.get("control_socket") != expected_socket:
        raise SupervisorError(
            "supervisor control socket does not match this repository"
        )
    request = {
        "command": args.command,
        "request_id": args.request_id or str(uuid.uuid4()),
        "reason": args.reason or "",
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(args.timeout)
        try:
            connection.connect(expected_socket)
            connection.sendall(
                (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            )
            response = connection.makefile("rb").readline(MAX_REQUEST + 1)
        except (OSError, TimeoutError) as exc:
            raise SupervisorError(f"supervisor control request failed: {exc}") from exc
    if not response or len(response) > MAX_REQUEST:
        raise SupervisorError("supervisor returned an invalid control response")
    try:
        result = json.loads(response)
    except json.JSONDecodeError as exc:
        raise SupervisorError("supervisor returned malformed JSON") from exc
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise SupervisorError(str((result or {}).get("error") or "control failed"))
    if args.command == "stop":
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            stopped = state_snapshot(paths["state"], repository)
            if (
                stopped
                and stopped.get("lifecycle_state") == "stopped"
                and (stopped.get("lease") or {}).get("release_count") == 1
                and process_status(stopped.get("process")) == "absent"
            ):
                result = status_response(stopped)
                break
            time.sleep(0.05)
        else:
            raise SupervisorError(
                "supervisor did not publish clean lease release before timeout"
            )
    emit(result)


def resolve_decision_command(args: argparse.Namespace) -> None:
    """Resolve one contract-classified ticket without disturbing other lanes."""

    repository = resolve_repository(args.repo)
    settings = planning_settings(repository)
    sprint = str(args.sprint or settings.get("requested_sprint") or "").strip()
    if not sprint:
        raise SupervisorError(
            "resolve-decision requires a configured or explicit sprint"
        )
    contract_value, _lifecycle, _digest = contract()
    allowed = {
        item.get("class")
        for item in contract_value.get("operator_only_decisions", [])
        if isinstance(item, dict)
    }
    if args.decision_class not in allowed:
        raise SupervisorError(
            "decision class is not permitted by the lifecycle contract"
        )
    operator_token = ""
    if args.operator_capability_stdin:
        operator_token = sys.stdin.readline().strip()
        if not operator_token:
            raise SupervisorError("operator capability stdin was empty")
    elif args.operator_capability:
        operator_token = args.operator_capability
    controller = Path(__file__).with_name("sprint-controller.py")
    command = [
        sys.executable,
        str(controller),
        "resolve-decision",
        "--sprint",
        sprint,
        "--ticket",
        args.ticket,
        "--decision-class",
        args.decision_class,
        "--decision-receipt",
        args.decision_receipt,
        "--reason",
        args.reason,
    ]
    if operator_token:
        command.append("--operator-capability-stdin")
    try:
        result = subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            text=True,
            input=f"{operator_token}\n" if operator_token else None,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SupervisorError(f"cannot resolve ticket decision: {exc}") from exc
    if result.returncode != 0:
        raise SupervisorError(
            (result.stderr or result.stdout or "decision resolution failed").strip()
        )
    try:
        response = json.loads(result.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise SupervisorError("controller returned malformed decision output") from exc
    emit(response)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    start_parser = commands.add_parser("start", help="start the detached supervisor")
    start_parser.add_argument("--repo")
    start_parser.add_argument("--timeout", type=bounded_timeout, default=10.0)
    start_parser.set_defaults(func=start)

    status_parser = commands.add_parser(
        "status", help="read the durable supervisor state"
    )
    status_parser.add_argument("--repo")
    status_parser.set_defaults(func=status)

    for name in ("pause", "resume", "drain", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--repo")
        command.add_argument("--request-id")
        command.add_argument("--reason")
        command.add_argument("--timeout", type=bounded_timeout, default=5.0)
        command.set_defaults(func=control)

    decision = commands.add_parser(
        "resolve-decision",
        help="resolve one contract-classified parked ticket",
    )
    decision.add_argument("--repo")
    decision.add_argument("--sprint")
    decision.add_argument("--ticket", required=True)
    decision.add_argument("--decision-class", required=True)
    decision.add_argument("--decision-receipt", required=True)
    decision.add_argument("--reason", required=True)
    decision_capability = decision.add_mutually_exclusive_group()
    decision_capability.add_argument("--operator-capability")
    decision_capability.add_argument("--operator-capability-stdin", action="store_true")
    decision.set_defaults(func=resolve_decision_command)

    internal = commands.add_parser("_run", help=argparse.SUPPRESS)
    internal.add_argument("--repo", required=True)
    internal.add_argument("--handshake", type=Path, required=True)
    internal.set_defaults(
        func=lambda args: sys.exit(
            run_daemon(resolve_repository(args.repo), args.handshake)
        )
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except SupervisorError as exc:
        print(f"sprint-supervisor: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
