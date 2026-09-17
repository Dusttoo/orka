#!/usr/bin/env python3
"""Fetch and derive exhaustive scheduler inventory through Jira REST v3."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from context_pipeline import sanitize_jira_response

Fetch = Callable[[str, str, int, int, str, list[str]], dict[str, Any]]
MAX_PAGES = 10_000
MAX_ITEMS = 1_000_000
DEFAULT_PRIORITY_ORDER = ["Highest", "High", "Medium", "Low", "Lowest"]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def url_origin(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Jira URL must use HTTPS without embedded credentials")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{parsed.hostname.lower()}{port}"


def validate_base_url(url: str) -> str:
    parsed = urlparse(url)
    approved = url_origin(url)
    if parsed.query or parsed.fragment:
        raise ValueError("Jira base URL cannot contain a query or fragment")
    return approved


# Backward-compatible helper for callers that only need an origin comparison.
origin = url_origin


class ApprovedOriginRedirectHandler(HTTPRedirectHandler):
    """Reject a redirect before urllib can copy credentials to its destination."""

    def __init__(self, approved_origin: str) -> None:
        super().__init__()
        self.approved_origin = approved_origin

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        absolute = urljoin(req.full_url, newurl)
        if url_origin(absolute) != self.approved_origin:
            raise ValueError(
                "Jira cross-origin redirect rejected before credentials were sent"
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, absolute)
        if redirected is None:
            redirected = Request(
                absolute, headers=dict(req.header_items()), method=req.method
            )
        return redirected


from ticket_dependencies import declared_dependencies


def required_fields(
    sprint_field: str, configured: list[str] | None = None
) -> list[str]:
    scoped = [field for field in (configured or []) if field == "description"]
    return list(
        dict.fromkeys(
            (
                "key",
                "summary",
                "status",
                "priority",
                "labels",
                "issuetype",
                "subtasks",
                "parent",
                "issuelinks",
                sprint_field,
                *scoped,
            )
        )
    )


def jira_text(value: Any) -> str:
    """Flatten Jira ADF/string descriptions for ephemeral scope assessment."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (jira_text(item) for item in value))).strip()
    if isinstance(value, dict):
        own = str(value.get("text") or "").strip()
        children = jira_text(value.get("content", []))
        return "\n".join(filter(None, (own, children))).strip()
    return ""


def sanitize_page(value: Any, fields: list[str]) -> dict[str, Any]:
    """Keep only pagination proof and the explicitly requested issue surface."""
    sanitized = sanitize_jira_response(value, fields)
    if not isinstance(sanitized, dict) or not isinstance(sanitized.get("issues"), list):
        raise ValueError("Jira response must contain an issues array")
    allowed = {"startAt", "total", "isLast", "nextPageToken", "issues"}
    return {key: child for key, child in sanitized.items() if key in allowed}


def network_fetcher(base_url: str) -> Fetch:
    approved = validate_base_url(base_url)
    endpoint = urljoin(base_url.rstrip("/") + "/", "rest/api/3/search/jql")
    if url_origin(endpoint) != approved:
        raise ValueError("Jira request escaped the approved origin")
    # Credentials are read only after canonical policy has established the trust anchor.
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not token:
        raise ValueError("JIRA_API_TOKEN is required")
    email = os.environ.get("JIRA_EMAIL", "")
    authorization = (
        "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        if email
        else "Bearer " + token
    )
    opener = build_opener(ApprovedOriginRedirectHandler(approved))

    def fetch(
        kind: str,
        jql: str,
        start_at: int,
        max_results: int,
        cursor: str,
        fields: list[str],
    ) -> dict[str, Any]:
        del kind
        query: dict[str, Any] = {
            "jql": jql,
            "fields": ",".join(fields),
            "maxResults": max_results,
        }
        if cursor:
            query["nextPageToken"] = cursor
        request = Request(
            endpoint + "?" + urlencode(query),
            headers={
                "Accept": "application/json",
                "Authorization": authorization,
            },
        )
        with opener.open(request, timeout=30) as response:
            if url_origin(response.geturl()) != approved:
                raise ValueError("Jira response escaped the approved origin")
            value = json.loads(response.read())
        return sanitize_page(value, fields)

    return fetch


def fixture_fetch(
    pages: dict[str, list[dict[str, Any]]], template: dict[str, Any]
) -> Fetch:
    """Return a clearly non-authoritative deterministic transport for tests only."""
    del template
    positions = {kind: 0 for kind in pages}

    def fetch(
        kind: str,
        jql: str,
        start_at: int,
        max_results: int,
        cursor: str,
        fields: list[str],
    ) -> dict[str, Any]:
        del jql, start_at, max_results, cursor
        try:
            page = pages[kind][positions[kind]]
        except (KeyError, IndexError) as exc:
            raise ValueError(f"test fixture has no {kind} Jira page") from exc
        positions[kind] += 1
        return sanitize_page(page, fields)

    return fetch


def exhaustive(
    fetch: Fetch, jql: str, kind: str, raw_dir: Path, fields: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_at, cursor = 0, ""
    pages: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    provider_total: int | None = None
    seen_cursors: set[str] = set()
    while True:
        if len(pages) >= MAX_PAGES or len(issues) >= MAX_ITEMS:
            raise ValueError("Jira pagination exceeded the page or item bound")
        if cursor:
            if cursor in seen_cursors:
                raise ValueError("Jira pagination repeated a previously seen cursor")
            seen_cursors.add(cursor)
        response = fetch(kind, jql, start_at, 100, cursor, fields)
        page_issues = response.get("issues")
        response_start = response.get("startAt", start_at)
        if not isinstance(page_issues, list) or response_start != start_at:
            raise ValueError("Jira returned invalid or non-contiguous pagination")
        total = response.get("total")
        if total is not None:
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("Jira returned an invalid total")
            if provider_total is None:
                provider_total = total
            elif provider_total != total:
                raise ValueError("Jira total changed during pagination")
        next_start = start_at + len(page_issues)
        if next_start > MAX_ITEMS:
            raise ValueError("Jira pagination exceeded the item bound")
        next_cursor = str(response.get("nextPageToken") or "")
        declared_last = response.get("isLast")
        if declared_last is not None and not isinstance(declared_last, bool):
            raise ValueError("Jira returned an invalid terminal marker")
        reached_total = provider_total is not None and next_start == provider_total
        if provider_total is not None and next_start > provider_total:
            raise ValueError("Jira returned more issues than its declared total")
        terminal = declared_last is True or (declared_last is None and reached_total)
        if declared_last is True and provider_total is not None and not reached_total:
            raise ValueError("Jira terminated before the declared total")
        if declared_last is False and reached_total:
            raise ValueError("Jira terminal marker conflicts with its declared total")
        raw = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        raw_path = raw_dir / f"sha256-{digest}.json"
        if not raw_path.exists():
            write_json(raw_path, response)
        keys = [str(item.get("key", "")).upper() for item in page_issues]
        pages.append(
            {
                "start_at": start_at,
                "count": len(page_issues),
                "total": total,
                "item_keys": keys,
                "terminal": terminal,
                "cursor_in": cursor,
                "cursor_out": next_cursor,
                "raw_sha256": digest,
                "raw_path": str(raw_path.resolve()),
            }
        )
        issues.extend(page_issues)
        if terminal:
            break
        if not next_cursor and provider_total is None:
            raise ValueError("Jira did not prove exhaustion or provide a next cursor")
        if not page_issues:
            raise ValueError("Jira pagination made no progress")
        if next_cursor and next_cursor == cursor:
            raise ValueError("Jira pagination repeated its cursor")
        start_at, cursor = next_start, next_cursor
    if provider_total is not None and len(issues) != provider_total:
        raise ValueError("Jira pages do not match the declared total")
    return pages, issues


def issue_key(issue: dict[str, Any]) -> str:
    key = str(issue.get("key", "")).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
        raise ValueError(f"invalid Jira issue key: {key!r}")
    return key


def status_name(issue: dict[str, Any]) -> str:
    status = (issue.get("fields") or {}).get("status")
    value = status.get("name") if isinstance(status, dict) else status
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"Jira issue {issue_key(issue)} has no status")
    return value


def sprint_value(
    issue: dict[str, Any], sprint_field: str, sprint_policy: str
) -> tuple[str, str]:
    value = (issue.get("fields") or {}).get(sprint_field)
    memberships = value if isinstance(value, list) else [value]
    memberships = [item for item in memberships if isinstance(item, dict)]
    if sprint_policy.casefold() == "active":
        matches = [
            item
            for item in memberships
            if str(item.get("state", "")).strip().casefold() == "active"
        ]
    else:
        wanted = sprint_policy.strip().casefold()
        if re.fullmatch(r"[0-9]+", wanted):
            matches = [
                item
                for item in memberships
                if str(item.get("id", "")).strip().casefold() == wanted
            ]
        else:
            matches = [
                item
                for item in memberships
                if wanted
                in {
                    str(item.get("id", "")).strip().casefold(),
                    str(item.get("name", "")).strip().casefold(),
                }
            ]
    if len(matches) != 1:
        raise ValueError(
            f"Jira issue {issue_key(issue)} does not prove exactly one configured current sprint"
        )
    value = matches[0]
    sprint_id, name = (
        str(value.get("id", "")).strip(),
        str(value.get("name", "")).strip(),
    )
    if not sprint_id or not name:
        raise ValueError(
            f"Jira issue {issue_key(issue)} has incomplete sprint identity"
        )
    return sprint_id, name


def validate_relations(
    parents: list[dict[str, Any]], children: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], dict[str, str]]:
    declared = {
        issue_key(child): issue_key(parent)
        for parent in parents
        for child in ((parent.get("fields") or {}).get("subtasks") or [])
    }
    returned = {
        issue_key(child): issue_key(((child.get("fields") or {}).get("parent") or {}))
        for child in children
    }
    if declared != returned:
        raise ValueError("Jira parent/child relations are incomplete or contradictory")
    relations = [
        {"parent": parent, "child": child} for child, parent in sorted(returned.items())
    ]
    return relations, dict(sorted(returned.items()))


def dependency_keys(
    issue: dict[str, Any], dependency_links: list[dict[str, str]]
) -> list[str]:
    mappings = {
        (str(item.get("type", "")).casefold(), str(item.get("blocked_side", "")))
        for item in dependency_links
    }
    result: set[str] = set()
    for link in (issue.get("fields") or {}).get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        type_name = str((link.get("type") or {}).get("name", "")).casefold()
        for blocked_side, prerequisite_side in (
            ("inward", "outward"),
            ("outward", "inward"),
        ):
            other = link.get(f"{prerequisite_side}Issue")
            if (type_name, blocked_side) in mappings and isinstance(other, dict):
                result.add(issue_key(other))
    return sorted(result)


def external_statuses(
    expected: list[str], issues: list[dict[str, Any]]
) -> dict[str, str]:
    statuses = {issue_key(issue): status_name(issue) for issue in issues}
    if set(statuses) != set(expected):
        missing, unexpected = (
            sorted(set(expected) - set(statuses)),
            sorted(set(statuses) - set(expected)),
        )
        raise ValueError(
            f"Jira external dependency results mismatch: missing={missing}, unexpected={unexpected}"
        )
    return dict(sorted(statuses.items()))


def priority_rank(issue: dict[str, Any], priority_order: list[str]) -> int | None:
    value = (issue.get("fields") or {}).get("priority")
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name")
    normalized = str(value or "").strip().casefold()
    ranks = {
        name.strip().casefold(): rank for rank, name in enumerate(priority_order, 1)
    }
    if normalized not in ranks:
        raise ValueError(f"Jira priority {value!r} is absent from jira_priority_order")
    return ranks[normalized]


def sprint_policy_query(project: str, sprint_policy: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
        raise ValueError("configured Jira project must be a canonical project key")
    if sprint_policy.casefold() == "active":
        sprint_clause = "sprint in openSprints()"
    elif sprint_policy.isdigit():
        sprint_clause = f"sprint = {sprint_policy}"
    else:
        escaped = sprint_policy.replace("\\", "\\\\").replace('"', '\\"')
        sprint_clause = f'sprint = "{escaped}"'
    return f'project = "{project}" AND {sprint_clause}'


def subtask_policy_query(parent_keys: list[str]) -> str:
    canonical = sorted({issue_key({"key": key}) for key in parent_keys})
    if not canonical:
        raise ValueError("Jira subtask query requires at least one proven parent key")
    return "parent in (" + ",".join(canonical) + ")"


def verify_issue_policy(
    issue: dict[str, Any], sprint_field: str, project: str, sprint_policy: str
) -> tuple[str, str]:
    key = issue_key(issue)
    if not key.startswith(f"{project}-"):
        raise ValueError(f"Jira issue {key} is outside configured project {project}")
    return sprint_value(issue, sprint_field, sprint_policy)


def build_inventory(
    template: dict[str, Any],
    fetch: Fetch,
    raw_dir: Path,
    *,
    authority: str,
    approved_origin: str,
    fields: list[str],
    sprint_field: str,
    dependency_links: list[dict[str, str]],
    project: str,
    sprint_policy: str,
    priority_order: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    del template
    parent_jql = sprint_policy_query(project, sprint_policy)
    parent_pages, parents = exhaustive(fetch, parent_jql, "parents", raw_dir, fields)
    if not parents:
        raise ValueError(
            "Jira sprint query returned no issues; sprint identity is unproven"
        )
    keys = [issue_key(issue) for issue in parents]
    if len(keys) != len(set(keys)):
        raise ValueError("Jira parent query contains duplicate issues")
    child_jql = subtask_policy_query(keys)
    child_pages, children = exhaustive(fetch, child_jql, "children", raw_dir, fields)
    child_keys = [issue_key(issue) for issue in children]
    if len(child_keys) != len(set(child_keys)):
        raise ValueError("Jira child query contains duplicate issues")
    for child in children:
        key = issue_key(child)
        if not key.startswith(f"{project}-"):
            raise ValueError(f"Jira issue {key} is outside configured project {project}")
    sprints = {
        verify_issue_policy(issue, sprint_field, project, sprint_policy)
        for issue in parents
    }
    if len(sprints) != 1:
        raise ValueError("Jira issues disagree on exact sprint identity")
    sprint_id, sprint_name = next(iter(sprints))
    relations, child_parents = validate_relations(parents, children)
    issue_by_key = {issue_key(issue): issue for issue in parents}
    for child in children:
        key = issue_key(child)
        combined = dict(issue_by_key.get(key, {}))
        combined["fields"] = {
            **(issue_by_key.get(key, {}).get("fields") or {}),
            **(child.get("fields") or {}),
        }
        combined["key"] = key
        issue_by_key[key] = combined
    dependencies = {
        key: sorted(set(dependency_keys(issue, dependency_links)) | set(declared_dependencies(jira_text((issue.get("fields") or {}).get("description")))))
        for key, issue in issue_by_key.items()
    }
    external = sorted(
        {dep for values in dependencies.values() for dep in values} - set(issue_by_key)
    )
    queries = [
        {"kind": "parents", "jql": parent_jql, "fields": fields, "pages": parent_pages},
        {"kind": "children", "jql": child_jql, "fields": fields, "pages": child_pages},
    ]
    dependency_status: dict[str, str] = {}
    if external:
        external_jql, external_fields = (
            "key in (" + ",".join(external) + ")",
            ["key", "status"],
        )
        external_pages, external_issues = exhaustive(
            fetch, external_jql, "external", raw_dir, external_fields
        )
        dependency_status = external_statuses(external, external_issues)
        queries.append(
            {
                "kind": "external",
                "jql": external_jql,
                "fields": external_fields,
                "pages": external_pages,
            }
        )
    tickets = []
    for key in sorted(issue_by_key):
        issue, data = issue_by_key[key], issue_by_key[key].get("fields") or {}
        ticket: dict[str, Any] = {
            "key": key,
            "summary": str(data.get("summary") or "").strip(),
            "status": status_name(issue),
            "priority": priority_rank(issue, priority_order),
            "labels": sorted(
                str(label) for label in (data.get("labels") or []) if str(label)
            ),
            "issue_type": str((data.get("issuetype") or {}).get("name") or "").strip(),
            "is_subtask": (data.get("issuetype") or {}).get("subtask") is True,
            "dependencies": dependencies[key],
            "subtasks": sorted(
                child for child, parent in child_parents.items() if parent == key
            ),
            "url": f"{approved_origin}/browse/{key}"
            if authority == "provider-network"
            else "",
        }
        if "description" in fields:
            ticket["description"] = jira_text(data.get("description"))
        if key in child_parents:
            ticket["parent"] = child_parents[key]
        tickets.append(ticket)
    output = {
        "project": project,
        "sprint": {"id": sprint_id, "name": sprint_name},
        "source_query": parent_jql,
        "subtask_source_query": child_jql,
        "subtask_keys": sorted(child_parents),
        "tickets": tickets,
        "dependency_status": dependency_status,
    }
    artifact = {
        "schema_version": 3,
        "adapter": "jira-rest-v3",
        "authority": authority,
        "approved_origin": approved_origin,
        "queries": queries,
        "relations": relations,
        "child_parents": child_parents,
    }
    return output, artifact


def scalar_config(path: Path, key: str, default: str) -> str:
    if not path.is_file():
        return default
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*([^#\n]+)", path.read_text())
    return match.group(1).strip().strip("\"'") if match else default


def ticket_project_from_config(path: Path) -> str:
    if not path.is_file():
        return ""
    in_ticket = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("ticket:"):
            in_ticket = True
            continue
        if in_ticket and raw and not raw[0].isspace():
            break
        match = re.fullmatch(r"\s+project:\s*([^#]+?)(?:\s+#.*)?", raw)
        if in_ticket and match:
            return match.group(1).strip().strip("\"'")
    return ""


def list_config(path: Path, key: str, default: list[str]) -> list[str]:
    """Read a top-level list through the shared engine parser.

    Block lists, one-line flow lists, and Prettier-wrapped flow lists resolve
    identically; a malformed list fails closed instead of silently defaulting.
    """
    if not path.is_file():
        return list(default)
    engine_path = Path(__file__).with_name("orchestration-engine.py")
    spec = importlib.util.spec_from_file_location("orka_inventory_config_parser", engine_path)
    if spec is None or spec.loader is None:
        raise ValueError("could not load orchestration configuration parser")
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    try:
        parsed = engine.load_simple_yaml(path)
    except engine.EngineError as exc:
        raise ValueError(f"invalid config syntax in {path}: {exc}") from exc
    value = parsed.get(key) if isinstance(parsed, dict) else None
    if not isinstance(value, list):
        return list(default)
    values = [str(item).strip() for item in value if item is not None and str(item).strip()]
    return values or list(default)


def dependency_links_from_config(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return [{"type": "Blocks", "blocked_side": "inward"}]
    lines, in_block, saw_block, result, current = (
        path.read_text().splitlines(),
        False,
        False,
        [],
        None,
    )
    for raw in lines:
        if raw.startswith("sprint_dependency_links:"):
            in_block = saw_block = True
            continue
        if not in_block:
            continue
        if raw and not raw[0].isspace():
            break
        clean = raw.split("#", 1)[0].strip()
        match = re.fullmatch(r"-\s+type:\s*(.+)", clean)
        if match:
            if current:
                result.append(current)
            current = {"type": match.group(1).strip().strip("\"'")}
            continue
        match = re.fullmatch(r"blocked_side:\s*(inward|outward)", clean)
        if match and current is not None:
            current["blocked_side"] = match.group(1)
    if current:
        result.append(current)
    if not saw_block:
        return [{"type": "Blocks", "blocked_side": "inward"}]
    if not result or any(set(item) != {"type", "blocked_side"} for item in result):
        raise ValueError("sprint_dependency_links must define type and blocked_side")
    return result


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-template", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output", required=True)
    return parser


def run_adapter(
    args: argparse.Namespace,
    *,
    fetch_override: Fetch | None = None,
    config_override: Path | None = None,
) -> int:
    template = json.loads(Path(args.inventory_template).read_text())
    config = (config_override or Path(".orchestration/config.yaml")).resolve()
    project = ticket_project_from_config(config).upper()
    sprint_policy = scalar_config(config, "sprint_id", "").strip()
    base_url = scalar_config(config, "jira_base_url", "").strip()
    if not project or not sprint_policy:
        raise ValueError(
            "ticket.project and sprint_id are required canonical Jira policy"
        )
    sprint_field = scalar_config(config, "jira_sprint_field", "sprint")
    fields = required_fields(sprint_field, ["description"])
    links = dependency_links_from_config(config)
    priority_order = list_config(config, "jira_priority_order", DEFAULT_PRIORITY_ORDER)
    raw_dir = Path(args.artifact).resolve().parent / "jira-raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if fetch_override is not None:
        authority, approved = "test-only", "test-only"
        fetch = fetch_override
    else:
        if not base_url:
            raise ValueError("jira_base_url is required canonical Jira policy")
        authority, approved, fetch = (
            "provider-network",
            validate_base_url(base_url),
            network_fetcher(base_url),
        )
    inventory, artifact = build_inventory(
        template,
        fetch,
        raw_dir,
        authority=authority,
        approved_origin=approved,
        fields=fields,
        sprint_field=sprint_field,
        dependency_links=links,
        project=project,
        sprint_policy=sprint_policy,
        priority_order=priority_order,
    )
    artifact_path = Path(args.artifact).resolve()
    write_json(artifact_path, artifact)
    digest = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inventory["fetch_artifact"] = {"path": str(artifact_path), "sha256": digest}
    write_json(Path(args.output), inventory)
    return 0


def run_fixture_adapter(
    argv: list[str], pages: dict[str, list[dict[str, Any]]], config: Path
) -> int:
    """In-process fixture seam; intentionally unreachable from the public CLI."""
    args = parser().parse_args(argv)
    template = json.loads(Path(args.inventory_template).read_text())
    return run_adapter(
        args,
        fetch_override=fixture_fetch(pages, template),
        config_override=config,
    )


def main() -> int:
    return run_adapter(parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
