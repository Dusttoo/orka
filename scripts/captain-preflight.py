#!/usr/bin/env python3
"""Fail-closed provenance check before an interactive sprint captain starts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


REQUIRED = (
    "scripts/sprint-controller.py",
    "scripts/orchestration-engine.py",
    "scripts/api_agent.py",
    "scripts/provider_health.py",
    "scripts/runtime_smoke.py",
    "scripts/native_gateway.py",
    "scripts/codex_gateway.py",
    "scripts/ticket_dependencies.py",
    "scripts/jira_inventory_fetch.py",
    "skills/orchestrate-sprint/SKILL.md",
    "skills/orchestrate-ticket/SKILL.md",
)


def release_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise ValueError(f"invalid Orka release version: {value}")
    return tuple(int(part) for part in match.groups())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    parser.add_argument("--verify-runtime", action="store_true", help="Run bounded client and authenticated token-count checks")
    parser.add_argument("--after-repair", action="store_true", help="Permit a fresh probe after correcting an auth/client incident")
    args = parser.parse_args()
    plugin = Path(args.plugin_root).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()
    manifest = plugin / ".claude-plugin/plugin.json"
    failures = [str(plugin / relative) for relative in REQUIRED if not (plugin / relative).is_file()]
    if not manifest.is_file():
        failures.append(str(manifest))
        version = "unknown"
    else:
        version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "unknown")
    config = repo / ".orchestration/config.yaml"
    if not config.is_file():
        failures.append(str(config))
    if failures:
        print(json.dumps({"status": "blocked", "missing": failures}, indent=2))
        return 2
    from api_agent import load_yaml
    minimum_version = str(load_yaml(config).get("minimum_orka_version") or "").strip()
    if minimum_version:
        try:
            if release_version(version) < release_version(minimum_version):
                print(json.dumps({
                    "status": "blocked",
                    "installation_status": "incompatible",
                    "plugin_version": version,
                    "minimum_orka_version": minimum_version,
                    "reason": "active Orka version is below repository minimum",
                }, indent=2))
                return 2
        except ValueError as exc:
            print(json.dumps({
                "status": "blocked",
                "installation_status": "incompatible",
                "plugin_version": version,
                "minimum_orka_version": minimum_version,
                "reason": str(exc),
            }, indent=2))
            return 2
    digest = hashlib.sha256()
    for relative in REQUIRED:
        digest.update(relative.encode())
        digest.update((plugin / relative).read_bytes())
    from context_pipeline import ContextError, llm_route_from_config
    from provider_health import HealthError, ProviderHealth, route_identity, probe
    routes = []
    for role in ("sprint-worker", "ticket-scoper", "implementer", "design-reviewer", "code-reviewer", "security-reviewer"):
        route = {"provider": "unknown", "model": ""}
        try:
            route = llm_route_from_config(config, role)
            status = (probe(repo, config, role, args.after_repair) if args.verify_runtime else
                      ProviderHealth(repo).status(route["provider"], route_identity(route)))
        except (ContextError, HealthError) as exc:
            status = {"state": "incompatible", "reason": str(exc)}
        routes.append({"role": role, "provider": route["provider"], "model": route["model"], **status})
    execution_ready = all(item["state"] == "healthy" for item in routes)
    print(json.dumps({
        "status": "ready" if execution_ready else "blocked",
        "installation_status": "ready",
        "execution_ready": execution_ready,
        "routes": routes,
        "captain_mode": "controller-only",
        "host": args.host,
        "plugin_root": str(plugin),
        "plugin_version": version,
        "minimum_orka_version": minimum_version or None,
        "runtime_fingerprint": digest.hexdigest(),
        "rules": [
            "do not implement sprint tickets in the captain context",
            "do not invent, approximate, or bypass missing plugin skills",
            "use sprint-controller for every reservation and terminal transition",
            "launch only the exact orchestrate-ticket skill from this plugin root",
        ],
    }, indent=2))
    return 0 if execution_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
