#!/usr/bin/env python3
"""Fail-closed repository minimum-version policy for every Orka entry point."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from typing import Any


class VersionPolicyError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        plugin_version: str | None = None,
        minimum_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.plugin_version = plugin_version
        self.minimum_version = minimum_version


def release_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise VersionPolicyError(f"invalid Orka release version: {value}")
    return tuple(int(part) for part in match.groups())


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise VersionPolicyError(f"orchestration configuration not found: {path}")
    engine = Path(__file__).with_name("orchestration-engine.py")
    spec = importlib.util.spec_from_file_location("orka_version_policy_loader", engine)
    if spec is None or spec.loader is None:
        raise VersionPolicyError("could not load orchestration configuration parser")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        value = module.load_simple_yaml(path)
    except Exception as exc:
        raise VersionPolicyError(f"could not parse orchestration configuration: {exc}") from exc
    if not isinstance(value, dict):
        raise VersionPolicyError("orchestration configuration root must be a map")
    return value


def manifest_version(plugin_root: Path) -> str:
    versions: list[str] = []
    for relative in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        path = plugin_root / relative
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VersionPolicyError(f"cannot read Orka manifest {path}: {exc}") from exc
        version = str(value.get("version") or "").strip()
        if not version:
            raise VersionPolicyError(f"Orka manifest has no version: {path}")
        versions.append(version)
    if versions[0] != versions[1]:
        raise VersionPolicyError("Claude and Codex Orka manifests disagree on version")
    return versions[0]


def assert_minimum_version(
    plugin_root: Path,
    config_path: Path,
    *,
    active_version: str | None = None,
    allow_missing_config: bool = False,
) -> dict[str, Any]:
    if allow_missing_config and not config_path.is_file():
        config: dict[str, Any] = {}
    else:
        config = load_config(config_path)
    minimum = str(config.get("minimum_orka_version") or "").strip()
    version = (active_version or manifest_version(plugin_root)).strip()
    if minimum:
        try:
            incompatible = release_version(version) < release_version(minimum)
        except VersionPolicyError as exc:
            raise VersionPolicyError(
                str(exc), plugin_version=version, minimum_version=minimum
            ) from exc
        if incompatible:
            raise VersionPolicyError(
                f"active Orka version {version} is below repository minimum {minimum}",
                plugin_version=version,
                minimum_version=minimum,
            )
    return {
        "status": "compatible",
        "plugin_version": version,
        "minimum_orka_version": minimum or None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--config", default=".orchestration/config.yaml")
    parser.add_argument("--active-version")
    args = parser.parse_args()
    try:
        result = assert_minimum_version(
            Path(args.plugin_root).expanduser().resolve(),
            Path(args.config).expanduser().resolve(),
            active_version=args.active_version,
        )
    except VersionPolicyError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
