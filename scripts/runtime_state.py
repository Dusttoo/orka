#!/usr/bin/env python3
"""Resolve repository identity, canonical policy, and legacy runtime state."""

from __future__ import annotations

import argparse
import contextlib
import filecmp
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


IDENTITY_SCHEMA_VERSION = 1
RUNTIME_DIRECTORY = "orka-runtime"
IDENTITY_FILE = "repository.json"
CUTOVER_FILE = "cutover.json"


class RuntimeStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryLayout:
    working_root: Path
    common_directory: Path
    object_directory: Path
    object_format: str
    bare: bool
    state_root: Path


@dataclass(frozen=True)
class PolicySnapshot:
    policy_ref: str
    policy_path: str
    commit: str
    blob: str
    digest: str
    content: bytes


def _git(start: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), *args],
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            detail = f": {(stderr or '').strip()}" if stderr else ""
        raise RuntimeStateError(f"git {' '.join(args)} failed{detail}") from exc
    return result.stdout


def working_repository_root(start: Path) -> Path:
    start = start.resolve()
    try:
        value = str(_git(start, "rev-parse", "--show-toplevel")).strip()
        if value:
            return Path(value).resolve()
    except RuntimeStateError:
        pass
    if (start / ".git").exists():
        return start
    return start


def repository_layout(start: Path) -> RepositoryLayout:
    """Return the Git-common-directory boundary without using its parent."""
    probe = start.resolve()
    common_raw = str(
        _git(probe, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).strip()
    object_raw = str(
        _git(probe, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    ).strip()
    object_format = str(_git(probe, "rev-parse", "--show-object-format")).strip()
    try:
        common = Path(common_raw).resolve(strict=True)
        objects = Path(object_raw).resolve(strict=True)
    except OSError as exc:
        raise RuntimeStateError("Git returned an invalid common or object directory") from exc
    # A linked checkout reports itself as non-bare even when its common
    # directory is a bare repository. Query the authority directory itself.
    bare_raw = str(_git(common, "rev-parse", "--is-bare-repository")).strip()
    if not common.is_dir() or not objects.is_dir():
        raise RuntimeStateError("Git common or object directory is not a directory")
    try:
        working = Path(str(_git(probe, "rev-parse", "--show-toplevel")).strip()).resolve()
    except RuntimeStateError:
        working = probe
    return RepositoryLayout(
        working_root=working,
        common_directory=common,
        object_directory=objects,
        object_format=object_format,
        bare=bare_raw == "true",
        state_root=common / RUNTIME_DIRECTORY,
    )


def repository_runtime_root(start: Path) -> Path:
    return repository_layout(start).state_root


def _validate_private_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise RuntimeStateError(f"repository runtime directory is unavailable: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeStateError(f"repository runtime path is not a real directory: {path}")
    if mode & 0o077:
        raise RuntimeStateError(f"repository runtime directory must be private (chmod 700): {path}")


def _validate_policy_binding(policy_ref: str, policy_path: str) -> tuple[str, str]:
    policy_ref = policy_ref.strip()
    policy_path = policy_path.strip()
    if not policy_ref.startswith("refs/") or any(ch.isspace() for ch in policy_ref):
        raise RuntimeStateError("policy ref must be an explicit fully qualified refs/... name")
    pure = PurePosixPath(policy_path)
    if (
        not policy_path
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != policy_path
    ):
        raise RuntimeStateError("policy path must be a normalized repository-relative path")
    return policy_ref, pure.as_posix()


def _resolve_policy(layout: RepositoryLayout, policy_ref: str, policy_path: str) -> PolicySnapshot:
    policy_ref, policy_path = _validate_policy_binding(policy_ref, policy_path)
    _git(layout.common_directory, "check-ref-format", policy_ref)
    commit = str(_git(layout.common_directory, "rev-parse", "--verify", f"{policy_ref}^{{commit}}")).strip()
    blob = str(
        _git(layout.common_directory, "rev-parse", "--verify", f"{commit}:{policy_path}")
    ).strip()
    object_type = str(_git(layout.common_directory, "cat-file", "-t", blob)).strip()
    if object_type != "blob":
        raise RuntimeStateError(f"canonical policy object is {object_type}, not a blob")
    content = bytes(_git(layout.common_directory, "cat-file", "blob", blob, text=False))
    if not content:
        raise RuntimeStateError("canonical policy blob is empty")
    return PolicySnapshot(
        policy_ref=policy_ref,
        policy_path=policy_path,
        commit=commit,
        blob=blob,
        digest=hashlib.sha256(content).hexdigest(),
        content=content,
    )


@contextlib.contextmanager
def _initialization_lock(layout: RepositoryLayout) -> Iterator[None]:
    lock_path = layout.common_directory / ".orka-runtime-initialize.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeStateError(f"cannot acquire repository initialization lock: {lock_path}") from exc
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise RuntimeStateError("short write while persisting repository runtime state")
        view = view[written:]


def _object_directory_identity(layout: RepositoryLayout) -> str:
    material = f"{layout.object_directory}\0{layout.object_format}".encode()
    return hashlib.sha256(material).hexdigest()


def _identity_path(layout: RepositoryLayout) -> Path:
    return layout.state_root / IDENTITY_FILE


def _read_identity(layout: RepositoryLayout) -> dict[str, Any]:
    _validate_private_directory(layout.state_root)
    marker = _identity_path(layout)
    try:
        mode = marker.lstat().st_mode
    except OSError as exc:
        raise RuntimeStateError(f"repository identity is not initialized: {marker}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeStateError("repository identity marker must be a regular file, not a symlink")
    if mode & 0o077:
        raise RuntimeStateError(f"repository identity marker must be private (chmod 600): {marker}")
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeStateError("repository identity marker is malformed") from exc
    required = {
        "schema_version", "repository_uuid", "common_directory", "object_directory",
        "object_directory_identity", "object_format", "created_at", "policy_ref", "policy_path",
    }
    if not isinstance(raw, dict) or required - raw.keys():
        raise RuntimeStateError("repository identity marker is missing required fields")
    if raw["schema_version"] != IDENTITY_SCHEMA_VERSION:
        raise RuntimeStateError(f"unsupported repository identity schema: {raw['schema_version']!r}")
    try:
        uuid.UUID(str(raw["repository_uuid"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeStateError("repository identity UUID is malformed") from exc
    expected = {
        "common_directory": str(layout.common_directory),
        "object_directory": str(layout.object_directory),
        "object_directory_identity": _object_directory_identity(layout),
        "object_format": layout.object_format,
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise RuntimeStateError(
                f"repository identity {key} binding does not match this repository; explicit migration is required"
            )
    _validate_policy_binding(str(raw["policy_ref"]), str(raw["policy_path"]))
    return raw


def initialize_repository_identity(
    start: Path, *, policy_ref: str, policy_path: str
) -> dict[str, Any]:
    """Create the private repository marker atomically, or validate it."""
    layout = repository_layout(start)
    policy_ref, policy_path = _validate_policy_binding(policy_ref, policy_path)
    snapshot = _resolve_policy(layout, policy_ref, policy_path)
    with _initialization_lock(layout):
        if layout.state_root.exists() or layout.state_root.is_symlink():
            _validate_private_directory(layout.state_root)
        else:
            _validate_private_directory(layout.state_root, create=True)
        marker = _identity_path(layout)
        if marker.exists() or marker.is_symlink():
            identity = _read_identity(layout)
            if identity["policy_ref"] != policy_ref or identity["policy_path"] != policy_path:
                raise RuntimeStateError("repository identity already binds a different canonical policy")
            return identity
        identity = {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "repository_uuid": str(uuid.uuid4()),
            "common_directory": str(layout.common_directory),
            "object_directory": str(layout.object_directory),
            "object_directory_identity": _object_directory_identity(layout),
            "object_format": layout.object_format,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_ref": snapshot.policy_ref,
            "policy_path": snapshot.policy_path,
        }
        temporary = layout.state_root / f".{IDENTITY_FILE}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            payload = (json.dumps(identity, indent=2, sort_keys=True) + "\n").encode()
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, marker)
            directory = os.open(layout.state_root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return _read_identity(layout)


def resolve_canonical_policy(start: Path) -> PolicySnapshot:
    layout = repository_layout(start)
    identity = _read_identity(layout)
    return _resolve_policy(layout, str(identity["policy_ref"]), str(identity["policy_path"]))


def repository_identity(start: Path) -> dict[str, Any]:
    """Return the exact initialized repository identity or fail closed."""

    return _read_identity(repository_layout(start))


@contextlib.contextmanager
def repository_initialization_authority(start: Path) -> Iterator[None]:
    """Exclude identity initialization while an offline state operation runs."""

    with _initialization_lock(repository_layout(start)):
        yield


def runtime_cutover_marker(start: Path) -> dict[str, Any] | None:
    """Return and authenticate the repository-local cutover marker."""

    layout = repository_layout(start)
    marker = layout.state_root / CUTOVER_FILE
    if not marker.exists() and not marker.is_symlink():
        return None
    try:
        mode = marker.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or mode & 0o077:
            raise RuntimeStateError(
                "runtime cutover marker must be a private regular file"
            )
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeStateError("runtime cutover marker is unreadable") from exc
    required = {
        "schema_version",
        "repository_id",
        "minimum_orka_version",
        "legacy_snapshot_digest",
        "activated_at",
        "activation_id",
    }
    if not isinstance(value, dict) or required - value.keys():
        raise RuntimeStateError("runtime cutover marker is missing required fields")
    if value["schema_version"] != 1:
        raise RuntimeStateError("unsupported runtime cutover marker schema")
    identity = _read_identity(layout)
    if value["repository_id"] != identity["repository_uuid"]:
        raise RuntimeStateError("runtime cutover marker belongs to another repository")
    material = {key: value[key] for key in sorted(value) if key != "activation_id"}
    expected = hashlib.sha256(
        json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    if value["activation_id"] != f"cutover-{expected}":
        raise RuntimeStateError("runtime cutover marker digest is invalid")
    return value


def assert_legacy_runtime_writable(
    start: Path, *, plugin_root: Path | None = None
) -> None:
    """Refuse every legacy JSON mutation once transactional cutover is active."""

    marker = runtime_cutover_marker(start)
    if marker is None:
        return
    assert_cutover_runtime_compatible(start, plugin_root=plugin_root, marker=marker)
    raise RuntimeStateError(
        "legacy JSON runtime is read-only after transactional cutover; "
        "use the repository supervisor"
    )


def assert_cutover_runtime_compatible(
    start: Path,
    *,
    plugin_root: Path | None = None,
    marker: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Verify an active cutover's minimum version without rejecting its supervisor."""

    marker = marker if marker is not None else runtime_cutover_marker(start)
    if marker is None:
        return None
    try:
        from version_policy import manifest_version, release_version

        root = plugin_root or Path(__file__).resolve().parent.parent
        active = manifest_version(root)
        minimum = str(marker["minimum_orka_version"])
        if release_version(active) < release_version(minimum):
            raise RuntimeStateError(
                f"active Orka version {active} is below transactional cutover minimum {minimum}"
            )
    except (ImportError, ValueError) as exc:
        raise RuntimeStateError(
            "transactional cutover cannot verify the active Orka version"
        ) from exc
    return marker


def _materialize_policy(layout: RepositoryLayout, snapshot: PolicySnapshot) -> Path:
    policy_root = layout.state_root / "policy"
    if policy_root.exists() or policy_root.is_symlink():
        _validate_private_directory(policy_root)
    else:
        policy_root.mkdir(mode=0o700)
    destination = policy_root / f"{snapshot.digest}.yaml"
    if destination.exists() or destination.is_symlink():
        mode = destination.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or mode & 0o077:
            raise RuntimeStateError("canonical policy snapshot is not a private regular file")
        if destination.read_bytes() != snapshot.content:
            raise RuntimeStateError("canonical policy snapshot digest collision or corruption")
        return destination
    temporary = policy_root / f".{snapshot.digest}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _write_all(descriptor, snapshot.content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def legacy_state_inventory(start: Path) -> dict[str, Any]:
    layout = repository_layout(start)
    candidates: set[Path] = set()
    try:
        output = str(_git(layout.common_directory, "worktree", "list", "--porcelain"))
        for line in output.splitlines():
            if line.startswith("worktree "):
                candidate = Path(line[9:]).resolve() / ".orchestration"
                if candidate.exists():
                    candidates.add(candidate)
    except RuntimeStateError:
        pass
    historical_parent = layout.common_directory.parent / ".orchestration"
    if layout.bare and historical_parent.exists():
        candidates.add(historical_parent.resolve())
    return {
        "candidates": [str(path) for path in sorted(candidates)],
        "ambiguous_parent_state": str(historical_parent.resolve())
        if layout.bare and historical_parent.exists()
        else None,
    }


def repository_status(start: Path) -> dict[str, Any]:
    layout = repository_layout(start)
    result: dict[str, Any] = {
        "mode": "legacy-uninitialized",
        "common_directory": str(layout.common_directory),
        "object_directory_identity": _object_directory_identity(layout),
        "state_root": str(layout.state_root),
        "bare": layout.bare,
        "legacy_state": legacy_state_inventory(start),
    }
    marker = _identity_path(layout)
    if not marker.exists() and not marker.is_symlink():
        return result
    identity = _read_identity(layout)
    policy = _resolve_policy(layout, str(identity["policy_ref"]), str(identity["policy_path"]))
    result.update(
        {
            "mode": "initialized",
            "repository_uuid": identity["repository_uuid"],
            "canonical_policy": {
                "ref": policy.policy_ref,
                "path": policy.policy_path,
                "commit": policy.commit,
                "blob": policy.blob,
                "sha256": policy.digest,
            },
        }
    )
    return result


def shared_repository_root(start: Path) -> Path:
    """Return legacy JSON authority without leaking out of a bare repository."""
    root = working_repository_root(start)
    try:
        layout = repository_layout(root)
        return layout.common_directory if layout.bare else layout.common_directory.parent
    except RuntimeStateError:
        return root


@contextlib.contextmanager
def _migration_lock(root: Path) -> Iterator[None]:
    lock_path = root / ".git" / "orchestration-runtime-migration.lock"
    if not lock_path.parent.is_dir():
        lock_path = root / ".orchestration-runtime-migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def migrate_legacy_runtime_dir(start: Path, relative: str | Path) -> Path:
    """Copy worktree-local pre-0.11 state into the shared domain, or fail."""
    working = working_repository_root(start)
    shared = shared_repository_root(working)
    requested = Path(relative)
    if requested.is_absolute():
        raise RuntimeStateError("runtime state path must be repository-relative")
    target = shared_runtime_path(working, requested)
    with _migration_lock(shared):
        roots = [working]
        try:
            output = str(_git(working, "worktree", "list", "--porcelain"))
            roots = [
                Path(line[9:]).resolve() for line in output.splitlines()
                if line.startswith("worktree ")
            ] or roots
        except RuntimeStateError:
            pass
        sources: list[tuple[Path, Path]] = []
        for checkout in roots:
            legacy = (checkout / requested).resolve()
            if legacy == target or not legacy.exists():
                continue
            if legacy.is_file():
                sources.append((legacy, target))
            else:
                sources.extend(
                    (source, target / source.relative_to(legacy))
                    for source in sorted(path for path in legacy.rglob("*") if path.is_file())
                )
        pending: dict[Path, Path] = {}
        for source, destination in sources:
            prior = pending.get(destination)
            if destination.exists() and not filecmp.cmp(source, destination, shallow=False):
                raise RuntimeStateError(f"conflicting legacy runtime state: {source} and {destination}")
            if prior and not filecmp.cmp(source, prior, shallow=False):
                raise RuntimeStateError(f"conflicting legacy runtime state: {source} and {prior}")
            pending[destination] = source
        for destination, source in pending.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
        for source, _ in sources:
            source.unlink(missing_ok=True)
        for checkout in roots:
            legacy = (checkout / requested).resolve()
            if legacy != target and legacy.is_dir():
                for directory in sorted(
                    (path for path in legacy.rglob("*") if path.is_dir()), reverse=True
                ):
                    with contextlib.suppress(OSError):
                        directory.rmdir()
                with contextlib.suppress(OSError):
                    legacy.rmdir()
    return target


def shared_runtime_path(start: Path, relative: str | Path) -> Path:
    root = shared_repository_root(start)
    requested = Path(relative)
    if requested.is_absolute():
        raise RuntimeStateError("runtime state path must be repository-relative")
    resolved = (root / requested).resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeStateError("runtime state path escapes the shared repository root")
    return resolved


def canonical_config_path(start: Path, requested: str | Path | None = None) -> Path:
    """Return initialized Git-blob policy, or the compatible legacy policy."""
    try:
        layout = repository_layout(start)
    except RuntimeStateError:
        canonical = (shared_repository_root(start) / ".orchestration/config.yaml").resolve()
    else:
        marker = _identity_path(layout)
        if marker.exists() or marker.is_symlink():
            canonical = _materialize_policy(layout, resolve_canonical_policy(start))
        else:
            canonical = (shared_repository_root(start) / ".orchestration/config.yaml").resolve()
    if requested:
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = (working_repository_root(start) / candidate).resolve()
        else:
            candidate = candidate.resolve()
        local_default = (working_repository_root(start) / ".orchestration/config.yaml").resolve()
        if candidate not in {canonical, local_default}:
            raise RuntimeStateError("alternate orchestration config paths are not allowed")
    return canonical


def resolve_config_argument(start: Path, requested: str | Path) -> Path:
    """Redirect the repository-local default while preserving explicit fixtures."""
    working = working_repository_root(start)
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = (working / candidate).resolve()
    else:
        candidate = candidate.resolve()
    local_default = (working / ".orchestration/config.yaml").resolve()
    if candidate == local_default:
        return canonical_config_path(working, candidate)
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init", help="Initialize repository identity and policy authority")
    initialize.add_argument("--repo", default=".")
    initialize.add_argument("--policy-ref", required=True)
    initialize.add_argument("--policy-path", default=".orchestration/config.yaml")
    status = subparsers.add_parser("status", help="Report repository identity and policy authority")
    status.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize_repository_identity(
                Path(args.repo), policy_ref=args.policy_ref, policy_path=args.policy_path
            )
        report = repository_status(Path(args.repo))
    except RuntimeStateError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, indent=2))
        return 2
    print(json.dumps({"status": "ready", **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
