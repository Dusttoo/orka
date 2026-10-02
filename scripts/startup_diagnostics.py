#!/usr/bin/env python3
"""Read-only event-store startup diagnostics for supervisor admission.

The diagnostic deliberately opens SQLite in read-only mode, emits only stable
reason codes and non-sensitive digests, and never repairs a failed invariant.
The deeper ``PRAGMA integrity_check`` remains an explicit operator diagnostic;
startup uses bounded ``quick_check(1)`` instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from event_store import (
    SCHEMA_VERSION,
    canonical_json,
    migration_specifications,
)
from runtime_state import (
    CUTOVER_FILE,
    IDENTITY_FILE,
    PolicySnapshot,
    RuntimeStateError,
    repository_identity,
    repository_layout,
    resolve_canonical_policy,
    runtime_cutover_marker,
)
from version_policy import manifest_version, release_version

CONTRACT_ID = "orka.startup-diagnostic"
CONTRACT_SCHEMA_VERSION = 1
CHECK_ORDER = (
    "runtime_permissions",
    "quick_check",
    "foreign_keys",
    "schema",
    "migration_ledger",
    "repository_identity",
    "canonical_policy",
    "cutover",
    "minimum_version",
)
DATABASE_NAME = "orka-state.sqlite3"
MIGRATIONS = migration_specifications()
NEXT_ACTIONS = {
    "runtime_path_unavailable": "restore the private repository runtime directory from trusted storage",
    "runtime_path_permissions_invalid": "restore owner-only permissions on repository runtime files",
    "database_unavailable": "restore the transactional database from trusted storage",
    "database_permissions_invalid": "restore owner-only permissions on the transactional database and sidecars",
    "quick_check_failed": "run the explicit full integrity diagnostic and restore from a verified backup",
    "foreign_key_violation": "preserve the store and investigate the reported relational corruption",
    "schema_mismatch": "preserve the store and run the reviewed offline migration or restore procedure",
    "migration_ledger_mismatch": "restore the reviewed migration ledger; do not rewrite it in place",
    "repository_identity_invalid": "restore the repository identity marker from trusted storage",
    "repository_binding_mismatch": "use the event store bound to this repository identity",
    "canonical_policy_invalid": "restore the configured canonical policy ref and blob",
    "canonical_policy_binding_mismatch": "re-run the reviewed offline policy migration procedure",
    "cutover_marker_invalid": "restore the authenticated cutover marker from trusted storage",
    "cutover_binding_mismatch": "reconcile the cutover marker and store through the offline cutover procedure",
    "minimum_version_invalid": "install a valid released Orka runtime before retrying",
    "minimum_version_not_met": "upgrade Orka to the cutover minimum version or newer",
}


@dataclass(frozen=True)
class DatabaseIdentity:
    """Opaque local-file identity retained outside the sanitized receipt."""

    device: int
    inode: int


@dataclass(frozen=True)
class StartupAdmission:
    """Checked capabilities the elected supervisor may consume exactly once."""

    receipt: dict[str, Any]
    policy: PolicySnapshot | None = None
    database_path: Path | None = None
    database_identity: DatabaseIdentity | None = None
    repository: Path | None = None
    plugin_root: Path | None = None
    identity_document: str = ""
    cutover_document: str = ""

    def validate_writer_connection(
        self, database: sqlite3.Connection, database_path: Path
    ) -> None:
        """Revalidate retained authority on the writer-owned connection."""

        specifications = (
            MIGRATIONS[:-1] if self.receipt.get("upgrade_required") else MIGRATIONS
        )
        _validate_writer_admission(
            self, database, database_path, specifications=specifications
        )

    def validate_final_writer_connection(
        self, database: sqlite3.Connection, database_path: Path
    ) -> None:
        """Require complete current authority after any registered migration."""

        _validate_writer_admission(
            self, database, database_path, specifications=MIGRATIONS
        )

    def finalized_receipt(self) -> dict[str, Any]:
        """Return the admission receipt after writer-owned upgrade validation."""

        if not self.receipt.get("upgrade_required"):
            return dict(self.receipt)
        checks = {item["id"]: dict(item) for item in self.receipt.get("checks") or []}
        for check_id in ("schema", "migration_ledger"):
            checks[check_id] = _check(
                check_id,
                "pass",
                "ok",
                {"schema_version": SCHEMA_VERSION},
            )
        receipt = _receipt("transactional", checks)
        receipt["upgraded_from_schema_version"] = SCHEMA_VERSION - 1
        receipt["receipt_digest"] = _digest(
            {key: value for key, value in receipt.items() if key != "receipt_digest"}
        )
        return receipt


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _check(
    check_id: str,
    status: str,
    reason_code: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "status": status,
        "reason_code": reason_code,
    }
    if evidence:
        result["evidence"] = dict(evidence)
    return result


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _private_regular(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    mode = stat.S_IMODE(metadata.st_mode)
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and mode & 0o600 == 0o600
        and not mode & 0o077
    )


def _database_identity(path: Path) -> DatabaseIdentity:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("database path is not a regular file")
    return DatabaseIdentity(device=metadata.st_dev, inode=metadata.st_ino)


def _active_cutover_in_existing_store(path: Path) -> bool | None:
    """Return active-cutover state, or None when an existing store is unreadable."""

    if not path.exists() and not path.is_symlink():
        return False
    try:
        database = _read_only_database(path)
    except (OSError, sqlite3.Error):
        return None
    try:
        table = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_cutovers'"
        ).fetchone()
        if table is None:
            return False
        return (
            database.execute(
                "SELECT 1 FROM runtime_cutovers WHERE state = 'active' LIMIT 1"
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return None
    finally:
        database.close()


def _validate_writer_admission(
    admission: StartupAdmission,
    database: sqlite3.Connection,
    database_path: Path,
    *,
    specifications: tuple[tuple[int, str, Path], ...],
) -> None:
    """Fail when checked authority changed before writer admission.

    The caller owns the event-store writer lock and supplies the exact opened
    SQLite connection. This function is query-only and rolls back its snapshot.
    """

    if (
        not (
            admission.receipt.get("healthy")
            or admission.receipt.get("upgrade_required")
        )
        or admission.receipt.get("mode") != "transactional"
        or admission.repository is None
        or admission.plugin_root is None
        or admission.policy is None
        or admission.database_path is None
        or admission.database_identity is None
        or not admission.identity_document
        or not admission.cutover_document
        or database_path != admission.database_path
    ):
        raise RuntimeStateError("startup admission authority is incomplete")
    identity = json.loads(admission.identity_document)
    marker = json.loads(admission.cutover_document)
    layout = repository_layout(admission.repository)
    if (
        _permission_check(layout, database_path, require_writer_lock=True)["status"]
        != "pass"
    ):
        raise RuntimeStateError("runtime permissions changed after startup diagnostics")
    if (
        canonical_json(repository_identity(admission.repository))
        != admission.identity_document
    ):
        raise RuntimeStateError("repository identity changed after startup diagnostics")
    current_marker = runtime_cutover_marker(admission.repository)
    if (
        current_marker is None
        or canonical_json(current_marker) != admission.cutover_document
    ):
        raise RuntimeStateError("cutover marker changed after startup diagnostics")
    if hashlib.sha256(admission.policy.content).hexdigest() != admission.policy.digest:
        raise RuntimeStateError("retained canonical policy content is invalid")
    try:
        active_version = manifest_version(admission.plugin_root)
        minimum_version = str(marker["minimum_orka_version"])
        if release_version(active_version) < release_version(minimum_version):
            raise RuntimeStateError(
                "minimum Orka version changed after startup diagnostics"
            )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeStateError("minimum Orka version is invalid") from exc

    database.execute("BEGIN")
    try:
        if [str(row[0]) for row in database.execute("PRAGMA quick_check(1)")] != ["ok"]:
            raise RuntimeStateError(
                "SQLite quick check changed after startup diagnostics"
            )
        if list(database.execute("PRAGMA foreign_key_check")):
            raise RuntimeStateError("foreign keys changed after startup diagnostics")
        if _schema_digest(database) != _expected_schema_digest(specifications):
            raise RuntimeStateError("schema changed after startup diagnostics")
        migrations = [
            [int(row[0]), str(row[1]), str(row[2])]
            for row in database.execute(
                "SELECT version, migration_id, source_digest FROM schema_migrations ORDER BY version"
            )
        ]
        metadata = database.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        expected_version = specifications[-1][0]
        if migrations != _expected_migrations(specifications) or metadata != (
            str(expected_version),
        ):
            raise RuntimeStateError(
                "migration ledger changed after startup diagnostics"
            )
        repositories = database.execute("""
            SELECT repository_id, common_directory, object_directory_id,
                   policy_ref, policy_path, policy_commit, policy_blob,
                   policy_digest, created_at
            FROM repositories
            """).fetchall()
        expected_repository = [
            identity["repository_uuid"],
            identity["common_directory"],
            identity["object_directory_identity"],
            identity["policy_ref"],
            identity["policy_path"],
            admission.policy.commit,
            admission.policy.blob,
            admission.policy.digest,
            identity["created_at"],
        ]
        if len(repositories) != 1 or list(map(str, repositories[0])) != list(
            map(str, expected_repository)
        ):
            raise RuntimeStateError(
                "repository authority changed after startup diagnostics"
            )
        cutovers = database.execute("""
            SELECT repository_id, activation_id, marker_digest,
                   minimum_version, legacy_snapshot_digest, state
            FROM runtime_cutovers
            """).fetchall()
        expected_cutover = [
            identity["repository_uuid"],
            marker["activation_id"],
            _digest(marker),
            marker["minimum_orka_version"],
            marker["legacy_snapshot_digest"],
            "active",
        ]
        if len(cutovers) != 1 or list(map(str, cutovers[0])) != list(
            map(str, expected_cutover)
        ):
            raise RuntimeStateError(
                "cutover authority changed after startup diagnostics"
            )
    except (KeyError, sqlite3.Error) as exc:
        raise RuntimeStateError("startup authority cannot be revalidated") from exc
    finally:
        database.rollback()


def _expected_schema_digest(
    specifications: tuple[tuple[int, str, Path], ...] = MIGRATIONS,
) -> str:
    reference = sqlite3.connect(":memory:")
    try:
        for _version, _migration_id, path in specifications:
            reference.executescript(path.read_text(encoding="utf-8"))
        return _schema_digest(reference)
    finally:
        reference.close()


def _schema_digest(database: sqlite3.Connection) -> str:
    rows = database.execute("""
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """).fetchall()
    return _digest([list(map(str, row)) for row in rows])


def _expected_migrations(
    specifications: tuple[tuple[int, str, Path], ...] = MIGRATIONS,
) -> list[list[Any]]:
    return [
        [version, migration_id, hashlib.sha256(path.read_bytes()).hexdigest()]
        for version, migration_id, path in specifications
    ]


def _read_only_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    database.execute("PRAGMA query_only = ON")
    return database


def _permission_check(
    layout: Any,
    database_path: Path,
    *,
    require_writer_lock: bool = False,
) -> dict[str, Any]:
    state_root = layout.state_root
    runtime_files = (state_root / IDENTITY_FILE, state_root / CUTOVER_FILE)
    try:
        root_mode = _mode(state_root)
    except OSError:
        return _check("runtime_permissions", "fail", "runtime_path_unavailable")
    if (
        state_root.is_symlink()
        or not state_root.is_dir()
        or root_mode & 0o700 != 0o700
        or root_mode & 0o077
        or not os.access(state_root, os.R_OK | os.W_OK | os.X_OK)
        or any(not _private_regular(path) for path in runtime_files)
    ):
        return _check(
            "runtime_permissions",
            "fail",
            "runtime_path_permissions_invalid",
            {"runtime_mode": f"{root_mode:04o}"},
        )
    if not _private_regular(database_path) or not os.access(
        database_path, os.R_OK | os.W_OK
    ):
        return _check(
            "runtime_permissions",
            "fail",
            "database_permissions_invalid",
        )
    writer_lock = Path(f"{database_path}.writer.lock")
    if require_writer_lock and not _private_regular(writer_lock):
        return _check(
            "runtime_permissions",
            "fail",
            "database_permissions_invalid",
        )
    sidecars = [
        candidate
        for suffix in ("-journal", "-wal", "-shm", ".writer.lock")
        if (candidate := Path(f"{database_path}{suffix}")).exists()
        or candidate.is_symlink()
    ]
    if any(not _private_regular(path) for path in sidecars):
        return _check(
            "runtime_permissions",
            "fail",
            "database_permissions_invalid",
            {"sidecar_count": len(sidecars)},
        )
    return _check(
        "runtime_permissions",
        "pass",
        "ok",
        {
            "database_mode": f"{_mode(database_path):04o}",
            "runtime_mode": f"{root_mode:04o}",
            "sidecar_count": len(sidecars),
        },
    )


def _legacy_admission(repository: Path) -> StartupAdmission:
    checks: dict[str, dict[str, Any]] = {}
    policy: PolicySnapshot | None = None
    for check_id in CHECK_ORDER:
        checks[check_id] = _check(check_id, "skipped", "legacy_not_applicable")
    try:
        layout = repository_layout(repository)
    except RuntimeStateError:
        checks["repository_identity"] = _check(
            "repository_identity", "fail", "repository_identity_invalid"
        )
        return StartupAdmission(receipt=_receipt("legacy", checks))
    identity_path = layout.state_root / IDENTITY_FILE
    if identity_path.exists() or identity_path.is_symlink():
        try:
            identity = repository_identity(repository)
            checks["repository_identity"] = _check(
                "repository_identity",
                "pass",
                "ok",
                {"identity_schema_version": int(identity["schema_version"])},
            )
        except (KeyError, OSError, RuntimeStateError, TypeError, ValueError):
            checks["repository_identity"] = _check(
                "repository_identity", "fail", "repository_identity_invalid"
            )
        try:
            policy = resolve_canonical_policy(repository)
            checks["canonical_policy"] = _check(
                "canonical_policy",
                "pass",
                "ok",
                {"policy_digest": policy.digest},
            )
        except (OSError, RuntimeStateError):
            checks["canonical_policy"] = _check(
                "canonical_policy", "fail", "canonical_policy_invalid"
            )
    else:
        config = repository / ".orchestration/config.yaml"
        if config.is_file() and not config.is_symlink():
            checks["canonical_policy"] = _check(
                "canonical_policy",
                "pass",
                "ok",
                {"policy_digest": hashlib.sha256(config.read_bytes()).hexdigest()},
            )
        else:
            # Preserve the 1.x preflight path and its durable failure state.
            # Legacy policy absence is diagnosed by the existing preflight,
            # not promoted into a new event-store admission condition.
            checks["canonical_policy"] = _check(
                "canonical_policy", "skipped", "legacy_uninitialized"
            )
        checks["repository_identity"] = _check(
            "repository_identity", "skipped", "legacy_uninitialized"
        )
    return StartupAdmission(receipt=_receipt("legacy", checks), policy=policy)


def _receipt(mode: str, checks: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    ordered = [checks[check_id] for check_id in CHECK_ORDER]
    failed = [item["reason_code"] for item in ordered if item["status"] == "fail"]
    upgrade_required = any(item["status"] == "upgrade_required" for item in ordered)
    receipt: dict[str, Any] = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "mode": mode,
        "healthy": not failed and not upgrade_required,
        "upgrade_required": upgrade_required and not failed,
        "checks": ordered,
        "failure_reason_codes": failed,
        "operator_next_actions": [NEXT_ACTIONS[code] for code in failed],
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def prepare_startup_admission(
    repository: Path,
    *,
    plugin_root: Path | None = None,
    connection_factory: Callable[[Path], sqlite3.Connection] = _read_only_database,
) -> StartupAdmission:
    """Check startup state and retain exact capabilities outside the receipt."""

    repository = repository.resolve()
    try:
        layout = repository_layout(repository)
    except RuntimeStateError:
        checks = {
            check_id: _check(check_id, "skipped", "dependency_failed")
            for check_id in CHECK_ORDER
        }
        checks["repository_identity"] = _check(
            "repository_identity", "fail", "repository_identity_invalid"
        )
        return StartupAdmission(receipt=_receipt("unknown", checks))

    marker_path = layout.state_root / CUTOVER_FILE
    database_path = layout.state_root / DATABASE_NAME
    if not marker_path.exists() and not marker_path.is_symlink():
        active_cutover = _active_cutover_in_existing_store(database_path)
        if active_cutover is False:
            return _legacy_admission(layout.working_root)

    checks: dict[str, dict[str, Any]] = {
        check_id: _check(check_id, "skipped", "dependency_failed")
        for check_id in CHECK_ORDER
    }
    checks["runtime_permissions"] = _permission_check(
        layout, database_path, require_writer_lock=True
    )

    identity: dict[str, Any] | None = None
    policy: Any = None
    marker: dict[str, Any] | None = None
    try:
        identity = repository_identity(repository)
        checks["repository_identity"] = _check(
            "repository_identity",
            "pass",
            "ok",
            {"identity_schema_version": int(identity["schema_version"])},
        )
    except (OSError, RuntimeStateError, ValueError, TypeError):
        checks["repository_identity"] = _check(
            "repository_identity", "fail", "repository_identity_invalid"
        )
    try:
        policy = resolve_canonical_policy(repository)
        checks["canonical_policy"] = _check(
            "canonical_policy",
            "pass",
            "ok",
            {"policy_digest": policy.digest},
        )
    except (OSError, RuntimeStateError):
        checks["canonical_policy"] = _check(
            "canonical_policy", "fail", "canonical_policy_invalid"
        )
    try:
        marker = runtime_cutover_marker(repository)
        if marker is None:
            raise RuntimeStateError("cutover marker disappeared")
        checks["cutover"] = _check(
            "cutover", "pass", "ok", {"marker_digest": _digest(marker)}
        )
    except (OSError, RuntimeStateError, ValueError, TypeError):
        checks["cutover"] = _check("cutover", "fail", "cutover_marker_invalid")

    root = plugin_root or Path(__file__).resolve().parent.parent
    if marker is not None:
        try:
            active = manifest_version(root)
            minimum = str(marker["minimum_orka_version"])
            if release_version(active) < release_version(minimum):
                checks["minimum_version"] = _check(
                    "minimum_version",
                    "fail",
                    "minimum_version_not_met",
                    {"active_version": active, "minimum_version": minimum},
                )
            else:
                checks["minimum_version"] = _check(
                    "minimum_version",
                    "pass",
                    "ok",
                    {"active_version": active, "minimum_version": minimum},
                )
        except (KeyError, TypeError, ValueError, OSError):
            checks["minimum_version"] = _check(
                "minimum_version", "fail", "minimum_version_invalid"
            )

    if not database_path.is_file() or database_path.is_symlink():
        checks["quick_check"] = _check("quick_check", "fail", "database_unavailable")
        return StartupAdmission(
            receipt=_receipt("transactional", checks), policy=policy
        )

    try:
        checked_database_identity = _database_identity(database_path)
    except OSError:
        checks["quick_check"] = _check("quick_check", "fail", "database_unavailable")
        return StartupAdmission(
            receipt=_receipt("transactional", checks), policy=policy
        )

    try:
        database = connection_factory(database_path)
    except (OSError, sqlite3.Error):
        checks["quick_check"] = _check("quick_check", "fail", "database_unavailable")
        return StartupAdmission(
            receipt=_receipt("transactional", checks), policy=policy
        )

    try:
        database.execute("BEGIN")
        try:
            quick = [str(row[0]) for row in database.execute("PRAGMA quick_check(1)")]
            checks["quick_check"] = _check(
                "quick_check",
                "pass" if quick == ["ok"] else "fail",
                "ok" if quick == ["ok"] else "quick_check_failed",
                {"result_count": len(quick)},
            )
        except sqlite3.Error:
            checks["quick_check"] = _check("quick_check", "fail", "quick_check_failed")
        try:
            violations = list(database.execute("PRAGMA foreign_key_check"))
            checks["foreign_keys"] = _check(
                "foreign_keys",
                "pass" if not violations else "fail",
                "ok" if not violations else "foreign_key_violation",
                {"violation_count": len(violations)},
            )
        except sqlite3.Error:
            checks["foreign_keys"] = _check(
                "foreign_keys", "fail", "foreign_key_violation"
            )
        try:
            observed_schema = _schema_digest(database)
            expected_schema = _expected_schema_digest()
            prior_schema = _expected_schema_digest(MIGRATIONS[:-1])
            schema_current = observed_schema == expected_schema
            schema_prior = observed_schema == prior_schema
            checks["schema"] = _check(
                "schema",
                (
                    "pass"
                    if schema_current
                    else "upgrade_required" if schema_prior else "fail"
                ),
                (
                    "ok"
                    if schema_current
                    else (
                        "schema_upgrade_required" if schema_prior else "schema_mismatch"
                    )
                ),
                {
                    "expected_digest": expected_schema,
                    "observed_digest": observed_schema,
                    "schema_version": (
                        SCHEMA_VERSION if schema_current else SCHEMA_VERSION - 1
                    ),
                },
            )
        except (OSError, sqlite3.Error):
            checks["schema"] = _check("schema", "fail", "schema_mismatch")
        try:
            migrations = [
                [int(row[0]), str(row[1]), str(row[2])]
                for row in database.execute(
                    "SELECT version, migration_id, source_digest FROM schema_migrations ORDER BY version"
                )
            ]
            metadata = database.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            expected = _expected_migrations()
            expected_prior = _expected_migrations(MIGRATIONS[:-1])
            matches = migrations == expected and metadata == (str(SCHEMA_VERSION),)
            matches_prior = migrations == expected_prior and metadata == (
                str(SCHEMA_VERSION - 1),
            )
            checks["migration_ledger"] = _check(
                "migration_ledger",
                "pass" if matches else "upgrade_required" if matches_prior else "fail",
                (
                    "ok"
                    if matches
                    else (
                        "schema_upgrade_required"
                        if matches_prior
                        else "migration_ledger_mismatch"
                    )
                ),
                {
                    "expected_digest": _digest(expected),
                    "observed_digest": _digest(migrations),
                    "record_count": len(migrations),
                },
            )
        except sqlite3.Error:
            checks["migration_ledger"] = _check(
                "migration_ledger", "fail", "migration_ledger_mismatch"
            )

        if identity is not None:
            try:
                row = database.execute("""
                    SELECT repository_id, common_directory, object_directory_id,
                           policy_ref, policy_path, policy_commit, policy_blob,
                           policy_digest, created_at
                    FROM repositories
                    """).fetchall()
                expected_binding = [
                    identity["repository_uuid"],
                    identity["common_directory"],
                    identity["object_directory_identity"],
                    identity["policy_ref"],
                    identity["policy_path"],
                    policy.commit if policy is not None else "",
                    policy.blob if policy is not None else "",
                    policy.digest if policy is not None else "",
                    identity["created_at"],
                ]
                actual_binding = list(map(str, row[0])) if len(row) == 1 else []
                expected_strings = list(map(str, expected_binding))
                identity_matches = bool(actual_binding) and (
                    actual_binding[:5] + actual_binding[8:]
                    == expected_strings[:5] + expected_strings[8:]
                )
                policy_matches = bool(actual_binding) and (
                    actual_binding[5:8] == expected_strings[5:8]
                )
                if not identity_matches:
                    checks["repository_identity"] = _check(
                        "repository_identity",
                        "fail",
                        "repository_binding_mismatch",
                    )
                if policy is not None and not policy_matches:
                    checks["canonical_policy"] = _check(
                        "canonical_policy",
                        "fail",
                        "canonical_policy_binding_mismatch",
                    )
                elif policy is not None:
                    checks["canonical_policy"] = _check(
                        "canonical_policy",
                        "pass",
                        "ok",
                        {"policy_digest": policy.digest},
                    )
            except (KeyError, sqlite3.Error):
                checks["repository_identity"] = _check(
                    "repository_identity", "fail", "repository_binding_mismatch"
                )

        if marker is not None and identity is not None:
            try:
                rows = database.execute("""
                    SELECT repository_id, activation_id, marker_digest,
                           minimum_version, legacy_snapshot_digest, state
                    FROM runtime_cutovers
                    """).fetchall()
                expected_cutover = [
                    identity["repository_uuid"],
                    marker["activation_id"],
                    _digest(marker),
                    marker["minimum_orka_version"],
                    marker["legacy_snapshot_digest"],
                    "active",
                ]
                matches = len(rows) == 1 and list(map(str, rows[0])) == list(
                    map(str, expected_cutover)
                )
                checks["cutover"] = _check(
                    "cutover",
                    "pass" if matches else "fail",
                    "ok" if matches else "cutover_binding_mismatch",
                    {"marker_digest": _digest(marker)},
                )
            except (KeyError, sqlite3.Error):
                checks["cutover"] = _check(
                    "cutover", "fail", "cutover_binding_mismatch"
                )
        database.rollback()
    finally:
        database.close()
    try:
        if _database_identity(database_path) != checked_database_identity:
            checks["quick_check"] = _check(
                "quick_check", "fail", "database_unavailable"
            )
    except OSError:
        checks["quick_check"] = _check("quick_check", "fail", "database_unavailable")
    receipt = _receipt("transactional", checks)
    retained_authority = bool(receipt["healthy"] or receipt["upgrade_required"])
    return StartupAdmission(
        receipt=receipt,
        policy=policy,
        database_path=database_path if retained_authority else None,
        database_identity=checked_database_identity if retained_authority else None,
        repository=repository if retained_authority else None,
        plugin_root=root if retained_authority else None,
        identity_document=(
            canonical_json(identity)
            if retained_authority and identity is not None
            else ""
        ),
        cutover_document=(
            canonical_json(marker) if retained_authority and marker is not None else ""
        ),
    )


def run_startup_diagnostics(
    repository: Path,
    *,
    plugin_root: Path | None = None,
    connection_factory: Callable[[Path], sqlite3.Connection] = _read_only_database,
) -> dict[str, Any]:
    """Return only the deterministic sanitized portion of startup admission."""

    return prepare_startup_admission(
        repository,
        plugin_root=plugin_root,
        connection_factory=connection_factory,
    ).receipt
