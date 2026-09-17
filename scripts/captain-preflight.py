#!/usr/bin/env python3
"""Fail-closed provenance check before an interactive sprint captain starts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_policy import VersionPolicyError, assert_minimum_version, release_version


REQUIRED = (
    "scripts/sprint-controller.py",
    "scripts/orchestration-engine.py",
    "scripts/api_agent.py",
    "scripts/provider_health.py",
    "scripts/runtime_smoke.py",
    "scripts/version_policy.py",
    "scripts/native_gateway.py",
    "scripts/codex_gateway.py",
    "scripts/ticket_dependencies.py",
    "scripts/jira_inventory_fetch.py",
    "scripts/runtime_state.py",
    "skills/orchestrate-sprint/SKILL.md",
    "skills/orchestrate-ticket/SKILL.md",
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def jira_readiness(
    repo: Path,
    config: Path,
    *,
    skip_live: bool,
    opener: Any = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Prove the Jira access `sprint-controller.py sync` needs, without secrets.

    Policy is parsed and credentials are resolved by the same functions the
    controller-owned Jira adapter uses, so preflight and sync cannot disagree.
    """
    import jira_inventory_fetch as jira
    from api_agent import AgentError, load_yaml

    result: dict[str, Any] = {
        "state": "blocked",
        "live_check": "not_run",
        "credential_file": str(jira.credential_file(repo)),
    }
    try:
        ticket = load_yaml(config).get("ticket")
    except AgentError as exc:
        return {**result, "reason": str(exc)}
    kind = str(ticket.get("kind") or "") if isinstance(ticket, dict) else ""
    if kind != "jira":
        return {**result, "reason": f"orchestrate-sprint requires ticket.kind: jira (found {kind or 'unset'})"}
    project = jira.ticket_project_from_config(config).upper()
    sprint_policy = jira.scalar_config(config, "sprint_id", "").strip()
    base_url = jira.scalar_config(config, "jira_base_url", "").strip()
    if not project or not sprint_policy:
        return {**result, "reason": "ticket.project and sprint_id are required canonical Jira policy"}
    if not base_url:
        return {**result, "reason": "jira_base_url is required canonical Jira policy"}
    try:
        result["jira_origin"] = jira.validate_base_url(base_url)
        credentials = jira.resolve_jira_credentials(repo, environ)
    except ValueError as exc:
        return {**result, "reason": str(exc)}
    result["auth_mode"] = "basic" if credentials["email"] else "bearer"
    result["credential_sources"] = credentials["sources"]
    if skip_live:
        return {**result, "state": "ready", "live_check": "skipped"}
    try:
        jira.verify_jira_identity(base_url, credentials, opener=opener)
    except ValueError as exc:
        return {**result, "live_check": "failed", "reason": str(exc)}
    return {**result, "state": "ready", "live_check": "passed"}


def budget_report(config: Path, repo: Path | None = None) -> dict[str, Any]:
    """Effective (capped) budget limits, cap warnings, and the hard-cap source."""
    import api_agent

    try:
        loaded = api_agent.load_yaml(config)
        limits = api_agent.budgets_from_config(loaded, repo)
    except (api_agent.AgentError, ValueError) as exc:
        return {"budget_limits": None, "budget_error": str(exc)}
    report: dict[str, Any] = {"budget_limits": _json_safe(dict(sorted(limits.items())))}
    violations = getattr(api_agent, "budget_cap_violations", None)
    if callable(violations):
        report["budget_cap_warnings"] = _json_safe(violations(loaded, repo))
    report["budget_policy"] = _json_safe(api_agent.host_budget_policy_status(repo))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--host", choices=("claude", "codex"), required=True)
    parser.add_argument("--verify-runtime", action="store_true", help="Run bounded client and authenticated token-count checks")
    parser.add_argument("--after-repair", action="store_true", help="Permit a fresh probe after correcting an auth/client incident")
    parser.add_argument(
        "--skip-jira-auth-check",
        action="store_true",
        help="Do not call Jira /myself; credentials must still be present. Reported in skipped_checks.",
    )
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
    try:
        policy = assert_minimum_version(plugin, config, active_version=version)
    except VersionPolicyError as exc:
        print(json.dumps({
            "status": "blocked",
            "installation_status": "incompatible",
            "plugin_version": exc.plugin_version or version,
            "minimum_orka_version": exc.minimum_version,
            "reason": str(exc),
        }, indent=2))
        return 2
    minimum_version = str(policy.get("minimum_orka_version") or "")
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
    jira = jira_readiness(repo, config, skip_live=args.skip_jira_auth_check)
    budgets = budget_report(config, repo)
    execution_ready = (
        all(item["state"] == "healthy" for item in routes)
        and jira["state"] == "ready"
        and budgets["budget_limits"] is not None
    )
    print(json.dumps({
        "status": "ready" if execution_ready else "blocked",
        "installation_status": "ready",
        "execution_ready": execution_ready,
        "routes": routes,
        "jira": jira,
        "skipped_checks": ["jira-auth"] if args.skip_jira_auth_check else [],
        **budgets,
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
