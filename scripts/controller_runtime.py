#!/usr/bin/env python3
"""Route cutover controller invocations through the repository supervisor.

The legacy controller intentionally remains file based.  After transactional
cutover, only the elected supervisor may materialize that file, and only in a
private temporary directory.  Successful mutations are committed back through
the event store's generation-fenced runtime-document API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from event_store import EventStoreError, TransactionalEventStore, canonical_json
from authoritative_supervisor_state import read_authoritative_state
from runtime_state import (
    repository_identity,
    repository_layout,
    runtime_cutover_marker,
)

DATABASE_NAME = "orka-state.sqlite3"
MAX_WIRE_BYTES = 1024 * 1024
COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
INTERNAL_CAPABILITY_ENV = "ORKA_CONTROLLER_MATERIALIZATION_CAPABILITY"


class ControllerRuntimeError(RuntimeError):
    """A cutover controller request could not be authenticated or committed."""


@dataclass(frozen=True)
class ControllerDocument:
    document_id: str
    generation: int
    payload: dict[str, Any]
    payload_digest: str


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _socket_path(repository: Path) -> Path:
    repository_hash = hashlib.sha256(
        str(repository.resolve()).encode("utf-8")
    ).hexdigest()[:24]
    return Path(f"/tmp/orka-supervisor-{os.getuid()}-{repository_hash}.sock")


def _database_path(repository: Path) -> Path:
    return repository_layout(repository).state_root / DATABASE_NAME


def _read_documents(
    repository: Path,
    store: TransactionalEventStore | None = None,
) -> tuple[dict[str, Any], dict[str, ControllerDocument]]:
    """Read the active cutover and controller generation set without writer authority."""

    identity = repository_identity(repository)
    marker = runtime_cutover_marker(repository)
    if marker is None:
        raise ControllerRuntimeError("transactional runtime cutover is not active")
    if store is not None:
        snapshot = store.runtime_snapshot(repository_id=identity["repository_uuid"])
        cutover_value = snapshot.get("cutover") or {}
        cutover = {
            "activation_id": cutover_value.get("activation_id"),
            "state": cutover_value.get("state"),
        }
        rows = [
            {
                "document_id": item["document_id"],
                "generation": item["generation"],
                "payload_json": canonical_json(item["payload"]),
                "payload_digest": item["payload_digest"],
            }
            for item in snapshot.get("documents") or []
            if item.get("document_type") == "controller"
        ]
    else:
        database_path = _database_path(repository)
        try:
            database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
            database.row_factory = sqlite3.Row
            cutover = database.execute(
                """
                SELECT activation_id, state FROM runtime_cutovers
                WHERE repository_id = ?
                """,
                (identity["repository_uuid"],),
            ).fetchone()
            rows = database.execute(
                """
                SELECT document_id, generation, payload_json, payload_digest
                FROM runtime_documents
                WHERE repository_id = ? AND document_type = 'controller'
                ORDER BY document_id
                """,
                (identity["repository_uuid"],),
            ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise ControllerRuntimeError(
                f"cannot read transactional controller state: {exc}"
            ) from exc
        finally:
            if "database" in locals():
                database.close()
    if cutover is None or str(cutover["state"]) != "active":
        raise ControllerRuntimeError(
            "transactional runtime cutover is not active in the event store"
        )
    if str(cutover["activation_id"]) != str(marker["activation_id"]):
        raise ControllerRuntimeError(
            "cutover marker and event store activation do not match"
        )
    documents: dict[str, ControllerDocument] = {}
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError as exc:
            raise ControllerRuntimeError(
                "transactional controller document is malformed"
            ) from exc
        if not isinstance(payload, dict) or _digest(payload) != str(
            row["payload_digest"]
        ):
            raise ControllerRuntimeError(
                "transactional controller document digest is invalid"
            )
        document_id = str(row["document_id"])
        documents[document_id] = ControllerDocument(
            document_id=document_id,
            generation=int(row["generation"]),
            payload=payload,
            payload_digest=str(row["payload_digest"]),
        )
    return {"identity": identity, "marker": marker}, documents


def _command_receipt(
    repository: Path,
    command_id: str,
    store: TransactionalEventStore | None = None,
) -> dict[str, Any] | None:
    if store is not None:
        receipt = store.event_receipt(f"controller:{command_id}")
        if receipt is None:
            return None
        return {
            "generation": receipt["aggregate_version"],
            "payload": receipt["payload"],
        }
    try:
        database = sqlite3.connect(
            f"file:{_database_path(repository)}?mode=ro", uri=True
        )
        row = database.execute(
            "SELECT aggregate_version, payload_json FROM events WHERE idempotency_key = ?",
            (f"controller:{command_id}",),
        ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise ControllerRuntimeError(
            f"cannot inspect controller command receipt: {exc}"
        ) from exc
    finally:
        if "database" in locals():
            database.close()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[1]))
    except json.JSONDecodeError as exc:
        raise ControllerRuntimeError("controller command receipt is malformed") from exc
    return {"generation": int(row[0]), "payload": payload}


def direct_request(repository: Path, argv: Sequence[str]) -> int:
    """Send one direct controller invocation to the active supervisor."""

    command_id = os.environ.get("ORKA_CONTROLLER_COMMAND_ID") or str(uuid.uuid4())
    if not COMMAND_ID.fullmatch(command_id):
        raise ControllerRuntimeError(
            "controller command identity has an invalid format"
        )
    state = read_authoritative_state(repository)
    if state is None:
        raise ControllerRuntimeError(
            "cannot authenticate the active repository supervisor"
        )
    lease = state.get("lease") or {}
    fence = f"{lease.get('id', '')}:{lease.get('generation', '')}"
    if not lease.get("id") or not isinstance(lease.get("generation"), int):
        raise ControllerRuntimeError("active supervisor has no valid lease fence")
    request = supervisor_request(repository, argv, fence, command_id=command_id)
    encoded = (canonical_json(request) + "\n").encode("utf-8")
    if len(encoded) > MAX_WIRE_BYTES:
        raise ControllerRuntimeError(
            "controller request exceeds the supervisor wire limit"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(300)
        try:
            connection.connect(str(_socket_path(repository)))
            connection.sendall(encoded)
            response = connection.makefile("rb").readline(MAX_WIRE_BYTES + 1)
        except (OSError, TimeoutError) as exc:
            raise ControllerRuntimeError(
                f"supervisor controller request failed: {exc}"
            ) from exc
    if not response or len(response) > MAX_WIRE_BYTES:
        raise ControllerRuntimeError(
            "supervisor returned an invalid controller response"
        )
    try:
        result = json.loads(response)
    except json.JSONDecodeError as exc:
        raise ControllerRuntimeError(
            "supervisor returned malformed controller output"
        ) from exc
    if not isinstance(result, dict) or result.get("status") != "controller-result":
        raise ControllerRuntimeError(
            str((result or {}).get("error") or "controller request failed")
        )
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return int(result.get("returncode") or 0)


def supervisor_request(
    repository: Path,
    argv: Sequence[str],
    supervisor_fence: str,
    *,
    command_id: str | None = None,
    store: TransactionalEventStore | None = None,
) -> dict[str, Any]:
    """Bind one command to the repository and current controller generation set."""

    binding, documents = _read_documents(repository, store)
    identity = command_id or str(uuid.uuid4())
    if not COMMAND_ID.fullmatch(identity):
        raise ControllerRuntimeError(
            "controller command identity has an invalid format"
        )
    return {
        "command": "controller",
        "command_id": identity,
        "repository_id": binding["identity"]["repository_uuid"],
        "supervisor_fence": supervisor_fence,
        "activation_id": binding["marker"]["activation_id"],
        "expected_controller_generations": {
            key: item.generation for key, item in documents.items()
        },
        "argv": list(argv),
    }


def validate_internal_capability(state_directory: Path) -> None:
    """Authenticate one private supervisor materialization for controller main()."""

    capability = os.environ.get(INTERNAL_CAPABILITY_ENV, "")
    if not capability:
        raise ControllerRuntimeError("supervisor controller capability is missing")
    path = Path(capability)
    try:
        mode = path.lstat().st_mode
        expected = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ControllerRuntimeError(
            "supervisor controller capability is unreadable"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or mode & 0o077 or not expected:
        raise ControllerRuntimeError("supervisor controller capability is not private")
    try:
        state_directory.resolve().relative_to(path.parent.resolve())
    except ValueError as exc:
        raise ControllerRuntimeError(
            "controller materialization escaped its private directory"
        ) from exc
    if os.environ.get("ORKA_CONTROLLER_COMMAND_ID") != expected:
        raise ControllerRuntimeError(
            "supervisor controller capability identity changed"
        )


def execute_request(
    repository: Path,
    request: Mapping[str, Any],
    *,
    supervisor_fence: str,
    writer_identity: str,
    private_root: Path,
    controller_path: Path,
    store: TransactionalEventStore | None = None,
) -> dict[str, Any]:
    """Execute and transactionally commit one authenticated controller command."""

    command_id = str(request.get("command_id") or "")
    if not COMMAND_ID.fullmatch(command_id):
        raise ControllerRuntimeError(
            "controller command identity has an invalid format"
        )
    identity = repository_identity(repository)
    marker = runtime_cutover_marker(repository)
    if marker is None:
        raise ControllerRuntimeError("transactional runtime cutover is not active")
    if request.get("repository_id") != identity["repository_uuid"]:
        raise ControllerRuntimeError("controller request repository identity is stale")
    if request.get("supervisor_fence") != supervisor_fence:
        raise ControllerRuntimeError("controller request supervisor fence is stale")
    if request.get("activation_id") != marker["activation_id"]:
        raise ControllerRuntimeError("controller request cutover activation is stale")
    argv = request.get("argv")
    generations = request.get("expected_controller_generations")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or "\x00" in item for item in argv)
        or not isinstance(generations, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, int) or value < 0
            for key, value in generations.items()
        )
    ):
        raise ControllerRuntimeError("controller request payload is invalid")
    if any(
        item == "--state-dir"
        or item.startswith("--state-dir=")
        or item == "--config"
        or item.startswith("--config=")
        for item in argv
    ):
        raise ControllerRuntimeError(
            "cutover controller commands cannot override policy or materialized state paths"
        )

    request_digest = _digest(
        {
            "activation_id": request["activation_id"],
            "argv": argv,
            "expected_controller_generations": generations,
            "repository_id": request["repository_id"],
            "supervisor_fence": request["supervisor_fence"],
        }
    )
    receipt = _command_receipt(repository, command_id, store)
    if receipt is not None:
        if (receipt["payload"] or {}).get("operation_digest") != request_digest:
            raise ControllerRuntimeError(
                "controller command identity was reused with different material"
            )
        replay = {
            "command_id": command_id,
            "generation": receipt["generation"],
            "replayed": True,
        }
        return {
            "status": "controller-result",
            "returncode": 0,
            "stdout": canonical_json(replay) + "\n",
            "stderr": "",
            "commits": [replay],
        }

    binding, documents = _read_documents(repository, store)
    observed = {key: item.generation for key, item in documents.items()}
    if generations != observed:
        raise ControllerRuntimeError(
            f"controller generation is stale: expected {generations!r}, current {observed!r}"
        )
    private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="controller-", dir=private_root) as raw:
        workspace = Path(raw)
        workspace.chmod(0o700)
        state_directory = workspace / "state"
        state_directory.mkdir(mode=0o700)
        before: dict[str, str] = {}
        for document in documents.values():
            sprint = document.payload.get("sprint") or {}
            sprint_id = str(sprint.get("id") or document.document_id)
            slug = (
                re.sub(r"[^A-Za-z0-9._-]+", "-", sprint_id).strip("-.")[:48] or "sprint"
            )
            name = f"{slug}-{hashlib.sha256(sprint_id.encode()).hexdigest()[:10]}.json"
            path = state_directory / name
            encoded = json.dumps(document.payload, sort_keys=True, indent=2) + "\n"
            path.write_text(encoded, encoding="utf-8")
            path.chmod(0o600)
            before[document.document_id] = _digest(document.payload)
        capability = workspace / "capability"
        capability.write_text(command_id + "\n", encoding="utf-8")
        capability.chmod(0o600)
        environment = dict(os.environ)
        environment[INTERNAL_CAPABILITY_ENV] = str(capability)
        environment["ORKA_CONTROLLER_COMMAND_ID"] = command_id
        result = subprocess.run(
            [
                sys.executable,
                str(controller_path),
                "--state-dir",
                str(state_directory),
                *argv,
            ],
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=300,
            env=environment,
        )
        if result.returncode != 0:
            return {
                "status": "controller-result",
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "commits": [],
            }
        changed: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(state_directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ControllerRuntimeError(
                    "controller emitted a malformed checkpoint"
                ) from exc
            if not isinstance(payload, dict) or not isinstance(
                payload.get("sprint"), dict
            ):
                raise ControllerRuntimeError("controller emitted an invalid checkpoint")
            document_id = str(payload["sprint"].get("id") or "").strip()
            if not document_id:
                raise ControllerRuntimeError(
                    "controller checkpoint has no sprint identity"
                )
            if _digest(payload) != before.get(document_id):
                changed.append((document_id, payload))
        if len(changed) > 1:
            raise ControllerRuntimeError(
                "one controller command changed multiple sprint documents"
            )
        commits: list[dict[str, Any]] = []
        if changed:
            document_id, payload = changed[0]
        else:
            sprint_value = ""
            for index, item in enumerate(argv[:-1]):
                if item == "--sprint":
                    sprint_value = str(argv[index + 1]).strip()
                    break
            if sprint_value in documents:
                document_id = sprint_value
                payload = documents[document_id].payload
            elif len(documents) == 1:
                document_id, document = next(iter(documents.items()))
                payload = document.payload
            else:
                raise ControllerRuntimeError(
                    "controller command did not identify exactly one runtime document"
                )
        if changed or documents:
            expected_generation = int(generations.get(document_id, 0))
            database_path = _database_path(repository)
            try:
                owned_store: TransactionalEventStore | None = None
                active_store = store
                if active_store is None:
                    owned_store = TransactionalEventStore(
                        database_path, writer_identity=writer_identity
                    )
                    active_store = owned_store
                try:
                    written = active_store.write_runtime_document(
                        repository_id=identity["repository_uuid"],
                        activation_id=marker["activation_id"],
                        document_type="controller",
                        document_id=document_id,
                        expected_generation=expected_generation,
                        payload=payload,
                        supervisor_fence=supervisor_fence,
                        idempotency_key=f"controller:{command_id}",
                        writer_identity=writer_identity,
                        operation_digest=request_digest,
                    )
                finally:
                    if owned_store is not None:
                        owned_store.close()
            except EventStoreError as exc:
                raise ControllerRuntimeError(str(exc)) from exc
            commits.append(
                {
                    "document_id": document_id,
                    "generation": written.generation,
                    "replayed": written.replayed,
                    "payload_digest": written.payload_digest,
                }
            )
        return {
            "status": "controller-result",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "commits": commits,
        }
