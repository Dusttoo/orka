#!/usr/bin/env python3
"""Adversarial tests for the authenticated Jira inventory adapter."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "jira_inventory_fetch", ROOT / "scripts/jira_inventory_fetch.py"
)
assert SPEC and SPEC.loader
jira = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jira)


class JiraInventoryFetchTest(unittest.TestCase):
    def test_base_validation_and_response_origin_are_separate(self) -> None:
        self.assertEqual(
            jira.validate_base_url("https://jira.example/team"),
            "https://jira.example",
        )
        self.assertEqual(
            jira.url_origin("https://jira.example/rest/api/3/search?startAt=1"),
            "https://jira.example",
        )
        with self.assertRaisesRegex(ValueError, "base URL"):
            jira.validate_base_url("https://jira.example/team?redirect=evil")

    def test_network_fetcher_uses_canonical_base_not_environment(self) -> None:
        with (
            mock.patch.dict(
                jira.os.environ,
                {
                    "JIRA_BASE_URL": "https://attacker.example",
                    "JIRA_API_TOKEN": "secret",
                },
            ),
            mock.patch.object(jira, "build_opener") as opener,
        ):
            jira.network_fetcher("https://trusted.example")
        handler = opener.call_args.args[0]
        self.assertEqual(handler.approved_origin, "https://trusted.example")

    def test_network_fetcher_uses_cursor_pagination_without_start_at(self) -> None:
        class Response:
            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self) -> str:
                return self.url

            def read(self) -> bytes:
                return b'{"issues":[],"isLast":true}'

        class Opener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                return Response(request.full_url)

        opener = Opener()
        with (
            mock.patch.dict(jira.os.environ, {"JIRA_API_TOKEN": "secret"}),
            mock.patch.object(jira, "build_opener", return_value=opener),
        ):
            fetch = jira.network_fetcher("https://jira.example")
            fetch("parents", "project = PROJ", 0, 100, "", ["key"])
            fetch("parents", "project = PROJ", 100, 100, "cursor-2", ["key"])
        first = parse_qs(urlparse(opener.urls[0]).query)
        second = parse_qs(urlparse(opener.urls[1]).query)
        self.assertNotIn("startAt", first)
        self.assertNotIn("nextPageToken", first)
        self.assertEqual(second["nextPageToken"], ["cursor-2"])
        self.assertNotIn("startAt", second)

    def test_cross_origin_redirect_is_rejected_before_authorization_can_follow(
        self,
    ) -> None:
        handler = jira.ApprovedOriginRedirectHandler("https://jira.example")
        request = Request(
            "https://jira.example/rest/api/3/search/jql",
            headers={"Authorization": "Bearer secret"},
        )
        with self.assertRaisesRegex(ValueError, "cross-origin redirect"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example/collect",
            )

    def test_same_origin_redirect_preserves_auth_and_cross_origin_never_does(
        self,
    ) -> None:
        handler = jira.ApprovedOriginRedirectHandler("https://jira.example")
        request = Request(
            "https://jira.example/old",
            headers={"Authorization": "Bearer secret", "Accept": "application/json"},
        )
        redirected = handler.redirect_request(
            request, None, 307, "Temporary Redirect", {}, "https://jira.example/new"
        )
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")
        with self.assertRaises(ValueError):
            handler.redirect_request(
                request, None, 307, "Temporary Redirect", {}, "https://evil.example/new"
            )

    def test_inventory_is_derived_from_pages_not_conflicting_template(self) -> None:
        template = {
            "project": "EVIL",
            "sprint": {"id": "forged", "name": "forged"},
            "source_query": "project = PROJ AND sprint = 42",
            "subtask_source_query": "parent in (PROJ-1)",
            "tickets": [{"key": "EVIL-9", "status": "Done", "dependencies": []}],
            "dependency_status": {"EXT-9": "Done"},
        }
        root_issue = {
            "key": "PROJ-1",
            "fields": {
                "summary": "provider summary",
                "status": {"name": "Ready"},
                "priority": {"id": "opaque-99", "name": "High"},
                "sprint": {"id": "42", "name": "Provider Sprint"},
                "subtasks": [{"key": "PROJ-2"}],
                "issuelinks": [
                    {
                        "type": {"name": "Blocks"},
                        "outwardIssue": {"key": "EXT-9"},
                    }
                ],
            },
        }
        child = {
            "startAt": 0,
            "total": 1,
            "isLast": True,
            "issues": [
                {
                    "key": "PROJ-2",
                    "fields": {
                        "summary": "child",
                        "status": {"name": "Ready"},
                        "priority": None,
                        "parent": {"key": "PROJ-1"},
                        "subtasks": [],
                        "issuelinks": [],
                    },
                }
            ],
        }
        parent = {
            "startAt": 0,
            "total": 1,
            "isLast": True,
            "issues": [root_issue],
        }
        external = {
            "startAt": 0,
            "total": 1,
            "isLast": True,
            "issues": [{"key": "EXT-9", "fields": {"status": {"name": "In Progress"}}}],
        }
        pages = {"parents": [parent], "children": [child], "external": [external]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, artifact = jira.build_inventory(
                template,
                jira.fixture_fetch(pages, template),
                root / "raw",
                authority="test-only",
                approved_origin="test-only",
                fields=jira.required_fields("sprint"),
                sprint_field="sprint",
                dependency_links=[{"type": "Blocks", "blocked_side": "inward"}],
                project="PROJ",
                sprint_policy="42",
                priority_order=["Highest", "High", "Medium", "Low", "Lowest"],
            )
        self.assertEqual(output["project"], "PROJ")
        self.assertEqual(output["sprint"], {"id": "42", "name": "Provider Sprint"})
        self.assertEqual(
            [item["key"] for item in output["tickets"]], ["PROJ-1", "PROJ-2"]
        )
        self.assertEqual(output["tickets"][0]["dependencies"], ["EXT-9"])
        self.assertEqual(output["dependency_status"], {"EXT-9": "In Progress"})
        self.assertNotIn("EVIL-9", json.dumps(output))
        self.assertEqual(artifact["authority"], "test-only")
        self.assertEqual(
            artifact["queries"][1]["jql"],
            "parent in (PROJ-1)",
        )

    def test_policy_constructs_queries_and_rejects_wrong_project(self) -> None:
        self.assertEqual(
            jira.sprint_policy_query("PROJ", "42"),
            'project = "PROJ" AND sprint = 42',
        )
        self.assertEqual(
            jira.subtask_policy_query(["PROJ-9", "PROJ-2"]),
            "parent in (PROJ-2,PROJ-9)",
        )
        with self.assertRaisesRegex(ValueError, "outside configured project"):
            jira.verify_issue_policy(
                {"key": "EVIL-1", "fields": {"sprint": {"id": "42", "name": "S"}}},
                "sprint",
                "PROJ",
                "42",
            )

    def test_current_sprint_selected_while_history_is_tolerated(self) -> None:
        issue = {
            "key": "PROJ-1",
            "fields": {
                "sprint": [
                    {"id": "41", "name": "Old", "state": "closed"},
                    {"id": "42", "name": "Current", "state": "active"},
                ]
            },
        }
        self.assertEqual(
            jira.sprint_value(issue, "sprint", "active"), ("42", "Current")
        )

    def test_numeric_sprint_policy_matches_only_the_provider_id(self) -> None:
        issue = {
            "key": "PROJ-1",
            "fields": {
                "sprint": [
                    {"id": "41", "name": "42", "state": "closed"},
                    {"id": "42", "name": "Current", "state": "active"},
                ]
            },
        }
        self.assertEqual(jira.sprint_value(issue, "sprint", "42"), ("42", "Current"))

    def test_priority_uses_configured_names_not_opaque_ids(self) -> None:
        issue = {"fields": {"priority": {"id": "999", "name": "Highest"}}}
        self.assertEqual(jira.priority_rank(issue, ["Highest", "High"]), 1)
        issue["fields"]["priority"] = {"id": "1", "name": "High"}
        self.assertEqual(jira.priority_rank(issue, ["Highest", "High"]), 2)

    def test_required_fields_are_exactly_the_consumed_surface(self) -> None:
        self.assertEqual(
            jira.required_fields("customfield_10020", ["description", "components"]),
            [
                "key",
                "summary",
                "status",
                "priority",
                "labels",
                "issuetype",
                "subtasks",
                "parent",
                "issuelinks",
                "customfield_10020",
                "description",
            ],
        )

    def test_jira_adf_description_flattens_for_scope_context(self) -> None:
        self.assertEqual(
            jira.jira_text(
                {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "First"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]},
                    ],
                }
            ),
            "First\nSecond",
        )

    def test_non_adjacent_cursor_cycle_and_bounds_fail_closed(self) -> None:
        responses = iter(
            [
                {"startAt": 0, "isLast": False, "nextPageToken": "A", "issues": [{}]},
                {"startAt": 1, "isLast": False, "nextPageToken": "B", "issues": [{}]},
                {"startAt": 2, "isLast": False, "nextPageToken": "A", "issues": [{}]},
            ]
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "cursor"),
        ):
            jira.exhaustive(
                lambda *_: next(responses), "q", "parents", Path(temporary), ["key"]
            )

    def test_page_and_item_bounds_are_falsified_at_the_boundary(self) -> None:
        page_calls = 0

        def page_fetch(*_args):
            nonlocal page_calls
            page_calls += 1
            return {
                "startAt": page_calls - 1,
                "isLast": False,
                "nextPageToken": f"cursor-{page_calls}",
                "issues": [{"key": f"PROJ-{page_calls}"}],
            }

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(jira, "MAX_PAGES", 2),
            self.assertRaisesRegex(ValueError, "page or item bound"),
        ):
            jira.exhaustive(page_fetch, "q", "parents", Path(temporary), ["key"])
        self.assertEqual(page_calls, 2)

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(jira, "MAX_ITEMS", 1),
            self.assertRaisesRegex(ValueError, "item bound"),
        ):
            jira.exhaustive(
                lambda *_: {
                    "startAt": 0,
                    "isLast": True,
                    "issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}],
                },
                "q",
                "parents",
                Path(temporary),
                ["key"],
            )

    def test_public_cli_has_no_fixture_or_base_url_authority_switch(self) -> None:
        parser = jira.parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--inventory-template",
                    "i",
                    "--artifact",
                    "a",
                    "--output",
                    "o",
                    "--test-transport",
                    "x",
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--inventory-template",
                    "i",
                    "--artifact",
                    "a",
                    "--output",
                    "o",
                    "--base-url",
                    "https://evil",
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--inventory-template",
                    "i",
                    "--artifact",
                    "a",
                    "--output",
                    "o",
                    "--config",
                    "attacker.yaml",
                ]
            )

    def test_truncated_total_and_contradictory_relations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary)
            with self.assertRaisesRegex(ValueError, "declared total"):
                jira.exhaustive(
                    lambda *_: {"startAt": 0, "total": 2, "isLast": True, "issues": []},
                    "q",
                    "parents",
                    raw,
                    ["key"],
                )
        parent = {"key": "PROJ-1", "fields": {"subtasks": [{"key": "PROJ-2"}]}}
        child = {"key": "PROJ-2", "fields": {"parent": {"key": "PROJ-999"}}}
        with self.assertRaisesRegex(ValueError, "parent/child"):
            jira.validate_relations([parent], [child])

    def test_external_dependency_response_must_match_requested_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "external dependency"):
            jira.external_statuses(
                ["EXT-9"],
                [{"key": "EXT-10", "fields": {"status": {"name": "Done"}}}],
            )

    def test_evidence_keeps_only_pagination_and_requested_issue_fields(self) -> None:
        page = jira.sanitize_page(
            {
                "startAt": 0,
                "total": 1,
                "isLast": True,
                "expand": "schema,names",
                "warningMessages": ["large"],
                "issues": [
                    {
                        "key": "PROJ-1",
                        "changelog": {"histories": [1]},
                        "fields": {"summary": "small", "description": "discard"},
                    }
                ],
            },
            ["key", "summary"],
        )
        self.assertEqual(set(page), {"startAt", "total", "isLast", "issues"})
        self.assertEqual(page["issues"][0]["fields"], {"summary": "small"})


if __name__ == "__main__":
    unittest.main()
