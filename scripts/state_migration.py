#!/usr/bin/env python3
"""Offline legacy-state import and deterministic Orka event-store export."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from event_store import (
    EventStoreError,
    RepositoryBinding,
    TransactionalEventStore,
    canonical_json,
)
from runtime_state import (
    CUTOVER_FILE,
    RuntimeStateError,
    legacy_state_inventory,
    repository_identity,
    repository_initialization_authority,
    repository_layout,
    resolve_canonical_policy,
    runtime_cutover_marker,
)
from version_policy import manifest_version, release_version


DATABASE_NAME = "orka-state.sqlite3"
EXPORT_VERSION = 1
MAX_SOURCE_BYTES = 16 * 1024 * 1024
JSON_SUFFIXES = {".json", ".jsonl"}
SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~-]+|sk-(?:ant-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{12,})"
)
SENSITIVE_KEYS = {
    "api_key",
    "access_token",
    "authorization",
    "credential",
    "credentials",
    "input",
    "messages",
    "password",
    "prompt",
    "prompt_body",
    "request_body",
    "response_body",
    "secret",
    "system_prompt",
    "token",
}


class MigrationError(RuntimeError):
    pass


def _import_snapshot(
    store: TransactionalEventStore,
    inventory: Mapping[str, Any],
    writer: str,
) -> Any:
    try:
        return store.import_legacy_snapshot(
            repository_id=inventory["repository_id"],
            receipt_id=inventory["receipt_id"],
            manifest_digest=inventory["manifest_digest"],
            normalized_export_digest=inventory["normalized_export_digest"],
            sources=inventory["sources"],
            writer_identity=writer,
        )
    except EventStoreError as exc:
        raise MigrationError(str(exc)) from exc


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key in sorted(value, key=str):
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if normalized in SENSITIVE_KEYS or normalized.endswith(
                ("_secret", "_password", "_api_key")
            ):
                continue
            result[key] = _sanitize(value[raw_key])
        return result
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and SECRET_VALUE.search(value):
        return SECRET_VALUE.sub("[REDACTED]", value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise MigrationError(f"legacy state contains unsupported value type: {type(value).__name__}")


def _category(relative: Path) -> str | None:
    parts = set(relative.parts)
    name = relative.name.lower()
    if ".sprint-state" in parts:
        return "controller"
    if ".supervisor" in parts and name in {"state.json", "state.sha256.json"}:
        return "supervisor"
    if ".review-ledger" in parts:
        return "review_ledger"
    if ".api-usage" in parts or "usage" in name:
        return "usage"
    if ".api-runs" in parts or "receipt" in name:
        return "external_receipt"
    if ".provider-health" in parts or "provider-health" in name:
        return "provider_health"
    if ".recovery" in parts or "recovery" in name or "tombstone" in name:
        return "recovery"
    if ".decisions" in parts or "decision" in name:
        return "decision"
    return None


def _parse_source(path: Path, raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"legacy source is not UTF-8: {path}") from exc
    try:
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"legacy source is malformed JSON: {path}: {exc}") from exc


def _registered_worktree_roots(repository: Path) -> set[Path]:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MigrationError("cannot enumerate registered Git worktrees") from exc
    return {
        Path(line[9:]).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    }


def _prove_candidate(
    candidate: Path,
    *,
    worktrees: set[Path],
    identity: Mapping[str, Any],
) -> str:
    for root in worktrees:
        if candidate == (root / ".orchestration").resolve():
            return f"registered-worktree:{digest_bytes(str(root).encode())[:16]}"
    proof_path = candidate / "repository-binding.json"
    if not proof_path.is_file() or proof_path.is_symlink():
        raise MigrationError(
            f"legacy state ownership is ambiguous and lacks repository-binding.json: {candidate}"
        )
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"legacy ownership proof is malformed: {proof_path}") from exc
    required = {
        "repository_uuid": identity["repository_uuid"],
        "common_directory": identity["common_directory"],
        "object_directory_identity": identity["object_directory_identity"],
    }
    if not isinstance(proof, dict) or any(proof.get(key) != value for key, value in required.items()):
        raise MigrationError(f"legacy ownership proof does not match repository identity: {proof_path}")
    return f"identity-proof:{digest_bytes(proof_path.read_bytes())[:16]}"


def inventory_legacy_state(repository: Path) -> dict[str, Any]:
    try:
        identity = repository_identity(repository)
        candidates = legacy_state_inventory(repository)["candidates"]
    except RuntimeStateError as exc:
        raise MigrationError(str(exc)) from exc
    worktrees = _registered_worktree_roots(repository)
    sources: list[dict[str, Any]] = []
    for candidate_raw in sorted(set(candidates)):
        candidate = Path(candidate_raw).resolve()
        proof = _prove_candidate(candidate, worktrees=worktrees, identity=identity)
        for path in sorted(candidate.rglob("*")):
            if path.suffix not in JSON_SUFFIXES:
                continue
            relative = path.relative_to(candidate)
            category = _category(relative)
            if category is None or relative.name == "repository-binding.json":
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise MigrationError(f"legacy source must be a regular file: {path}")
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise MigrationError(f"legacy source exceeds {MAX_SOURCE_BYTES} bytes: {path}")
            raw = path.read_bytes()
            payload = _sanitize(_parse_source(path, raw))
            source_path = f"{proof}/{relative.as_posix()}"
            sources.append(
                {
                    "category": category,
                    "normalized_digest": digest_json(payload),
                    "payload": payload,
                    "record_key": source_path,
                    "source_digest": digest_bytes(raw),
                    "source_kind": "jsonl" if path.suffix == ".jsonl" else "json",
                    "source_path": source_path,
                }
            )
    if not sources:
        raise MigrationError("no recognized repository-owned legacy state was found")
    sources.sort(key=lambda item: item["source_path"])
    metadata = {
        "repository_id": identity["repository_uuid"],
        "sources": [
            {key: source[key] for key in (
                "category",
                "normalized_digest",
                "record_key",
                "source_digest",
                "source_kind",
                "source_path",
            )}
            for source in sources
        ],
    }
    legacy_snapshot = {
        "repository_id": identity["repository_uuid"],
        "sources": sources,
    }
    return {
        "inventory_version": 1,
        "repository_id": identity["repository_uuid"],
        "manifest_digest": digest_json(metadata),
        "normalized_export_digest": digest_json(legacy_snapshot),
        "receipt_id": f"legacy-{digest_json(metadata)[:32]}",
        "sources": sources,
    }


@contextlib.contextmanager
def exclusive_migration_authority(repository: Path) -> Iterator[None]:
    layout = repository_layout(repository)
    state_root = layout.state_root
    migration_lock = state_root / "legacy-import.lock"
    descriptor = os.open(
        migration_lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    handle = os.fdopen(descriptor, "a+")
    supervisor_handle: Any = None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        supervisor_dir = layout.common_directory.parent / ".orchestration/.supervisor"
        if layout.bare:
            supervisor_dir = layout.common_directory / ".orchestration/.supervisor"
        state_path = supervisor_dir / "state.json"
        lease_path = supervisor_dir / "lease.lock"
        if state_path.exists() and not lease_path.exists():
            raise MigrationError("supervisor state exists without its repository lease")
        if lease_path.exists():
            supervisor_handle = lease_path.open("a+")
            try:
                fcntl.flock(
                    supervisor_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError as exc:
                raise MigrationError("repository supervisor lease is held") from exc
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise MigrationError("supervisor state cannot prove a clean stop") from exc
                lease = state.get("lease") if isinstance(state, dict) else None
                if (
                    state.get("lifecycle_state") != "stopped"
                    or not isinstance(lease, dict)
                    or not lease.get("released_at")
                    or lease.get("release_count") != 1
                ):
                    raise MigrationError("supervisor is not cleanly stopped")
        with repository_initialization_authority(repository):
            yield
    except BlockingIOError as exc:
        raise MigrationError("another legacy migration owns the repository") from exc
    finally:
        if supervisor_handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(supervisor_handle.fileno(), fcntl.LOCK_UN)
            supervisor_handle.close()
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _binding(repository: Path) -> RepositoryBinding:
    identity = repository_identity(repository)
    policy = resolve_canonical_policy(repository)
    return RepositoryBinding(
        repository_id=str(identity["repository_uuid"]),
        common_directory=str(identity["common_directory"]),
        object_directory_id=str(identity["object_directory_identity"]),
        policy_ref=policy.policy_ref,
        policy_path=policy.policy_path,
        policy_commit=policy.commit,
        policy_blob=policy.blob,
        policy_digest=policy.digest,
        created_at=str(identity["created_at"]),
    )


def _rows(database: sqlite3.Connection, table: str, order: str) -> list[dict[str, Any]]:
    cursor = database.execute(f"SELECT * FROM {table} ORDER BY {order}")
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def export_state(database_path: Path) -> dict[str, Any]:
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        database = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot open event store for export: {exc}") from exc
    try:
        database.execute("PRAGMA foreign_keys = ON")
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("event store failed integrity_check")
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("event store failed foreign_key_check")
        schema_version = int(
            database.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        sections = {
            "repositories": _rows(database, "repositories", "repository_id"),
            "events": _rows(database, "events", "sequence"),
            "jobs": _rows(database, "jobs", "repository_id, sprint_id, ticket_id"),
            "attempts": _rows(database, "attempts", "attempt_token"),
            "resource_claims": _rows(database, "resource_claims", "claim_key"),
            "timers": _rows(database, "timers", "timer_id"),
            "external_operations": _rows(database, "external_operations", "operation_key"),
            "migration_receipts": _rows(database, "migration_receipts", "receipt_id"),
            "migration_sources": _rows(database, "migration_sources", "receipt_id, source_path"),
            "legacy_records": _rows(database, "legacy_records", "repository_id, category, record_key"),
            "runtime_cutovers": _rows(database, "runtime_cutovers", "repository_id"),
            "runtime_documents": _rows(
                database,
                "runtime_documents",
                "repository_id, document_type, document_id",
            ),
        }
    except (sqlite3.Error, TypeError, IndexError) as exc:
        raise MigrationError(f"event store export failed: {exc}") from exc
    finally:
        database.close()
    for event in sections["events"]:
        event["payload_json"] = _sanitize(json.loads(event["payload_json"]))
    for operation in sections["external_operations"]:
        if operation["receipt_json"] is not None:
            operation["receipt_json"] = _sanitize(json.loads(operation["receipt_json"]))
    records_by_path: dict[str, Any] = {}
    for record in sections["legacy_records"]:
        record["payload_json"] = _sanitize(json.loads(record["payload_json"]))
        records_by_path[record["source_path"]] = record["payload_json"]
    for document in sections["runtime_documents"]:
        document["payload_json"] = _sanitize(json.loads(document["payload_json"]))
    sources = []
    for source in sections["migration_sources"]:
        record = next(
            item
            for item in sections["legacy_records"]
            if item["source_path"] == source["source_path"]
        )
        sources.append(
            {
                "category": record["category"],
                "normalized_digest": source["normalized_digest"],
                "payload": records_by_path[source["source_path"]],
                "record_key": record["record_key"],
                "source_digest": source["source_digest"],
                "source_kind": source["source_kind"],
                "source_path": source["source_path"],
            }
        )
    repository_id = sections["repositories"][0]["repository_id"]
    legacy_snapshot = {"repository_id": repository_id, "sources": sources}
    payload = {
        "export_version": EXPORT_VERSION,
        "schema_version": schema_version,
        "repository_id": repository_id,
        "legacy_snapshot": legacy_snapshot,
        "sections": _sanitize(sections),
    }
    digests = {
        name: digest_json(value)
        for name, value in sorted(payload["sections"].items())
    }
    digests["legacy_snapshot"] = digest_json(legacy_snapshot)
    digests["export"] = digest_json(payload)
    return {**payload, "digests": digests}


def export_bytes(database_path: Path) -> bytes:
    return (canonical_json(export_state(database_path)) + "\n").encode("utf-8")


def import_legacy_state(
    repository: Path,
    *,
    crash_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    hook = crash_hook or (lambda _boundary: None)
    layout = repository_layout(repository)
    destination = layout.state_root / DATABASE_NAME
    with exclusive_migration_authority(repository):
        inventory = inventory_legacy_state(repository)
        hook("after_inventory")
        if destination.exists():
            writer = f"legacy-import:{inventory['receipt_id']}"
            with TransactionalEventStore(
                destination,
                writer_identity=writer,
            ) as store:
                result = _import_snapshot(store, inventory, writer)
            if not result.replayed:
                raise MigrationError("existing live database accepted a new legacy import")
            exported = export_state(destination)
            if exported["digests"]["legacy_snapshot"] != inventory["normalized_export_digest"]:
                raise MigrationError("existing migration shadow export no longer matches legacy state")
            return {**asdict(result), "database_path": str(destination)}

        temporary = layout.state_root / f".{DATABASE_NAME}.{inventory['receipt_id']}.tmp"
        temporary_lock = temporary.with_name(temporary.name + ".writer.lock")
        if temporary.exists() or temporary.is_symlink():
            mode = temporary.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise MigrationError("stale migration temporary is not a regular file")
            temporary.unlink()
        temporary_lock.unlink(missing_ok=True)
        hook("before_temporary_database")
        try:
            writer = f"legacy-import:{inventory['receipt_id']}"
            with TransactionalEventStore(temporary, writer_identity=writer) as store:
                hook("after_temporary_database")
                store.bind_repository(_binding(repository), writer_identity=writer)
                result = _import_snapshot(store, inventory, writer)
                hook("after_import_transaction")
                checks = store.check_integrity()
                if checks != {"integrity": "ok", "foreign_key_violations": []}:
                    raise MigrationError(f"temporary event store failed integrity checks: {checks}")
            hook("after_integrity_check")
            exported = export_state(temporary)
            if exported["digests"]["legacy_snapshot"] != inventory["normalized_export_digest"]:
                raise MigrationError("shadow export does not match normalized legacy state")
            hook("before_install")
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, destination)
            directory = os.open(layout.state_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            hook("after_install")
        finally:
            temporary.unlink(missing_ok=True)
            temporary_lock.unlink(missing_ok=True)
        return {**asdict(result), "database_path": str(destination)}


def _cutover_seed_documents(exported: Mapping[str, Any]) -> list[dict[str, Any]]:
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    sources = (exported.get("legacy_snapshot") or {}).get("sources") or []
    for source in sources:
        payload = source.get("payload")
        if not isinstance(payload, dict):
            continue
        identity: tuple[str, str] | None = None
        if (
            source.get("category") == "controller"
            and isinstance(payload.get("sprint"), dict)
            and isinstance(payload.get("tickets"), dict)
            and str(payload["sprint"].get("id") or "").strip()
        ):
            identity = ("controller", str(payload["sprint"]["id"]).strip())
        elif (
            source.get("category") == "supervisor"
            and str(source.get("source_path") or "").endswith("/.supervisor/state.json")
            and str(payload.get("lifecycle_state") or "").strip()
        ):
            identity = ("supervisor", "primary")
        if identity is None:
            continue
        candidate = {
            "document_type": identity[0],
            "document_id": identity[1],
            "payload": payload,
            "payload_digest": digest_json(payload),
        }
        existing = documents.get(identity)
        if existing is not None and existing["payload_digest"] != candidate["payload_digest"]:
            raise MigrationError(
                "legacy state contains conflicting authoritative runtime documents: "
                f"{identity[0]}:{identity[1]}"
            )
        documents[identity] = candidate
    if not documents:
        raise MigrationError(
            "legacy import contains no controller or supervisor checkpoint to seed"
        )
    return [documents[key] for key in sorted(documents)]


def _write_cutover_marker(path: Path, marker: Mapping[str, Any]) -> None:
    encoded = (canonical_json(marker) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise MigrationError("short write while persisting cutover marker")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def activate_runtime_cutover(
    repository: Path,
    *,
    crash_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Activate the explicit no-dual-writer boundary after a shadow import."""

    hook = crash_hook or (lambda _boundary: None)
    layout = repository_layout(repository)
    database = layout.state_root / DATABASE_NAME
    marker_path = layout.state_root / CUTOVER_FILE
    with exclusive_migration_authority(repository):
        if not database.is_file() or database.is_symlink():
            raise MigrationError("transactional event store must be imported before cutover")
        inventory = inventory_legacy_state(repository)
        writer = f"cutover:{inventory['receipt_id']}"
        # Opening with the offline writer applies only reviewed schema migrations.
        with TransactionalEventStore(database, writer_identity=writer):
            pass
        exported = export_state(database)
        if exported["digests"]["legacy_snapshot"] != inventory["normalized_export_digest"]:
            raise MigrationError("cutover shadow export no longer matches legacy state")
        current_version = manifest_version(Path(__file__).resolve().parent.parent)
        release_version(current_version)
        existing_marker = runtime_cutover_marker(repository)
        if existing_marker is None:
            material = {
                "activated_at": datetime.now(timezone.utc).isoformat(),
                "legacy_snapshot_digest": inventory["normalized_export_digest"],
                "minimum_orka_version": current_version,
                "repository_id": inventory["repository_id"],
                "schema_version": 1,
            }
            marker = {
                **material,
                "activation_id": f"cutover-{digest_json(material)}",
            }
            _write_cutover_marker(marker_path, marker)
        else:
            marker = existing_marker
            if (
                marker["legacy_snapshot_digest"]
                != inventory["normalized_export_digest"]
                or marker["minimum_orka_version"] != current_version
            ):
                raise MigrationError("existing cutover marker conflicts with current state")
        hook("after_marker_install")
        marker_digest = digest_json(marker)
        writer = f"cutover:{marker['activation_id']}"
        with TransactionalEventStore(database, writer_identity=writer) as store:
            try:
                result = store.activate_runtime_cutover(
                    repository_id=inventory["repository_id"],
                    activation_id=marker["activation_id"],
                    marker_digest=marker_digest,
                    minimum_version=current_version,
                    legacy_snapshot_digest=inventory["normalized_export_digest"],
                    seed_documents=_cutover_seed_documents(exported),
                    idempotency_key=f"activate:{marker['activation_id']}",
                    writer_identity=writer,
                    occurred_at=marker["activated_at"],
                )
            except EventStoreError as exc:
                raise MigrationError(str(exc)) from exc
        hook("after_store_activation")
        return {
            **asdict(result),
            "database_path": str(database),
            "marker_path": str(marker_path),
            "marker_digest": marker_digest,
            "minimum_orka_version": current_version,
        }


def rollback_runtime_cutover(
    repository: Path,
    *,
    reason: str,
    crash_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Rollback activation only before the first authoritative runtime write."""

    hook = crash_hook or (lambda _boundary: None)
    layout = repository_layout(repository)
    database = layout.state_root / DATABASE_NAME
    marker_path = layout.state_root / CUTOVER_FILE
    with exclusive_migration_authority(repository):
        marker = runtime_cutover_marker(repository)
        if marker is None:
            if database.is_file():
                writer = "cutover-rollback:status-replay"
                with TransactionalEventStore(database, writer_identity=writer) as store:
                    snapshot = store.runtime_snapshot(
                        repository_id=repository_identity(repository)[
                            "repository_uuid"
                        ]
                    )
                    cutover = snapshot.get("cutover") or {}
                    if cutover.get("state") == "rolled_back":
                        try:
                            result = store.rollback_runtime_cutover(
                                repository_id=cutover["repository_id"],
                                activation_id=cutover["activation_id"],
                                marker_digest=cutover["marker_digest"],
                                reason=reason,
                                idempotency_key=(
                                    f"rollback:{cutover['activation_id']}:"
                                    f"{digest_json(reason)}"
                                ),
                                writer_identity=writer,
                            )
                        except EventStoreError as exc:
                            raise MigrationError(str(exc)) from exc
                        return {**asdict(result), "marker_path": str(marker_path)}
            raise MigrationError("runtime cutover marker is not active")
        marker_digest = digest_json(marker)
        writer = f"cutover-rollback:{marker['activation_id']}"
        with TransactionalEventStore(database, writer_identity=writer) as store:
            try:
                result = store.rollback_runtime_cutover(
                    repository_id=marker["repository_id"],
                    activation_id=marker["activation_id"],
                    marker_digest=marker_digest,
                    reason=reason,
                    idempotency_key=f"rollback:{marker['activation_id']}:{digest_json(reason)}",
                    writer_identity=writer,
                )
            except EventStoreError as exc:
                raise MigrationError(str(exc)) from exc
        hook("after_store_rollback")
        marker_path.unlink(missing_ok=True)
        directory = os.open(layout.state_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        hook("after_marker_removal")
        return {**asdict(result), "marker_path": str(marker_path)}


def runtime_cutover_status(repository: Path) -> dict[str, Any]:
    layout = repository_layout(repository)
    marker = runtime_cutover_marker(repository)
    database = layout.state_root / DATABASE_NAME
    if not database.is_file():
        return {"active": False, "marker": marker, "store_cutover": None}
    exported = export_state(database)
    cutovers = exported["sections"].get("runtime_cutovers") or []
    return {
        "active": bool(marker and cutovers and cutovers[0].get("state") == "active"),
        "marker": marker,
        "store_cutover": cutovers[0] if cutovers else None,
    }


def _repository(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--repo", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--repo", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--repo", required=True)
    export_parser.add_argument("--output")
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--repo", required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--repo", required=True)
    rollback_parser.add_argument("--reason", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        repository = _repository(args.repo)
        if args.command == "inventory":
            value = inventory_legacy_state(repository)
            source_count = len(value["sources"])
            value = {key: item for key, item in value.items() if key != "sources"}
            value["source_count"] = source_count
            print(canonical_json(value))
        elif args.command == "import":
            print(canonical_json(import_legacy_state(repository)))
        elif args.command == "export":
            database = repository_layout(repository).state_root / DATABASE_NAME
            encoded = export_bytes(database)
            if args.output:
                output = Path(args.output).expanduser().resolve()
                output.write_bytes(encoded)
            else:
                sys.stdout.buffer.write(encoded)
        elif args.command == "activate":
            print(canonical_json(activate_runtime_cutover(repository)))
        elif args.command == "rollback":
            print(
                canonical_json(
                    rollback_runtime_cutover(repository, reason=args.reason)
                )
            )
        else:
            print(canonical_json(runtime_cutover_status(repository)))
    except (EventStoreError, MigrationError, RuntimeStateError, sqlite3.Error) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
