#!/usr/bin/env python3
"""Create bounded, idempotently discoverable Jira children from a scope assessment."""

from __future__ import annotations

from slice_delivery import decomposition_provenance, validate_delivery, validate_owner

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, build_opener

from api_agent import load_yaml
from jira_inventory_fetch import (
    ApprovedOriginRedirectHandler,
    url_origin,
    validate_base_url,
    write_json,
)


class DecompositionError(RuntimeError):
    pass


def status_names(config: dict[str, Any], key: str, default: list[str]) -> list[str]:
    values = config.get(key, default)
    if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise DecompositionError(f"{key} must be a nonempty list of status names")
    return [value.strip().casefold() for value in values]


def ready_transition_path(
    config: dict[str, Any],
    *,
    ready: list[str],
    done: list[str],
    blocked: list[str],
) -> list[str]:
    """Return a bounded, explicitly approved Jira status path."""
    feature = config.get("sprint_decomposition") or {}
    if not isinstance(feature, dict):
        raise DecompositionError("sprint_decomposition must be a map")
    values = feature.get("jira_ready_transition_path")
    if values is None:
        return []
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= 5
        or any(not isinstance(value, str) or not value.strip() for value in values)
    ):
        raise DecompositionError(
            "jira_ready_transition_path must contain 1 through 5 status names"
        )
    path = [value.strip() for value in values]
    normalized = [value.casefold() for value in path]
    if len(set(normalized)) != len(normalized):
        raise DecompositionError("jira_ready_transition_path must not repeat a status")
    if normalized[-1] not in ready:
        raise DecompositionError(
            "jira_ready_transition_path must end in a configured sprint_ready_status"
        )
    if any(value in ready for value in normalized[:-1]):
        raise DecompositionError(
            "only the final jira_ready_transition_path status may be launchable"
        )
    if any(value in done or value in blocked for value in normalized):
        raise DecompositionError(
            "jira_ready_transition_path must not include done or blocked statuses"
        )
    return path


def auth_headers() -> dict[str, str]:
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not token:
        raise DecompositionError("JIRA_API_TOKEN is required")
    email = os.environ.get("JIRA_EMAIL", "")
    authorization = (
        "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        if email
        else "Bearer " + token
    )
    return {
        "Accept": "application/json",
        "Authorization": authorization,
        "Content-Type": "application/json",
    }


def adf(slice_: dict[str, Any], parent: str) -> dict[str, Any]:
    paragraphs = [
        f"Automatically decomposed from {parent}.",
        "Behavior: " + str(slice_["behavior"]).strip(),
        "Migration owner: " + slice_["migration_owner"],
        "Test plan:",
        *[f"- {value.strip()}" for value in slice_["test_plan"]],
        "Acceptance criteria:",
        *[f"- {value.strip()}" for value in slice_["acceptance_criteria"]],
    ]
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": paragraph}],
            }
            for paragraph in paragraphs
        ],
    }


class Jira:
    def __init__(self, base_url: str) -> None:
        self.origin = validate_base_url(base_url)
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = auth_headers()
        self.opener = build_opener(ApprovedOriginRedirectHandler(self.origin))

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        endpoint = urljoin(self.base_url, path.lstrip("/"))
        if url_origin(endpoint) != self.origin:
            raise DecompositionError("Jira request escaped the configured origin")
        request = Request(
            endpoint,
            data=(json.dumps(body).encode() if body is not None else None),
            headers=self.headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if url_origin(response.geturl()) != self.origin:
                    raise DecompositionError("Jira response escaped the configured origin")
                payload = response.read()
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise DecompositionError(f"Jira {method} {path} failed ({exc.code}): {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DecompositionError(f"Jira {method} {path} outcome is uncertain: {exc}") from exc

    def find_child(
        self,
        parent: str,
        label: str,
        *,
        issue_type: str,
        slice_: dict[str, Any],
        source: str,
    ) -> str | None:
        jql = f'parent = {parent} AND labels = "{label}"'
        query = urlencode({
            "jql": jql,
            "fields": "key,parent,issuetype,summary,description,labels",
            "maxResults": 2,
        })
        result = self.request("GET", "rest/api/3/search/jql?" + query)
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            raise DecompositionError("Jira child lookup returned an invalid issue list")
        if len(issues) > 1:
            raise DecompositionError(f"multiple Jira children carry idempotency label {label}")
        if not issues:
            return None
        issue = issues[0]
        key = str(issue.get("key") or "").upper()
        fields = issue.get("fields") or {}
        actual_type = fields.get("issuetype") or {}
        expected_labels = {
            "orchestration-slice",
            label,
            decomposition_provenance(source, slice_),
        }
        valid = (
            re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key)
            and str((fields.get("parent") or {}).get("key") or "").upper() == parent
            and actual_type.get("subtask") is True
            and str(actual_type.get("name") or "").casefold() == issue_type.casefold()
            and str(fields.get("summary") or "").strip() == str(slice_["summary"]).strip()
            and fields.get("description") == adf(slice_, source)
            and expected_labels <= set(fields.get("labels") or [])
        )
        if not valid:
            raise DecompositionError(
                f"Jira issue {key or '<unknown>'} collides with {label} but does not match its exact Orka slice identity"
            )
        return key

    def decomposition_parent(self, source: str, mode: str) -> str:
        """Return the Jira parent that can legally own generated slices."""
        if mode not in {"sibling", "child"}:
            raise DecompositionError(
                "jira_subtask_decomposition_mode must be sibling or child"
            )
        result = self.request(
            "GET", f"rest/api/3/issue/{source}?fields=issuetype,parent"
        )
        fields = result.get("fields") or {}
        issue_type = fields.get("issuetype") or {}
        is_subtask = issue_type.get("subtask") is True
        if not is_subtask:
            return source
        if mode == "child":
            raise DecompositionError(
                f"source {source} is already a Jira subtask; nested subtasks are unsupported"
            )
        parent = str((fields.get("parent") or {}).get("key") or "").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", parent):
            raise DecompositionError(
                f"source subtask {source} has no authoritative parent"
            )
        return parent

    def create_child(
        self,
        *,
        project: str,
        parent: str,
        issue_type: str,
        slice_: dict[str, Any],
        label: str,
        source: str | None = None,
    ) -> str:
        body = {
            "fields": {
                "project": {"key": project},
                "parent": {"key": parent},
                "issuetype": {"name": issue_type},
                "summary": str(slice_["summary"]).strip(),
                "description": adf(slice_, source or parent),
                "labels": [
                    "orchestration-slice",
                    label,
                    decomposition_provenance(source or parent, slice_),
                ],
            }
        }
        try:
            result = self.request("POST", "rest/api/3/issue", body)
        except DecompositionError as exc:
            # POST may have been accepted before a transport failure. Re-query
            # the deterministic label before deciding whether a retry is safe.
            recovered = self.find_child(
                parent,
                label,
                issue_type=issue_type,
                slice_=slice_,
                source=source or parent,
            )
            if recovered:
                return recovered
            raise exc
        key = str(result.get("key") or "").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
            raise DecompositionError("Jira create response omitted a valid child key")
        return key

    def issue_links(self, key: str) -> list[dict[str, Any]]:
        result = self.request("GET", f"rest/api/3/issue/{key}?fields=issuelinks")
        links = ((result.get("fields") or {}).get("issuelinks") or [])
        if not isinstance(links, list):
            raise DecompositionError(f"Jira issue {key} returned invalid links")
        return links

    def ensure_dependency(
        self, *, blocked: str, prerequisite: str, link_type: str, blocked_side: str
    ) -> bool:
        if blocked_side not in {"inward", "outward"}:
            raise DecompositionError("dependency blocked_side must be inward or outward")

        def exists() -> bool:
            other_side = "outward" if blocked_side == "inward" else "inward"
            for link in self.issue_links(blocked):
                type_name = str((link.get("type") or {}).get("name") or "")
                other = str((link.get(f"{other_side}Issue") or {}).get("key") or "").upper()
                own = str((link.get(f"{blocked_side}Issue") or {}).get("key") or "").upper()
                # GET issue returns the counterpart only; full link records
                # may also include the current issue. Match canonical policy.
                if type_name.casefold() == link_type.casefold() and other == prerequisite and own in {"", blocked}:
                    return True
            return False

        if exists():
            return False
        body: dict[str, Any] = {"type": {"name": link_type}}
        if blocked_side == "inward":
            body.update({"inwardIssue": {"key": blocked}, "outwardIssue": {"key": prerequisite}})
        else:
            body.update({"outwardIssue": {"key": blocked}, "inwardIssue": {"key": prerequisite}})
        try:
            self.request("POST", "rest/api/3/issueLink", body)
        except DecompositionError as exc:
            if exists():
                return False
            raise exc
        return True


    def ensure_ready(self, key: str, config: dict[str, Any]) -> dict[str, Any]:
        """Move an untouched child to a permitted ready status, verifying the result."""
        ready = status_names(config, "sprint_ready_statuses", ["Ready", "To Do", "Open", "Selected for Development"])
        done = status_names(config, "sprint_done_statuses", ["Done", "Closed", "Resolved"])
        blocked = status_names(config, "sprint_blocked_statuses", ["Blocked"])
        configured_path = ready_transition_path(
            config, ready=ready, done=done, blocked=blocked
        )
        normalized_path = [value.casefold() for value in configured_path]

        def status() -> dict[str, Any]:
            value = (self.request("GET", f"rest/api/3/issue/{key}?fields=status").get("fields") or {}).get("status")
            if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"]:
                raise DecompositionError(f"Jira issue {key} has no verified status")
            return value

        current = status()
        name = current["name"].casefold()
        if name in ready or name in done:
            return {"key": key, "status": current["name"], "transitioned": False}
        permitted_intermediate = name in normalized_path[:-1]
        if name in blocked or (
            (current.get("statusCategory") or {}).get("key") != "new"
            and not permitted_intermediate
        ):
            raise DecompositionError(f"child {key} is {current['name']}; preserving its existing workflow state")

        if configured_path:
            start = normalized_path.index(name) + 1 if name in normalized_path else 0
            targets = configured_path[start:]
            transitioned = False
            observed = current
            for target_name in targets:
                target = target_name.casefold()
                result = self.request(
                    "GET",
                    f"rest/api/3/issue/{key}/transitions?expand=transitions.fields",
                )
                transitions = result.get("transitions")
                if not isinstance(transitions, list):
                    raise DecompositionError(f"child {key} has no verified transition list")
                candidates = []
                for transition in transitions:
                    if not isinstance(transition, dict) or not isinstance(transition.get("to"), dict):
                        raise DecompositionError(f"child {key} has malformed transition metadata")
                    if str((transition.get("to") or {}).get("name") or "").casefold() != target:
                        continue
                    fields = transition.get("fields", {})
                    if not isinstance(fields, dict) or any(
                        not isinstance(field, dict) for field in fields.values()
                    ):
                        raise DecompositionError(f"child {key} has malformed transition field metadata")
                    if any(
                        field.get("required") and field.get("hasDefaultValue") is not True
                        for field in fields.values()
                    ):
                        continue
                    identifier = str(transition.get("id") or "")
                    if re.fullmatch(r"[0-9]+", identifier):
                        candidates.append(identifier)
                if not candidates:
                    raise DecompositionError(
                        f"child {key} has no transition to {target_name} without missing required fields"
                    )
                identifier = min(candidates, key=int)
                try:
                    self.request(
                        "POST",
                        f"rest/api/3/issue/{key}/transitions",
                        {"transition": {"id": identifier}},
                    )
                    observed = status()
                except DecompositionError:
                    # Reconcile an uncertain POST before deciding whether the
                    # exact configured step may continue.
                    observed = status()
                    if observed["name"].casefold() != target:
                        raise
                if observed["name"].casefold() != target:
                    raise DecompositionError(
                        f"child {key} did not reach configured path status {target_name}"
                    )
                transitioned = True
            if observed["name"].casefold() not in ready:
                raise DecompositionError(f"child {key} did not reach a configured ready status")
            return {"key": key, "status": observed["name"], "transitioned": transitioned}

        result = self.request("GET", f"rest/api/3/issue/{key}/transitions?expand=transitions.fields")
        transitions = result.get("transitions")
        if not isinstance(transitions, list):
            raise DecompositionError(f"child {key} has no verified transition list")
        candidates = []
        for transition in transitions:
            if not isinstance(transition, dict) or not isinstance(transition.get("to"), dict):
                raise DecompositionError(f"child {key} has malformed transition metadata")
            target = str((transition.get("to") or {}).get("name") or "").casefold()
            fields = transition.get("fields", {})
            if target not in ready or not isinstance(fields, dict):
                continue
            if any(not isinstance(field, dict) for field in fields.values()):
                raise DecompositionError(f"child {key} has malformed transition field metadata")
            if any(field.get("required") and field.get("hasDefaultValue") is not True for field in fields.values()):
                continue
            identifier = str(transition.get("id") or "")
            if re.fullmatch(r"[0-9]+", identifier):
                candidates.append((ready.index(target), identifier))
        if not candidates:
            raise DecompositionError(f"child {key} has no ready transition without missing required fields")
        _, identifier = min(candidates)
        try:
            self.request("POST", f"rest/api/3/issue/{key}/transitions", {"transition": {"id": identifier}})
        except DecompositionError:
            # A timed-out POST may already have transitioned. Do not repeat it
            # without an authoritative status check.
            if status()["name"].casefold() not in ready:
                raise
        final = status()
        if final["name"].casefold() not in ready:
            raise DecompositionError(f"child {key} did not reach a configured ready status")
        return {"key": key, "status": final["name"], "transitioned": True}


def validated_input(config: dict[str, Any], assessment: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    feature = config.get("sprint_decomposition") or {}
    if not isinstance(feature, dict):
        raise DecompositionError("sprint_decomposition must be a map")
    if feature.get("auto_decompose_large_tickets") is not True:
        raise DecompositionError("automatic decomposition is not enabled by repository policy")
    ready = status_names(config, "sprint_ready_statuses", ["Ready", "To Do", "Open", "Selected for Development"])
    done = status_names(config, "sprint_done_statuses", ["Done", "Closed", "Resolved"])
    blocked = status_names(config, "sprint_blocked_statuses", ["Blocked"])
    ready_transition_path(config, ready=ready, done=done, blocked=blocked)
    if assessment.get("schema_version") != 1 or assessment.get("verdict") != "decompose":
        raise DecompositionError("assessment must be a schema-v1 decompose verdict")
    ticket_policy = config.get("ticket") or {}
    if not isinstance(ticket_policy, dict) or ticket_policy.get("kind") != "jira":
        raise DecompositionError("automatic decomposition requires ticket.kind jira")
    parent = str(assessment.get("ticket") or "").upper()
    project = str((ticket_policy.get("project") or "")).upper()
    if (
        not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", parent)
        or not project
        or not parent.startswith(project + "-")
    ):
        raise DecompositionError("assessment ticket and configured Jira project are required")
    slices = assessment.get("slices")
    maximum = min(int(feature.get("max_auto_slices", 6)), 10)
    if not isinstance(slices, list) or not 2 <= len(slices) <= maximum:
        raise DecompositionError(f"assessment must contain 2 through {maximum} slices")
    score = assessment.get("complexity_score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise DecompositionError("assessment complexity must be from 0 through 100")
    identifiers: set[str] = set()
    for item in slices:
        if not isinstance(item, dict):
            raise DecompositionError("every decomposition slice must be an object")
        identifier = str(item.get("id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", identifier) or identifier in identifiers:
            raise DecompositionError(f"invalid or duplicate slice id: {identifier!r}")
        identifiers.add(identifier)
        if not str(item.get("summary") or "").strip() or not str(item.get("behavior") or "").strip():
            raise DecompositionError(f"slice {identifier} requires summary and behavior")
        if len(str(item["summary"])) > 255 or len(str(item["behavior"])) > 8000:
            raise DecompositionError(f"slice {identifier} exceeds Jira field limits")
        validate_delivery(item, DecompositionError)
        criteria = item.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria or any(
            not isinstance(value, str) or not value.strip() for value in criteria
        ):
            raise DecompositionError(f"slice {identifier} requires acceptance criteria")
        if len(criteria) > 30 or any(len(value) > 2000 for value in criteria):
            raise DecompositionError(f"slice {identifier} acceptance criteria exceed limits")
    for item in slices:
        validate_owner(item, identifiers, DecompositionError)
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or item["id"] in dependencies:
            raise DecompositionError(f"slice {item['id']} has invalid dependencies")
        unknown = set(dependencies) - identifiers
        if unknown:
            raise DecompositionError(
                f"slice {item['id']} depends on unknown slices: {', '.join(sorted(unknown))}"
            )
    graph = {item["id"]: set(item.get("depends_on", [])) for item in slices}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise DecompositionError("decomposition dependencies contain a cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in graph[identifier]:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in graph:
        visit(identifier)
    return project, parent, slices, feature


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--assessment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load_yaml(Path(args.config).resolve())
    assessment = json.loads(Path(args.assessment).read_text(encoding="utf-8"))
    project, parent, slices, feature = validated_input(config, assessment)
    labels = {
        item["id"]: f"orchestration-slice-{parent.casefold()}-{item['id']}"
        for item in slices
    }
    output: dict[str, Any] = {
        "schema_version": 1,
        "parent": parent,
        "mode": "apply" if args.apply else "plan",
        "slices": [
            {"id": item["id"], "summary": item["summary"], "label": labels[item["id"]]}
            for item in slices
        ],
    }
    if args.apply:
        base_url = str(config.get("jira_base_url") or "")
        jira = Jira(base_url)
        issue_type = str(feature.get("jira_child_issue_type") or "Sub-task")
        placement_parent = jira.decomposition_parent(
            parent, str(feature.get("jira_subtask_decomposition_mode") or "sibling")
        )
        output["placement_parent"] = placement_parent
        keys: dict[str, str] = {}
        for item in slices:
            label = labels[item["id"]]
            keys[item["id"]] = jira.find_child(
                placement_parent,
                label,
                issue_type=issue_type,
                slice_=item,
                source=parent,
            ) or jira.create_child(
                project=project,
                parent=placement_parent,
                issue_type=issue_type,
                slice_=item,
                label=label,
                source=parent,
            )
        links = config.get("sprint_dependency_links") or [{"type": "Blocks", "blocked_side": "inward"}]
        link = links[0]
        linked = []
        for item in slices:
            for dependency in item.get("depends_on", []):
                created = jira.ensure_dependency(
                    blocked=keys[item["id"]],
                    prerequisite=keys[dependency],
                    link_type=str(link.get("type") or "Blocks"),
                    blocked_side=str(link.get("blocked_side") or "inward"),
                )
                linked.append({"blocked": keys[item["id"]], "prerequisite": keys[dependency], "created": created})
        output["children"] = [keys[item["id"]] for item in slices]
        output["links"] = linked
        output["readiness"] = []
        output["readiness_blockers"] = []
        for key in output["children"]:
            try:
                output["readiness"].append(jira.ensure_ready(key, config))
            except DecompositionError as exc:
                # Creation/linking succeeded. Preserve those results and let
                # authoritative sync expose this child's blocker independently.
                output["readiness_blockers"].append({"key": key, "reason": str(exc)})
    destination = Path(args.output)
    write_json(destination, output)
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DecompositionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"jira-decomposition: {exc}", file=sys.stderr)
        raise SystemExit(2)
