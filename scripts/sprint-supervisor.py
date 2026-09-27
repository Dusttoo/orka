#!/usr/bin/env python3
"""Run and control Orka's host-owned repository supervisor process.

This first Orka 2 runtime slice owns only the repository lease and supervisor
lifecycle. Planning and worker dispatch are deliberately left to later slices.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import re
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
    canonical_config_path,
    shared_repository_root,
    shared_runtime_path,
    working_repository_root,
)


class SupervisorError(RuntimeError):
    pass


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = PLUGIN_ROOT / "contracts/supervisor-lifecycle-v1.json"
RUNTIME_RELATIVE = Path(".orchestration/.supervisor")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_REASON = 2000
MAX_REQUEST = 16 * 1024
SUPERVISOR_STATES = {
    "starting",
    "active",
    "paused",
    "draining",
    "degraded",
    "takeover_pending",
    "stopped",
}


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


def state_snapshot(path: Path, repository: Path | None = None) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise SupervisorError("supervisor state must not be a symlink")
    value = load_json(path, "supervisor state")
    if (
        value.get("schema_version") != 1
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


def validate_request(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SupervisorError("control request must be an object")
    command = value.get("command")
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
    return result


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
    elif command == "resume":
        lifecycle.transition(
            state,
            "operator_resumed",
            {"operator_request_id": request_id, "blockers_checked": True},
        )
    elif command == "drain":
        lifecycle.transition(
            state, "drain_requested", {"operator_request_id": request_id}
        )
        lifecycle.transition(
            state,
            "drain_completed",
            {
                "active_job_count": 0,
                "queue_snapshot_digest": digest_bytes(b"[]"),
            },
        )
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
        ensure_private_directory(paths["directory"])
        contract_value, lifecycle, contract_digest = contract()
        del contract_value
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_descriptor = os.open(paths["lock"], lock_flags, 0o600)
    except (OSError, SupervisorError) as exc:
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

    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            write_handshake(
                handshake,
                {"status": "error", "error": "repository supervisor lease is held"},
            )
            return 2

        previous = state_snapshot(paths["state"], repository)
        previous_lease = (previous or {}).get("lease") or {}
        previous_clean = bool(
            previous
            and previous.get("lifecycle_state") == "stopped"
            and previous_lease.get("released_at")
            and previous_lease.get("release_count") == 1
        )
        if previous and not previous_clean:
            write_handshake(
                handshake,
                {
                    "status": "error",
                    "error": (
                        "prior supervisor did not stop cleanly; takeover authority is "
                        "not implemented in this runtime slice"
                    ),
                },
            )
            return 2
        generation = (
            int(((previous or {}).get("lease") or {}).get("generation") or 0) + 1
        )
        lease_id = str(uuid.uuid4())
        os.fchmod(lock_handle.fileno(), 0o600)
        identity = process_identity(os.getpid())
        prior_history = list((previous or {}).get("history") or [])[-255:]
        state = {
            "schema_version": 1,
            "contract_id": "orka.supervisor-lifecycle",
            "contract_schema_version": 1,
            "repository": str(repository),
            "lifecycle_state": "starting",
            "last_event": "supervisor_starting",
            "started_at": now(),
            "updated_at": now(),
            "stopped_at": "",
            "process": identity,
            "lease": {
                "id": lease_id,
                "generation": generation,
                "lock_path": str(paths["lock"]),
                "lock_device": os.fstat(lock_handle.fileno()).st_dev,
                "lock_inode": os.fstat(lock_handle.fileno()).st_ino,
                "acquired_at": now(),
                "released_at": "",
                "release_count": 0,
            },
            "control_socket": str(paths["socket"]),
            "history": prior_history
            + [
                {
                    "at": now(),
                    "event": "supervisor_starting",
                    "from": None,
                    "to": "starting",
                    "evidence": {"lease_id": lease_id, "generation": generation},
                }
            ],
            "requests": [],
        }
        persist_state()
        try:
            config_path = canonical_config_path(repository)
            if not config_path.is_file():
                raise SupervisorError(f"repository config is missing: {config_path}")
            config_digest = digest_bytes(config_path.read_bytes())
            runtime_fingerprint = digest_bytes(
                Path(__file__).read_bytes()
                + CONTRACT_PATH.read_bytes()
                + (PLUGIN_ROOT / "scripts/runtime_state.py").read_bytes()
                + (PLUGIN_ROOT / ".codex-plugin/plugin.json").read_bytes()
            )
            lifecycle.transition(
                state,
                "preflight_succeeded",
                {
                    "config_digest": config_digest,
                    "runtime_fingerprint": runtime_fingerprint,
                    "lease_id": lease_id,
                },
            )
            state["config_digest"] = config_digest
            state["runtime_fingerprint"] = runtime_fingerprint
            state["contract_digest"] = contract_digest
            persist_state()
        except (OSError, RuntimeStateError, SupervisorError) as exc:
            diagnostic = str(exc)
            lifecycle.transition(
                state,
                "preflight_failed",
                {
                    "failure_class": type(exc).__name__,
                    "diagnostic_digest": digest_bytes(diagnostic.encode("utf-8")),
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
        server.settimeout(0.25)
        write_handshake(handshake, response_for(state))

        signal_number = 0

        def stop_from_signal(received: int, _frame: Any) -> None:
            nonlocal signal_number
            signal_number = received

        signal.signal(signal.SIGTERM, stop_from_signal)
        signal.signal(signal.SIGINT, stop_from_signal)

        should_stop = False
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
            try:
                observed_state_digest = digest_bytes(paths["state"].read_bytes())
            except OSError:
                observed_state_digest = "missing"
            if observed_state_digest != expected_state_digest:
                lifecycle.transition(
                    state,
                    "durable_state_invalid",
                    {
                        "state_digest": observed_state_digest,
                        "validation_error": "durable supervisor state changed outside its owner",
                    },
                )
                persist_state()
                should_stop = True
                continue
            if not lease_is_current(lock_handle, paths["lock"]):
                lifecycle.transition(
                    state,
                    "lease_lost",
                    {
                        "lease_id": lease_id,
                        "observed_owner": "lease path no longer names the held inode",
                    },
                )
                persist_state()
                should_stop = True
                continue
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    request = read_request(connection)
                    response, request_stop = apply_control(state, lifecycle, request)
                    persist_state()
                    send_response(connection, response)
                    should_stop = should_stop or request_stop
                except SupervisorError as exc:
                    send_response(connection, {"status": "error", "error": str(exc)})
        return 0
    except Exception as exc:  # fail closed and leave a diagnostic handshake
        if state is not None and state.get("lifecycle_state") != "stopped":
            try:
                lifecycle.transition(
                    state,
                    "durable_state_invalid",
                    {
                        "state_digest": expected_state_digest or "unavailable",
                        "validation_error": type(exc).__name__,
                    },
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
    with open_private_file(paths["log"], append=True) as output, open(
        os.devnull, "rb"
    ) as input_stream:
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
