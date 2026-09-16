#!/usr/bin/env python3
"""Build compact Anthropic/OpenAI/Azure ADM payloads and prune Jira responses.

This module is deliberately transport-neutral: host adapters own credentials and
HTTP, while this layer owns stable prompt ordering, cache boundaries, Jira field
selection, and sanitization before untrusted ticket data reaches an LLM.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_JIRA_FIELDS = [
    "key",
    "summary",
    "description",
    "status",
    "priority",
    "components",
    "subtasks",
    "issuelinks",
]
DROP_JIRA_KEYS = {
    "renderedfields",
    "renderingschema",
    "renderschema",
    "editmeta",
    "changelog",
    "schema",
    "names",
    "avatarurl",
    "avatarurls",
    "expand",
    "iconurl",
    "self",
}
ROUTE_FIELDS = {"execution", "provider", "fallback", "model", "effort", "allowed_tools"}
WORKER_TRUST_PROFILES = {"cooperative-worker", "isolated-worker"}
LLM_POLICY_BLOCKS = {"budgets", "pricing"}
EXECUTIONS = {"desktop", "api"}
PROVIDERS = {"anthropic", "openai", "azure_adm", "bedrock", "bedrock_mantle"}
FALLBACKS = {"desktop", "none"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
REVIEW_MODES = {"code-review", "security-review"}
REVIEW_SCHEMA_VERSION = 1
DEFAULT_PAYLOAD_MAX_TOKENS = 8192


class ContextError(RuntimeError):
    pass


def bedrock_model_family(model: str) -> str:
    """Return the provider family encoded in a Bedrock model/profile ID."""
    if ".anthropic." in model or model.startswith("anthropic."):
        return "anthropic"
    if ".openai." in model or model.startswith("openai."):
        return "openai"
    return "other"


def review_output_schema(gate: str) -> dict[str, Any]:
    """Return the stable, provider-neutral contract for a reviewer result."""
    if gate not in REVIEW_MODES:
        raise ContextError(f"unsupported review gate: {gate}")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "gate", "verdict", "checks", "findings"],
        "properties": {
            "schema_version": {"type": "integer", "enum": [REVIEW_SCHEMA_VERSION]},
            "gate": {"type": "string", "enum": [gate]},
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "checks": {
                "type": "array", "maxItems": 16,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["name", "status"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 80},
                        "status": {"type": "string", "enum": ["pass", "fail", "not_run", "not_applicable"]},
                    },
                },
            },
            "findings": {
                "type": "array", "maxItems": 20,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["component", "disposition", "severity", "title", "explanation", "regression"],
                    "properties": {
                        "component": {
                            "type": "string",
                            "minLength": 3,
                            "maxLength": 240,
                            "description": (
                                "Bare repo-relative <path>:<symbol> key; do not include "
                                "whitespace, a [component: ...] wrapper, or a line number"
                            ),
                        },
                        "disposition": {"type": "string", "enum": ["blocking", "advisory"]},
                        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "title": {"type": "string", "minLength": 1, "maxLength": 120},
                        "explanation": {"type": "string", "minLength": 1, "maxLength": 1200},
                        "regression": {"type": "boolean"},
                    },
                },
            },
        },
    }


def validate_review_output(value: Any, expected_gate: str) -> dict[str, Any]:
    """Validate semantic rules that provider JSON Schema cannot express portably."""
    if expected_gate not in REVIEW_MODES:
        raise ContextError(f"unsupported review gate: {expected_gate}")
    if not isinstance(value, dict):
        raise ContextError("review output must be a JSON object")
    expected_keys = {"schema_version", "gate", "verdict", "checks", "findings"}
    if set(value) != expected_keys:
        extra = sorted(set(value) - expected_keys)
        missing = sorted(expected_keys - set(value))
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise ContextError("invalid review fields: " + "; ".join(detail))
    if value["schema_version"] != REVIEW_SCHEMA_VERSION:
        raise ContextError(f"review schema_version must be {REVIEW_SCHEMA_VERSION}")
    if value["gate"] != expected_gate:
        raise ContextError(f"review gate must be {expected_gate}")
    if value["verdict"] not in {"PASS", "FAIL"}:
        raise ContextError("review verdict must be PASS or FAIL")
    checks, findings = value["checks"], value["findings"]
    if not isinstance(checks, list) or len(checks) > 16:
        raise ContextError("review checks must be an array of at most 16 items")
    seen_checks: set[str] = set()
    failing_check = False
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"name", "status"}:
            raise ContextError("each review check must contain only name and status")
        name, status = check["name"], check["status"]
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ContextError("review check names must be 1-80 characters")
        if name.casefold() in seen_checks:
            raise ContextError(f"duplicate review check: {name}")
        seen_checks.add(name.casefold())
        if status not in {"pass", "fail", "not_run", "not_applicable"}:
            raise ContextError(f"invalid status for review check {name}: {status}")
        failing_check = failing_check or status in {"fail", "not_run"}
    if not isinstance(findings, list) or len(findings) > 20:
        raise ContextError("review findings must be an array of at most 20 items")
    blocking = 0
    normalized_findings: list[dict[str, Any]] = []
    required = {"component", "disposition", "severity", "title", "explanation", "regression"}
    for raw_finding in findings:
        if not isinstance(raw_finding, dict) or set(raw_finding) != required:
            raise ContextError("each finding must contain only the six documented fields")
        finding = dict(raw_finding)
        component = finding["component"]
        if not isinstance(component, str) or len(component) > 240:
            raise ContextError("finding component must be a string of at most 240 characters")
        legacy_wrapper = re.fullmatch(r"\[\s*component\s*:\s*(.*?)\s*\]", component, re.IGNORECASE)
        if legacy_wrapper:
            component = legacy_wrapper.group(1)
            finding["component"] = component
        if not re.fullmatch(r"[^\s:][^\s]*:[^\s:][^\s]*", component):
            raise ContextError(f"finding component must be <path>:<symbol>: {component!r}")
        if re.search(r":\d+(?::\d+)?$", component):
            raise ContextError(f"finding component must use a symbol, not a line number: {component}")
        if finding["disposition"] not in {"blocking", "advisory"}:
            raise ContextError(f"invalid finding disposition: {finding['disposition']}")
        if finding["severity"] not in {"critical", "high", "medium", "low"}:
            raise ContextError(f"invalid finding severity: {finding['severity']}")
        for field, limit in (("title", 120), ("explanation", 1200)):
            item = finding[field]
            if not isinstance(item, str) or not item.strip() or len(item) > limit:
                raise ContextError(f"finding {field} must be 1-{limit} characters")
        if not isinstance(finding["regression"], bool):
            raise ContextError("finding regression must be a boolean")
        blocking += finding["disposition"] == "blocking"
        normalized_findings.append(finding)
    if value["verdict"] == "PASS" and blocking:
        raise ContextError("PASS review cannot contain blocking findings")
    if value["verdict"] == "FAIL" and not blocking:
        raise ContextError("FAIL review requires at least one blocking finding")
    if failing_check and not blocking:
        raise ContextError("failed or unrun checks require a blocking finding with the explanation")
    result = dict(value)
    result["findings"] = normalized_findings
    return result


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _strip_yaml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _flat_config_scalar(lines: list[str], key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(.*?)\s*$")
    for raw in lines:
        clean = _strip_yaml_comment(raw).strip()
        match = pattern.fullmatch(clean)
        if match:
            value = _unquote(match.group(1))
            return value or None
    return None


def max_output_tokens_from_config(path: Path) -> int:
    """Resolve the repository's payload ceiling without a YAML dependency."""
    if not path.is_file():
        return DEFAULT_PAYLOAD_MAX_TOKENS
    llm_indent: int | None = None
    budgets_indent: int | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        clean = _strip_yaml_comment(raw).rstrip()
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        text = clean.strip()
        if llm_indent is None:
            if re.fullmatch(r"llm\s*:\s*", text):
                llm_indent = indent
            continue
        if indent <= llm_indent:
            break
        if budgets_indent is None:
            if re.fullmatch(r"budgets\s*:\s*", text):
                budgets_indent = indent
            continue
        if indent <= budgets_indent:
            break
        match = re.fullmatch(r"max_output_tokens_per_turn\s*:\s*(.*?)\s*", text)
        if not match:
            continue
        value = _unquote(match.group(1))
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ContextError("llm.budgets.max_output_tokens_per_turn must be a positive integer")
        return int(value)
    return DEFAULT_PAYLOAD_MAX_TOKENS


def _canonical_role(role: str) -> str:
    value = role.strip().casefold().replace("_", "-")
    if value.startswith("orchestration-"):
        value = value[len("orchestration-") :]
    if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
        raise ContextError(f"invalid LLM role name: {role!r}")
    return value


def _route_value(field: str, value: str) -> Any:
    value = value.strip()
    if field != "allowed_tools":
        return _unquote(value)
    if not value:
        return []
    if not (value.startswith("[") and value.endswith("]")):
        raise ContextError("llm role allowed_tools must be an inline YAML list")
    tools = [_unquote(item).strip() for item in value[1:-1].split(",") if item.strip()]
    for tool in tools:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", tool):
            raise ContextError(f"invalid LLM tool name: {tool!r}")
    return list(dict.fromkeys(tools))


def _validate_route(route: dict[str, Any], role: str) -> dict[str, Any]:
    execution = str(route.get("execution") or "desktop").casefold()
    provider = str(route.get("provider") or "anthropic").casefold()
    fallback = str(route.get("fallback") or "none").casefold()
    model = str(route.get("model") or "").strip()
    effort = str(route.get("effort") or "").casefold()
    allowed_tools = route.get("allowed_tools") or []
    if execution not in EXECUTIONS:
        raise ContextError(f"llm route {role} execution must be desktop or api")
    if provider not in PROVIDERS:
        raise ContextError(
            f"llm route {role} provider must be one of: {', '.join(sorted(PROVIDERS))}"
        )
    if fallback not in FALLBACKS:
        raise ContextError(f"llm route {role} fallback must be desktop or none")
    if effort and effort not in EFFORTS:
        raise ContextError(f"llm route {role} effort must be one of: {', '.join(sorted(EFFORTS))}")
    if execution == "api" and not model:
        raise ContextError(f"llm route {role} uses api execution but has no model")
    if execution == "desktop":
        fallback = "none"
    if not isinstance(allowed_tools, list):
        raise ContextError(f"llm route {role} allowed_tools must be a list")
    return {
        "role": role,
        "execution": execution,
        "provider": provider,
        "model": model,
        "effort": effort,
        "fallback": fallback,
        "allowed_tools": allowed_tools,
        "fallback_before_provider_ack_only": fallback == "desktop",
    }


def llm_route_from_config(path: Path, requested_role: str) -> dict[str, Any]:
    """Resolve global LLM policy plus optional field-by-field role overrides."""
    role = _canonical_role(requested_role)
    if not path.is_file():
        return _validate_route({}, role)
    lines = path.read_text(encoding="utf-8").splitlines()
    global_route: dict[str, Any] = {
        "execution": _flat_config_scalar(lines, "llm_execution") or "desktop",
        "provider": _flat_config_scalar(lines, "llm_provider") or "anthropic",
        "fallback": _flat_config_scalar(lines, "llm_fallback") or "none",
        "model": _flat_config_scalar(lines, "llm_model") or "",
        "effort": _flat_config_scalar(lines, "llm_effort") or "",
        "allowed_tools": [],
    }
    overrides: dict[str, dict[str, Any]] = {}
    llm_indent: int | None = None
    roles_indent: int | None = None
    current_role: str | None = None
    current_role_indent: int | None = None
    ignored_indent: int | None = None
    for raw in lines:
        clean = _strip_yaml_comment(raw).rstrip()
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        text = clean.strip()
        if llm_indent is None:
            if re.fullmatch(r"llm\s*:\s*", text):
                llm_indent = indent
            continue
        if indent <= llm_indent:
            break
        if ignored_indent is not None:
            if indent > ignored_indent:
                continue
            ignored_indent = None
        if roles_indent is not None and indent <= roles_indent:
            roles_indent = None
            current_role = None
            current_role_indent = None
        if roles_indent is None:
            if re.fullmatch(r"roles\s*:\s*", text):
                roles_indent = indent
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)", text)
            if not match:
                raise ContextError(f"invalid llm configuration line: {text}")
            field, value = match.groups()
            if field in LLM_POLICY_BLOCKS and not value:
                ignored_indent = indent
                continue
            if field not in ROUTE_FIELDS:
                raise ContextError(f"unknown llm field: {field}")
            global_route[field] = _route_value(field, value)
            continue
        if current_role is None or indent <= (current_role_indent or roles_indent):
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*", text)
            if not match or indent <= roles_indent:
                raise ContextError(f"invalid llm.roles entry: {text}")
            current_role = _canonical_role(match.group(1))
            current_role_indent = indent
            overrides.setdefault(current_role, {})
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)", text)
        if not match:
            raise ContextError(f"invalid llm.roles.{current_role} line: {text}")
        field, value = match.groups()
        if field not in ROUTE_FIELDS:
            raise ContextError(f"unknown llm.roles.{current_role} field: {field}")
        overrides[current_role][field] = _route_value(field, value)
    resolved = dict(global_route)
    resolved.update(overrides.get(role, {}))
    _validate_route(global_route, "default")
    for configured_role, override in overrides.items():
        configured = dict(global_route)
        configured.update(override)
        _validate_route(configured, configured_role)
    return _validate_route(resolved, role)


def worker_trust_profile_from_config(path: Path | None) -> str:
    """Resolve the host-worker threat model without requiring a YAML runtime."""
    if path is None or not path.is_file():
        return "cooperative-worker"
    value = _flat_config_scalar(
        path.read_text(encoding="utf-8").splitlines(), "worker_trust_profile"
    )
    profile = value or "cooperative-worker"
    if profile not in WORKER_TRUST_PROFILES:
        raise ContextError(
            "worker_trust_profile must be cooperative-worker or isolated-worker"
        )
    return profile


def jira_fields_from_config(path: Path) -> list[str]:
    """Read ticket.jira_fields without requiring a YAML runtime dependency."""
    if not path.is_file():
        return list(DEFAULT_JIRA_FIELDS)
    lines = path.read_text(encoding="utf-8").splitlines()
    ticket_indent: int | None = None
    field_indent: int | None = None
    values: list[str] = []
    for raw in lines:
        clean = raw.split("#", 1)[0].rstrip()
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        text = clean.strip()
        if ticket_indent is None:
            if re.fullmatch(r"ticket\s*:\s*", text):
                ticket_indent = indent
            continue
        if indent <= ticket_indent:
            break
        if field_indent is None:
            match = re.fullmatch(r"jira_fields\s*:\s*(.*)", text)
            if not match:
                continue
            field_indent = indent
            inline = match.group(1).strip()
            if inline:
                if not (inline.startswith("[") and inline.endswith("]")):
                    raise ContextError("ticket.jira_fields must be a YAML list")
                values = [_unquote(item) for item in inline[1:-1].split(",") if item.strip()]
                break
            continue
        if indent <= field_indent:
            break
        match = re.fullmatch(r"-\s+(.+)", text)
        if not match:
            raise ContextError("ticket.jira_fields must contain scalar field names")
        values.append(_unquote(match.group(1)))
    if field_indent is None:
        return list(DEFAULT_JIRA_FIELDS)
    cleaned: list[str] = []
    for field in values:
        field = field.strip()
        if not field or not re.fullmatch(r"[A-Za-z0-9_.-]+", field):
            raise ContextError(f"invalid Jira field name: {field!r}")
        if field not in cleaned:
            cleaned.append(field)
    if not cleaned:
        raise ContextError("ticket.jira_fields must not be empty")
    return cleaned


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _sanitize_value(child)
        for key, child in value.items()
        if str(key).casefold().replace("_", "") not in DROP_JIRA_KEYS
    }


def _sanitize_issue(issue: Any, requested: set[str]) -> Any:
    if not isinstance(issue, dict):
        return _sanitize_value(issue)
    result: dict[str, Any] = {}
    if "key" in issue and "key" in requested:
        result["key"] = issue["key"]
    fields = issue.get("fields")
    if isinstance(fields, dict):
        result["fields"] = {
            key: _sanitize_value(value)
            for key, value in fields.items()
            if key in requested and key != "key"
        }
    else:
        for key, value in issue.items():
            if key in requested:
                result[key] = _sanitize_value(value)
    return result


def sanitize_jira_response(value: Any, fields: list[str]) -> Any:
    """Allow only requested issue fields and recursively remove bulky metadata."""
    requested = set(fields)
    if isinstance(value, dict) and isinstance(value.get("issues"), list):
        result = {
            key: _sanitize_value(child)
            for key, child in value.items()
            if key.casefold() not in {
                "issues", "renderedfields", "editmeta", "changelog", "schema", "names", "expand"
            }
        }
        result["issues"] = [_sanitize_issue(issue, requested) for issue in value["issues"]]
        return result
    return _sanitize_issue(value, requested)


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8").strip()


def _joined_files(paths: list[str], heading: str) -> str:
    chunks = []
    for raw in paths:
        path = Path(raw)
        chunks.append(f"## {path.name}\n{path.read_text(encoding='utf-8').strip()}")
    if not chunks:
        raise ContextError(f"at least one {heading} file is required")
    return f"# {heading}\n" + "\n\n".join(chunks)


def ordered_context(args: argparse.Namespace) -> tuple[list[str], str]:
    """Return the stable prefix sections and dynamic suffix in canonical order."""
    profile = worker_trust_profile_from_config(
        Path(args.config) if args.config else None
    )
    profile_contract = (
        "# Orchestration worker trust profile\n"
        f"Selected profile: {profile}. This profile applies only to orchestration "
        "workers versus their host/controller. It never weakens the threat model "
        "for application users, tenants, remote clients, ticket text, or external "
        "provider responses. Judge findings against this selected profile."
    )
    stable = [
        _joined_files(args.role_file, "Global role briefs")
        + "\n\n"
        + profile_contract,
        _joined_files(args.rules_file, "Repository rules and conventions"),
    ]
    repo_map = _read_text(args.repo_map)
    if not repo_map:
        raise ContextError("a non-empty stable repository map is required")
    stable.append(f"# Stable repository map\n{repo_map}")
    ticket = _read_text(args.ticket)
    diff = _read_text(args.diff)
    if args.mode in {"code-review", "security-review"} and not diff:
        raise ContextError("review payloads require a raw unified diff")
    dynamic = []
    if ticket:
        dynamic.append(f"<ticket>\n{ticket}\n</ticket>")
    if diff:
        dynamic.append(f"<active_branch_unified_diff>\n{diff}\n</active_branch_unified_diff>")
    if args.mode in {"code-review", "security-review"}:
        dynamic.insert(
            0,
            "Review the supplied raw unified diff as the default and authoritative code scope. "
            "Do not index or ingest the full repository. Open additional files only when an "
            "explicit verification or regression check requires a named path, test, or symbol.",
        )
    if not dynamic:
        raise ContextError("dynamic ticket data or a diff is required")
    return stable, "\n\n".join(dynamic)


def anthropic_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Return an Anthropic Messages API body with an explicit cache prefix."""
    stable, dynamic = ordered_context(args)
    system = [{"type": "text", "text": text} for text in stable]
    boundary = args.cache_boundary
    if boundary == "auto":
        boundary = "repo-map"
    index = 1 if boundary == "rules" else 2
    system[index]["cache_control"] = {"type": "ephemeral"}
    result: dict[str, Any] = {
        "model": args.model,
        "max_tokens": args.max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": dynamic}],
    }
    if args.effort:
        result["thinking"] = {"type": "adaptive"}
        result["output_config"] = {"effort": args.effort}
    if args.mode in REVIEW_MODES:
        result.setdefault("output_config", {})["format"] = {
            "type": "json_schema",
            "schema": review_output_schema(args.mode),
        }
    return result


def openai_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Return a route-compatible OpenAI Responses API body."""
    stable, dynamic = ordered_context(args)
    blocks = [{"type": "input_text", "text": text} for text in stable]
    result: dict[str, Any] = {
        "model": args.model,
        "max_output_tokens": args.max_tokens,
        "input": [
            {"role": "developer", "content": blocks},
            {"role": "user", "content": [{"type": "input_text", "text": dynamic}]},
        ],
    }
    if args.effort:
        result["reasoning"] = {"effort": args.effort}
    if args.mode in REVIEW_MODES:
        result["text"] = {
            "verbosity": "low",
            "format": {
                "type": "json_schema", "name": "orchestration_review_v1", "strict": True,
                "schema": review_output_schema(args.mode),
            },
        }
    return result


def azure_adm_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Return an OpenAI-compatible Chat Completions body for Azure Direct Models."""
    stable, dynamic = ordered_context(args)
    system = "\n\n".join(stable)
    if args.mode in REVIEW_MODES:
        contract = (
            "FINAL RESPONSE CONTRACT: Your entire final response must be exactly one JSON "
            "object beginning with { and ending with }. Do not emit analysis, Markdown, a "
            "heading, commentary, or a code fence before or after the object. The object must "
            "match this schema: "
            + json.dumps(review_output_schema(args.mode), separators=(",", ":"))
        )
        system += "\n\n" + contract
        # Some Azure Direct Models follow the most recent instruction more
        # reliably than a long system prefix. Repeat the contract after the
        # dynamic diff so it is the final instruction before generation.
        dynamic += "\n\n" + contract
    return {
        "model": args.model,
        "max_completion_tokens": args.max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": dynamic},
        ],
    }


def bedrock_mantle_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Return an OpenAI-compatible Chat Completions body for Bedrock Mantle."""
    result = azure_adm_payload(args)
    result["max_tokens"] = result.pop("max_completion_tokens")
    return result


def bedrock_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Return a native Amazon Bedrock Converse request with a stable cache prefix."""
    stable, dynamic = ordered_context(args)
    family = bedrock_model_family(args.model)
    system: list[dict[str, Any]] = [{"text": value} for value in stable]
    if family == "anthropic":
        boundary = args.cache_boundary
        if boundary == "auto":
            boundary = "repo-map"
        index = 1 if boundary == "rules" else 2
        system.insert(index + 1, {"cachePoint": {"type": "default", "ttl": "1h"}})
    additional: dict[str, Any] = {}
    if args.effort and family == "anthropic":
        additional["thinking"] = {"type": "adaptive"}
        additional["output_config"] = {"effort": args.effort}
    elif args.effort and family == "openai":
        additional["reasoning_effort"] = args.effort
    elif args.effort:
        raise ContextError(
            f"Bedrock effort is not mapped for model family in {args.model!r}"
        )
    if args.mode in REVIEW_MODES:
        contract = (
            "FINAL RESPONSE CONTRACT: Your entire final response must be exactly one JSON "
            "object beginning with { and ending with }. Do not emit analysis, Markdown, a "
            "heading, commentary, or a code fence before or after the object. The object must "
            "match this schema: "
            + json.dumps(review_output_schema(args.mode), separators=(",", ":"))
        )
        system.append({"text": contract})
        dynamic += "\n\n" + contract
    result: dict[str, Any] = {
        "modelId": args.model,
        "inferenceConfig": {"maxTokens": args.max_tokens},
        "system": system,
        "messages": [{"role": "user", "content": [{"text": dynamic}]}],
    }
    if additional:
        result["additionalModelRequestFields"] = additional
    return result


def provider_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.config:
        if not args.role:
            raise ContextError("--role is required when --config resolves an LLM route")
        route = llm_route_from_config(Path(args.config), args.role)
        if route["execution"] != "api":
            raise ContextError(
                f"llm route {route['role']} resolves to desktop; launch the desktop adapter instead"
            )
        args.provider = route["provider"]
        args.model = route["model"]
        args.effort = route["effort"] or None
    configured_cap = (
        max_output_tokens_from_config(Path(args.config))
        if args.config else DEFAULT_PAYLOAD_MAX_TOKENS
    )
    if args.max_tokens is None:
        args.max_tokens = configured_cap
    elif args.max_tokens < 1:
        raise ContextError("--max-tokens must be a positive integer")
    elif args.config:
        args.max_tokens = min(args.max_tokens, configured_cap)
    if not args.provider:
        raise ContextError("--provider is required without an API route config")
    if not args.model:
        raise ContextError("--model is required without an API route config")
    if args.provider == "anthropic":
        return anthropic_payload(args)
    if args.provider == "azure_adm":
        return azure_adm_payload(args)
    if args.provider == "bedrock_mantle":
        return bedrock_mantle_payload(args)
    if args.provider == "bedrock":
        return bedrock_payload(args)
    return openai_payload(args)


def add_payload_arguments(command: argparse.ArgumentParser, provider: bool = True) -> None:
    if provider:
        command.add_argument(
            "--provider",
            choices=["anthropic", "openai", "azure_adm", "bedrock", "bedrock_mantle"],
        )
    command.add_argument("--config", help="resolve provider/model/effort from an API role route")
    command.add_argument("--role", help="role to resolve when --config is used")
    command.add_argument("--role-file", action="append", default=[], required=True)
    command.add_argument("--rules-file", action="append", default=[], required=True)
    command.add_argument("--repo-map", required=True)
    command.add_argument("--ticket")
    command.add_argument("--diff")
    command.add_argument(
        "--mode",
        choices=["scope", "implement", "code-review", "security-review"],
        default="implement",
    )
    # Retained as a call-site label. Cache breakpoints are now unconditional:
    # an execution mode must never be able to silently disable prompt caching.
    command.add_argument(
        "--execution",
        choices=["on-demand", "gate"],
        default="on-demand",
        help="call-site label; does not change the emitted payload",
    )
    command.add_argument("--cache-boundary", choices=["auto", "rules", "repo-map"], default="auto")
    command.add_argument("--model")
    command.add_argument(
        "--max-tokens",
        type=int,
        help=(
            "output-token request; defaults to llm.budgets.max_output_tokens_per_turn "
            "when --config is provided, otherwise 8192"
        ),
    )
    command.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    fields = commands.add_parser("jira-fields", help="emit explicit Jira fields request parameters")
    fields.add_argument("--config", default=".orchestration/config.yaml")
    sanitize = commands.add_parser("sanitize-jira", help="sanitize Jira JSON from a file or stdin")
    sanitize.add_argument("--config", default=".orchestration/config.yaml")
    sanitize.add_argument("--input", help="JSON input; defaults to stdin")
    route = commands.add_parser("route", help="resolve desktop/API execution policy for one role")
    route.add_argument("--config", default=".orchestration/config.yaml")
    route.add_argument("--role", required=True)
    schema = commands.add_parser("review-schema", help="emit the structured reviewer JSON Schema")
    schema.add_argument("--gate", choices=sorted(REVIEW_MODES), required=True)
    validate = commands.add_parser("validate-review", help="validate a structured reviewer result")
    validate.add_argument("--gate", choices=sorted(REVIEW_MODES), required=True)
    validate.add_argument("--input", help="JSON input; defaults to stdin")
    build = commands.add_parser("payload", help="assemble an ordered Anthropic or OpenAI API payload")
    add_payload_arguments(build)
    anthropic = commands.add_parser("anthropic", help="backward-compatible Anthropic payload alias")
    add_payload_arguments(anthropic, provider=False)
    anthropic.set_defaults(provider="anthropic")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "jira-fields":
            fields = jira_fields_from_config(Path(args.config))
            output = {"fields": ",".join(fields), "field_list": fields}
        elif args.command == "sanitize-jira":
            fields = jira_fields_from_config(Path(args.config))
            raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
            output = sanitize_jira_response(json.loads(raw), fields)
        elif args.command == "route":
            output = llm_route_from_config(Path(args.config), args.role)
        elif args.command == "review-schema":
            output = review_output_schema(args.gate)
        elif args.command == "validate-review":
            raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
            output = validate_review_output(json.loads(raw), args.gate)
        else:
            output = provider_payload(args)
        print(json.dumps(output, indent=2, sort_keys=False))
        return 0
    except (ContextError, OSError, json.JSONDecodeError) as exc:
        print(f"context-pipeline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
