import sys
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import jira_decomposition as decomposition  # noqa: E402


class JiraDecompositionTests(unittest.TestCase):
    def config(self):
        return {
            "ticket": {"kind": "jira", "project": "PROJ"},
            "jira_base_url": "https://jira.example",
            "sprint_decomposition": {
                "auto_decompose_large_tickets": True,
                "max_auto_slices": 4,
                "jira_child_issue_type": "Sub-task",
            },
        }

    def assessment(self):
        return {
            "schema_version": 1,
            "ticket": "PROJ-1",
            "verdict": "decompose",
            "complexity_score": 80,
            "reasons": ["two release boundaries"],
            "slices": [
                {
                    "id": "foundation",
                    "summary": "Foundation",
                    "behavior": "create the additive foundation",
                    "migration_owner": "foundation",
                    "test_plan": ["run the slice regression test"],
                    "acceptance_criteria": ["foundation is independently testable"],
                    "depends_on": [],
                },
                {
                    "id": "cutover",
                    "summary": "Cutover",
                    "behavior": "activate the foundation",
                    "migration_owner": "foundation",
                    "test_plan": ["run the slice regression test"],
                    "acceptance_criteria": ["cutover preserves compatibility"],
                    "depends_on": ["foundation"],
                },
            ],
        }

    def test_delivery_fields_are_required_and_owner_must_exist(self):
        for field in ("migration_owner", "test_plan"):
            value = self.assessment()
            del value["slices"][0][field]
            with self.assertRaises(decomposition.DecompositionError):
                decomposition.validated_input(self.config(), value)

    def test_accepts_bounded_acyclic_slices(self):
        project, parent, slices, feature = decomposition.validated_input(
            self.config(), self.assessment()
        )
        self.assertEqual((project, parent), ("PROJ", "PROJ-1"))
        self.assertEqual([item["id"] for item in slices], ["foundation", "cutover"])
        self.assertEqual(feature["max_auto_slices"], 4)

    def test_structural_decomposition_does_not_require_threshold_score(self):
        value = self.assessment()
        value["complexity_score"] = 62
        self.assertEqual(decomposition.validated_input(self.config(), value)[1], "PROJ-1")

    def test_subtask_slices_are_created_as_siblings(self):
        jira = object.__new__(decomposition.Jira)
        jira.request = Mock(return_value={"fields": {
            "issuetype": {"subtask": True}, "parent": {"key": "PROJ-9"}
        }})
        self.assertEqual(jira.decomposition_parent("PROJ-1", "sibling"), "PROJ-9")

    def test_subtask_without_parent_fails_closed(self):
        jira = object.__new__(decomposition.Jira)
        jira.request = Mock(return_value={"fields": {"issuetype": {"subtask": True}}})
        with self.assertRaisesRegex(decomposition.DecompositionError, "no authoritative parent"):
            jira.decomposition_parent("PROJ-1", "sibling")

    def test_existing_child_requires_exact_type_content_and_provenance(self):
        slice_ = self.assessment()["slices"][0]
        label = "orchestration-slice-proj-1-foundation"
        expected = {
            "key": "PROJ-2",
            "fields": {
                "parent": {"key": "PROJ-9"},
                "issuetype": {"name": "Sub-task", "subtask": True},
                "summary": "Foundation",
                "description": decomposition.adf(slice_, "PROJ-1"),
                "labels": [
                    "orchestration-slice",
                    label,
                    decomposition.decomposition_provenance("PROJ-1", slice_),
                ],
            },
        }
        jira = object.__new__(decomposition.Jira)
        jira.request = Mock(return_value={"issues": [expected]})
        self.assertEqual(
            jira.find_child(
                "PROJ-9", label, issue_type="Sub-task", slice_=slice_, source="PROJ-1"
            ),
            "PROJ-2",
        )
        expected["fields"]["summary"] = "Unrelated work"
        with self.assertRaisesRegex(decomposition.DecompositionError, "collides"):
            jira.find_child(
                "PROJ-9", label, issue_type="Sub-task", slice_=slice_, source="PROJ-1"
            )

    def test_rejects_cycles(self):
        value = self.assessment()
        value["slices"][0]["depends_on"] = ["cutover"]
        with self.assertRaisesRegex(decomposition.DecompositionError, "cycle"):
            decomposition.validated_input(self.config(), value)

    def test_requires_explicit_repository_opt_in(self):
        config = self.config()
        config["sprint_decomposition"]["auto_decompose_large_tickets"] = False
        with self.assertRaisesRegex(decomposition.DecompositionError, "not enabled"):
            decomposition.validated_input(config, self.assessment())

    def test_adf_preserves_behavior_and_acceptance_criteria(self):
        value = decomposition.adf(self.assessment()["slices"][0], "PROJ-1")
        text = [block["content"][0]["text"] for block in value["content"]]
        self.assertIn("Automatically decomposed from PROJ-1.", text)
        self.assertIn("- foundation is independently testable", text)

    def test_dependency_idempotency_requires_the_configured_direction(self):
        jira = object.__new__(decomposition.Jira)
        calls = []
        jira.issue_links = lambda _key: [
            {
                "type": {"name": "Blocks"},
                "outwardIssue": {"key": "PROJ-2"},
                "inwardIssue": {"key": "PROJ-1"},
            }
        ]
        jira.request = lambda method, path, body=None: calls.append((method, path, body)) or {}
        self.assertFalse(
            jira.ensure_dependency(
                blocked="PROJ-1",
                prerequisite="PROJ-2",
                link_type="Blocks",
                blocked_side="inward",
            )
        )
        self.assertEqual(calls, [])

        self.assertTrue(
            jira.ensure_dependency(
                blocked="PROJ-1",
                prerequisite="PROJ-2",
                link_type="Blocks",
                blocked_side="outward",
            )
        )
        self.assertEqual(calls[0][0:2], ("POST", "rest/api/3/issueLink"))

    def test_dependency_lookup_accepts_jira_counterpart_only_records(self):
        jira = object.__new__(decomposition.Jira)
        jira.issue_links = lambda _key: [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-2"}}]
        jira.request = Mock()
        self.assertFalse(jira.ensure_dependency(blocked="PROJ-1", prerequisite="PROJ-2", link_type="Blocks", blocked_side="inward"))
        jira.request.assert_not_called()

    @staticmethod
    def status(name, category="new"):
        return {"fields": {"status": {"name": name, "statusCategory": {"key": category}}}}

    def ready_jira(self, *responses):
        jira = object.__new__(decomposition.Jira)
        jira.request = Mock(side_effect=responses)
        return jira

    def test_child_transitions_from_backlog_to_configured_ready_status(self):
        jira = self.ready_jira(
            self.status("Backlog"),
            {"transitions": [{"id": "21", "to": {"name": "Ready"}, "fields": {}}]},
            {}, self.status("Ready"),
        )
        result = jira.ensure_ready("PROJ-2", self.config())
        self.assertTrue(result["transitioned"])
        self.assertEqual(jira.request.call_args_list[2].args, ("POST", "rest/api/3/issue/PROJ-2/transitions", {"transition": {"id": "21"}}))

    def test_readiness_retry_never_reopens_done_or_moves_active_work(self):
        for name, category in (("Ready", "new"), ("Done", "done"), ("In Progress", "indeterminate"), ("Blocked", "new")):
            with self.subTest(name=name):
                jira = self.ready_jira(self.status(name, category))
                if name in {"Ready", "Done"}:
                    self.assertFalse(jira.ensure_ready("PROJ-2", self.config())["transitioned"])
                else:
                    with self.assertRaisesRegex(decomposition.DecompositionError, "preserving"):
                        jira.ensure_ready("PROJ-2", self.config())
                self.assertEqual(jira.request.call_count, 1)

    def test_transition_timeout_reconciles_status_without_duplicate_post(self):
        jira = self.ready_jira(
            self.status("Backlog"), {"transitions": [{"id": "21", "to": {"name": "Ready"}}]},
            decomposition.DecompositionError("timeout"), self.status("Ready"), self.status("Ready"),
        )
        self.assertTrue(jira.ensure_ready("PROJ-2", self.config())["transitioned"])
        self.assertEqual(sum(call.args[0] == "POST" for call in jira.request.call_args_list), 1)

    def test_ready_transition_does_not_invent_required_fields(self):
        jira = self.ready_jira(self.status("Backlog"), {"transitions": [{
            "id": "21", "to": {"name": "Ready"}, "fields": {"customfield_1": {"required": True, "hasDefaultValue": False}},
        }]})
        with self.assertRaisesRegex(decomposition.DecompositionError, "missing required fields"):
            jira.ensure_ready("PROJ-2", self.config())
        self.assertEqual(jira.request.call_count, 2)

    def test_successful_post_requires_observed_ready_status(self):
        jira = self.ready_jira(
            self.status("Backlog"), {"transitions": [{"id": "21", "to": {"name": "Ready"}}]},
            {}, self.status("Backlog"),
        )
        with self.assertRaisesRegex(decomposition.DecompositionError, "did not reach"):
            jira.ensure_ready("PROJ-2", self.config())

    def test_transition_selection_obeys_configured_status_order(self):
        config = self.config()
        config["sprint_ready_statuses"] = ["Selected", "Ready"]
        jira = self.ready_jira(self.status("Backlog"), {"transitions": [
            {"id": "21", "to": {"name": "Ready"}}, {"id": "31", "to": {"name": "Selected"}},
        ]}, {}, self.status("Selected"))
        jira.ensure_ready("PROJ-2", config)
        self.assertEqual(jira.request.call_args_list[2].args[2]["transition"]["id"], "31")

    def test_child_follows_configured_multistep_path_to_ready(self):
        config = self.config()
        config["sprint_ready_statuses"] = ["Ready"]
        config["sprint_decomposition"]["jira_ready_transition_path"] = ["Scoping", "Ready"]
        jira = self.ready_jira(
            self.status("To Do"),
            {"transitions": [{"id": "11", "name": "Start Scoping", "to": {"name": "Scoping"}, "fields": {}}]},
            {}, self.status("Scoping", "indeterminate"),
            {"transitions": [{"id": "22", "name": "Scoping Done", "to": {"name": "Ready"}, "fields": {}}]},
            {}, self.status("Ready"),
        )
        result = jira.ensure_ready("PROJ-2", config)
        self.assertEqual(result, {"key": "PROJ-2", "status": "Ready", "transitioned": True})
        posts = [call.args for call in jira.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual([args[2]["transition"]["id"] for args in posts], ["11", "22"])

    def test_child_resumes_from_configured_intermediate_status(self):
        config = self.config()
        config["sprint_ready_statuses"] = ["Ready"]
        config["sprint_decomposition"]["jira_ready_transition_path"] = ["Scoping", "Ready"]
        jira = self.ready_jira(
            self.status("Scoping", "indeterminate"),
            {"transitions": [{"id": "22", "to": {"name": "Ready"}, "fields": {}}]},
            {}, self.status("Ready"),
        )
        self.assertTrue(jira.ensure_ready("PROJ-2", config)["transitioned"])
        self.assertEqual(jira.request.call_args_list[2].args[2]["transition"]["id"], "22")

    def test_multistep_timeout_reconciles_before_next_step(self):
        config = self.config()
        config["sprint_ready_statuses"] = ["Ready"]
        config["sprint_decomposition"]["jira_ready_transition_path"] = ["Scoping", "Ready"]
        jira = self.ready_jira(
            self.status("To Do"),
            {"transitions": [{"id": "11", "to": {"name": "Scoping"}, "fields": {}}]},
            decomposition.DecompositionError("timeout"), self.status("Scoping", "indeterminate"),
            {"transitions": [{"id": "22", "to": {"name": "Ready"}, "fields": {}}]},
            {}, self.status("Ready"),
        )
        self.assertTrue(jira.ensure_ready("PROJ-2", config)["transitioned"])
        self.assertEqual(sum(call.args[0] == "POST" for call in jira.request.call_args_list), 2)

    def test_multistep_path_rejects_required_intermediate_fields(self):
        config = self.config()
        config["sprint_ready_statuses"] = ["Ready"]
        config["sprint_decomposition"]["jira_ready_transition_path"] = ["Scoping", "Ready"]
        jira = self.ready_jira(self.status("To Do"), {"transitions": [{
            "id": "11", "to": {"name": "Scoping"},
            "fields": {"customfield_1": {"required": True, "hasDefaultValue": False}},
        }]})
        with self.assertRaisesRegex(decomposition.DecompositionError, "no transition to Scoping"):
            jira.ensure_ready("PROJ-2", config)
        self.assertEqual(sum(call.args[0] == "POST" for call in jira.request.call_args_list), 0)

    def test_multistep_path_must_end_ready_and_cannot_repeat(self):
        for path, message in ((["Scoping"], "must end"), (["Scoping", "Scoping", "Ready"], "must not repeat")):
            with self.subTest(path=path):
                config = self.config()
                config["sprint_ready_statuses"] = ["Ready"]
                config["sprint_decomposition"]["jira_ready_transition_path"] = path
                with self.assertRaisesRegex(decomposition.DecompositionError, message):
                    decomposition.validated_input(config, self.assessment())

    def test_multistep_path_cannot_make_an_intermediate_status_launchable(self):
        config = self.config()
        config["sprint_ready_statuses"] = ["Scoping", "Ready"]
        config["sprint_decomposition"]["jira_ready_transition_path"] = ["Scoping", "Ready"]
        with self.assertRaisesRegex(decomposition.DecompositionError, "only the final"):
            decomposition.validated_input(config, self.assessment())

    def test_one_readiness_blocker_preserves_created_children_and_other_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assessment = root / "assessment.json"
            assessment.write_text(json.dumps(self.assessment()))
            output = root / "output.json"
            jira = Mock()
            jira.decomposition_parent.return_value = "PROJ-1"
            jira.find_child.return_value = None
            jira.create_child.side_effect = ["PROJ-2", "PROJ-3"]
            jira.ensure_dependency.return_value = True
            jira.ensure_ready.side_effect = [
                decomposition.DecompositionError("required field needs operator decision"),
                {"key": "PROJ-3", "status": "Ready", "transitioned": True},
            ]
            with patch.object(sys, "argv", ["decompose", "--config", str(root / "config.yaml"), "--assessment", str(assessment), "--output", str(output), "--apply"]), \
                 patch.object(decomposition, "load_yaml", return_value=self.config()), \
                 patch.object(decomposition, "Jira", return_value=jira), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(decomposition.main(), 0)
            result = json.loads(output.read_text())
            self.assertEqual(result["children"], ["PROJ-2", "PROJ-3"])
            self.assertEqual(result["readiness"][0]["key"], "PROJ-3")
            self.assertEqual(result["readiness_blockers"][0]["key"], "PROJ-2")


if __name__ == "__main__":
    unittest.main()
