#!/usr/bin/env python3
"""Offline conformance tests for the external-service adapter contract."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from external_service_adapter import (  # noqa: E402
    AdapterError,
    canonical_digest,
    DeterministicExternalServiceAdapter,
    ExternalServiceCoordinator,
    FileIntentStore,
    MutationUncertainError,
)
from external_service_contract import (  # noqa: E402
    ContractError,
    load_contract,
    validate_contract,
)


class ExternalServiceAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract(
            ROOT / "contracts/external-service-adapter-v1.json"
        )
        self.fixtures = json.loads(
            (ROOT / "tests/fixtures/external-service-adapter-v1.json").read_text()
        )
        self.repository_id = self.fixtures["repository_id"]
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def intent_store(self, name: str = "intents.json") -> FileIntentStore:
        return FileIntentStore(Path(self.temporary.name) / name)

    def coordinator(
        self,
        profile: str,
        **options: object,
    ) -> tuple[ExternalServiceCoordinator, DeterministicExternalServiceAdapter]:
        capabilities = self.fixtures["capability_profiles"][profile]
        now_seconds = options.pop("now_seconds", None)
        adapter = DeterministicExternalServiceAdapter(
            capabilities=capabilities, **options
        )
        coordinator = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=self.intent_store(profile + ".json"),
            allow_test_adapter=True,
            **({"now_seconds": now_seconds} if now_seconds is not None else {}),
        )
        coordinator.negotiate(capabilities)
        return coordinator, adapter

    def test_contract_and_sanitized_provider_fixtures_are_complete(self) -> None:
        summary = validate_contract(self.contract)
        self.assertEqual(summary["contract_id"], self.fixtures["contract_id"])
        self.assertEqual(summary["protocol_version"], self.fixtures["protocol_version"])
        self.assertGreaterEqual(summary["capability_count"], 13)
        self.assertEqual(set(self.contract["compatibility_map"]), {"jira", "github"})
        self.assertEqual(
            set(self.fixtures["capability_coverage"]),
            set(self.contract["capabilities"]),
        )
        serialized = json.dumps(self.fixtures).lower()
        for private_marker in ("breedledger", "dusttoo", "atlassian.net", "api_token"):
            self.assertNotIn(private_marker, serialized)

    def test_contract_rejects_weakened_authority_evidence_and_ambiguity_rules(
        self,
    ) -> None:
        mutations = (
            lambda value: value["authority_boundary"]["forbidden"].remove(
                "authorize_merge"
            ),
            lambda value: value["evidence"].update(vendor_payload_allowed=True),
            lambda value: value["mutations"].update(blind_retry_after_ambiguous=True),
            lambda value: value["mutations"]["intent_required"].remove(
                "idempotency_key"
            ),
            lambda value: value["pagination"].update(incomplete_terminal_page="accept"),
            lambda value: value["pagination"]["page_required"].remove("cursor"),
            lambda value: value["compatibility"].update(
                unknown_required_capabilities="ignore"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(self.contract)
                mutate(changed)
                with self.assertRaises(ContractError):
                    validate_contract(changed)

    def test_missing_capability_fails_before_external_mutation(self) -> None:
        adapter = DeterministicExternalServiceAdapter(
            capabilities={"issues.status.read": 1}
        )
        coordinator = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=self.intent_store("missing-capability.json"),
            allow_test_adapter=True,
        )
        with self.assertRaisesRegex(AdapterError, "before reservation"):
            coordinator.negotiate({"issues.status.transition": 1})
        self.assertEqual(adapter.mutation_calls, 0)

    def test_additive_unknown_capability_does_not_break_known_negotiation(self) -> None:
        capabilities = {
            "issues.status.read": 1,
            "future.optional.evidence": 1,
        }
        adapter = DeterministicExternalServiceAdapter(capabilities=capabilities)
        coordinator = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=self.intent_store("unknown-capability.json"),
            allow_test_adapter=True,
        )
        result = coordinator.negotiate({"issues.status.read": 1})
        self.assertEqual(result["capabilities"], {"issues.status.read": 1})

    def test_normalized_jira_read_is_exactly_bound_and_redacted(self) -> None:
        coordinator, adapter = self.coordinator(
            "jira",
            read_data={"key": "FIX-1", "status": "Ready", "summary": "Fixture"},
        )
        evidence = coordinator.read(
            "issues.status.read",
            operation_key="status:FIX-1",
            request_id="request-status-1",
            external_object={"type": "issue", "id": "FIX-1"},
            query={"fields": ["status"]},
        )
        self.assertEqual(evidence["data"]["status"], "Ready")
        self.assertEqual(evidence["binding"]["repository_id"], self.repository_id)
        self.assertEqual(adapter.read_calls, 1)

        adapter.read_data = {"raw_payload": {"provider": "untrusted"}}
        with self.assertRaisesRegex(AdapterError, "forbidden key raw_payload"):
            coordinator.read(
                "issues.status.read",
                operation_key="status:FIX-2",
                request_id="request-status-2",
                external_object={"type": "issue", "id": "FIX-2"},
                query={"fields": ["status"]},
            )

    def test_mismatched_response_binding_is_rejected(self) -> None:
        class WrongRepositoryAdapter(DeterministicExternalServiceAdapter):
            def read(self, binding, query):
                evidence = super().read(binding, query)
                evidence["binding"]["repository_id"] = "repository:other"
                return evidence

        capabilities = self.fixtures["capability_profiles"]["jira"]
        adapter = WrongRepositoryAdapter(capabilities=capabilities)
        coordinator = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=self.intent_store("wrong-repository.json"),
            allow_test_adapter=True,
        )
        coordinator.negotiate(capabilities)
        with self.assertRaisesRegex(AdapterError, "exact request"):
            coordinator.read(
                "issues.status.read",
                operation_key="status:FIX-1",
                request_id="request-wrong-repository",
                external_object={"type": "issue", "id": "FIX-1"},
                query={"fields": ["status"]},
            )

    def test_inventory_pagination_proves_completeness_and_exact_identity(self) -> None:
        pages = self.fixtures["positive"]["jira_inventory_pages"]
        coordinator, adapter = self.coordinator("jira", pages=pages)
        result = coordinator.read_collection(
            "issues.inventory.read",
            operation_key="sprint:fixture-7",
            request_id="request-inventory-1",
            external_object={"type": "issue-collection", "id": "fixture-7"},
            query={"project": "FIX", "sprint": "fixture-7"},
        )
        self.assertEqual(
            [item["object_identity"]["id"] for item in result["items"]],
            ["FIX-1", "FIX-2"],
        )
        self.assertEqual(result["final_proof"]["page_count"], 2)
        self.assertEqual(result["final_proof"]["item_count"], 2)
        self.assertEqual(result["final_proof"]["completion_basis"], "provider-total")
        self.assertEqual(adapter.read_calls, 2)

    def test_duplicate_identity_and_invalid_final_proof_are_rejected(self) -> None:
        item = self.fixtures["positive"]["jira_inventory_pages"][0][0]
        coordinator, _adapter = self.coordinator("jira", pages=[[item], [item]])
        with self.assertRaisesRegex(AdapterError, "duplicate item identity"):
            coordinator.read_collection(
                "issues.inventory.read",
                operation_key="inventory-duplicate",
                request_id="request-duplicate",
                external_object={"type": "issue-collection", "id": "fixture-7"},
                query={"project": "FIX"},
            )

        class BadProofAdapter(DeterministicExternalServiceAdapter):
            def read_page(self, binding, query, cursor):
                page = super().read_page(binding, query, cursor)
                if page["complete"]:
                    page["final_proof"]["item_count"] += 1
                return page

        capabilities = self.fixtures["capability_profiles"]["jira"]
        bad = BadProofAdapter(capabilities=capabilities, pages=[[item]])
        coordinator = ExternalServiceCoordinator(
            self.contract,
            bad,
            repository_id=self.repository_id,
            intent_store=self.intent_store("bad-proof.json"),
            allow_test_adapter=True,
        )
        coordinator.negotiate(capabilities)
        with self.assertRaisesRegex(AdapterError, "completeness proof"):
            coordinator.read_collection(
                "issues.inventory.read",
                operation_key="inventory-proof",
                request_id="request-proof",
                external_object={"type": "issue-collection", "id": "fixture-7"},
                query={"project": "FIX"},
            )

    def test_non_json_or_non_finite_normalized_data_is_rejected(self) -> None:
        for invalid in ({"value": float("nan")}, {"value": ("tuple",)}):
            with self.subTest(invalid=invalid):
                coordinator, _adapter = self.coordinator("jira", read_data=invalid)
                with self.assertRaisesRegex(
                    AdapterError,
                    "(unknown normalized fields|invalid normalized type|canonical JSON|non-JSON value)",
                ):
                    coordinator.read(
                        "issues.status.read",
                        operation_key="status:FIX-1",
                        request_id="request-invalid-json",
                        external_object={"type": "issue", "id": "FIX-1"},
                        query={"fields": ["status"]},
                    )

    def test_github_pr_ci_and_approval_capabilities_share_one_boundary(self) -> None:
        coordinator, adapter = self.coordinator(
            "github",
            read_data=self.fixtures["positive"]["github_pull_request"],
            pages=[self.fixtures["positive"]["github_ci"]],
        )
        pr = coordinator.read(
            "scm.pull-request.read",
            operation_key="pr:17",
            request_id="request-pr-17",
            external_object={"type": "pull-request", "id": "17"},
            query={"number": 17},
        )
        self.assertEqual(pr["data"]["head"], "1" * 40)
        ci = coordinator.read_collection(
            "scm.ci.read",
            operation_key="ci:17:head",
            request_id="request-ci-17",
            external_object={"type": "pull-request", "id": "17"},
            query={"head": "1" * 40},
        )
        self.assertEqual(len(ci["items"]), 2)
        self.assertEqual(adapter.read_calls, 2)

    def test_every_current_jira_and_github_capability_has_sanitized_fixture_coverage(
        self,
    ) -> None:
        for operation, case in self.fixtures["capability_coverage"].items():
            with self.subTest(operation=operation):
                profile = case["profile"]
                kind = self.contract["capabilities"][operation]["kind"]
                item = case.get("item")
                adapter = DeterministicExternalServiceAdapter(
                    capabilities=self.fixtures["capability_profiles"][profile],
                    read_data=case.get("data"),
                    pages=(
                        [
                            [
                                {
                                    "object_identity": {
                                        "type": item["type"],
                                        "id": item["id"],
                                    },
                                    "data": item["data"],
                                }
                            ]
                        ]
                        if item is not None
                        else None
                    ),
                )
                coordinator = ExternalServiceCoordinator(
                    self.contract,
                    adapter,
                    repository_id=self.repository_id,
                    intent_store=self.intent_store(operation + ".json"),
                    allow_test_adapter=True,
                )
                coordinator.negotiate({operation: 1})
                common = {
                    "operation_key": "fixture:" + operation,
                    "request_id": "request:" + operation,
                    "external_object": case["object"],
                }
                if kind == "read":
                    result = coordinator.read(operation, query=case["query"], **common)
                    self.assertEqual(result["binding"]["operation"], operation)
                elif kind == "collection-read":
                    result = coordinator.read_collection(
                        operation, query=case["query"], **common
                    )
                    self.assertEqual(len(result["items"]), 1)
                else:
                    result = coordinator.mutate(
                        operation,
                        input_value=case["input"],
                        idempotency_key="fixture:" + operation,
                        **common,
                    )
                    self.assertEqual(result["status"], "completed")

    def test_vendor_specific_normalized_fields_are_rejected_recursively(self) -> None:
        coordinator, adapter = self.coordinator("github")
        with self.assertRaisesRegex(AdapterError, "unknown normalized fields"):
            coordinator.mutate(
                "scm.comment.create",
                operation_key="comment:17:vendor",
                request_id="request-vendor-field",
                external_object={"type": "pull-request", "id": "17"},
                input_value={"body": "safe", "vendor_metadata": "not normalized"},
                idempotency_key="comment-vendor-field",
            )
        self.assertEqual(adapter.mutation_calls, 0)

        adapter.read_data = {
            "repository_id": 1042,
            "number": 17,
            "head": "1" * 40,
            "base": "main",
            "state": {"provider_state": "open"},
        }
        with self.assertRaisesRegex(AdapterError, "invalid normalized type"):
            coordinator.read(
                "scm.pull-request.read",
                operation_key="pr:17:vendor",
                request_id="request-nested-vendor-field",
                external_object={"type": "pull-request", "id": "17"},
                query={"number": 17},
            )

    def test_strict_normalized_schemas_reject_missing_and_wrong_typed_fields(
        self,
    ) -> None:
        class EmptyReceiptAdapter(DeterministicExternalServiceAdapter):
            def mutate(self, intent):
                acknowledgement = super().mutate(intent)
                acknowledgement["receipt"] = {}
                body = {
                    key: value
                    for key, value in acknowledgement.items()
                    if key != "acknowledgement_digest"
                }
                acknowledgement["acknowledgement_digest"] = canonical_digest(body)
                return acknowledgement

        adapter = EmptyReceiptAdapter(
            capabilities=self.fixtures["capability_profiles"]["jira"]
        )
        coordinator = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=self.intent_store("empty-receipt.json"),
            allow_test_adapter=True,
        )
        coordinator.negotiate({"issues.status.transition": 1})
        with self.assertRaises(MutationUncertainError) as caught:
            coordinator.mutate(
                "issues.status.transition",
                operation_key="transition:FIX-1:ready",
                request_id="request-empty-receipt",
                external_object={"type": "issue", "id": "FIX-1"},
                input_value={"from": "To Do", "to": "Ready"},
                idempotency_key="empty-receipt",
            )
        self.assertIn(
            "omits required normalized fields", str(caught.exception.__cause__)
        )

        coordinator, _adapter = self.coordinator(
            "github",
            read_data={
                "head": "1" * 40,
                "merge_commit": "2" * 40,
                "merged": "yes",
            },
        )
        with self.assertRaisesRegex(
            AdapterError, "merged has an invalid normalized type"
        ):
            coordinator.read(
                "scm.merge-receipt.read",
                operation_key="merge:17",
                request_id="request-wrong-merged",
                external_object={"type": "merge-receipt", "id": "17"},
                query={"number": 17},
            )

        coordinator, _adapter = self.coordinator(
            "github",
            read_data={"merge_commit": "2" * 40, "merged": True},
        )
        with self.assertRaisesRegex(AdapterError, "omits required normalized fields"):
            coordinator.read(
                "scm.merge-receipt.read",
                operation_key="merge:17",
                request_id="request-missing-head",
                external_object={"type": "merge-receipt", "id": "17"},
                query={"number": 17},
            )

        malformed_item = {
            "object_identity": {"type": "ci-check", "id": "17:unit"},
            "data": {"name": "unit", "status": "completed"},
        }
        coordinator, _adapter = self.coordinator("github", pages=[[malformed_item]])
        with self.assertRaisesRegex(AdapterError, "omits required normalized fields"):
            coordinator.read_collection(
                "scm.ci.read",
                operation_key="ci:17:head",
                request_id="request-bad-item",
                external_object={"type": "pull-request", "id": "17"},
                query={"head": "1" * 40},
            )

    def test_completed_acknowledgement_cannot_change_bound_target(self) -> None:
        for changed_target in (
            {"type": "issue", "id": "FIX-2"},
            {"type": "other-issue", "id": "FIX-1"},
        ):
            with self.subTest(changed_target=changed_target):

                class TargetAdapter(DeterministicExternalServiceAdapter):
                    def mutate(self, intent):
                        acknowledgement = super().mutate(intent)
                        acknowledgement["external_object"] = copy.deepcopy(
                            changed_target
                        )
                        base = {
                            key: value
                            for key, value in acknowledgement.items()
                            if key != "acknowledgement_digest"
                        }
                        acknowledgement["acknowledgement_digest"] = __import__(
                            "external_service_adapter"
                        ).canonical_digest(base)
                        return acknowledgement

                adapter = TargetAdapter(
                    capabilities=self.fixtures["capability_profiles"]["jira"]
                )
                coordinator = ExternalServiceCoordinator(
                    self.contract,
                    adapter,
                    repository_id=self.repository_id,
                    intent_store=self.intent_store(
                        "changed-target-" + changed_target["type"] + ".json"
                    ),
                    allow_test_adapter=True,
                )
                coordinator.negotiate({"issues.status.transition": 1})
                with self.assertRaises(MutationUncertainError) as caught:
                    coordinator.mutate(
                        "issues.status.transition",
                        operation_key="transition:FIX-1:ready",
                        request_id="request-changed-target",
                        external_object={"type": "issue", "id": "FIX-1"},
                        input_value={"from": "To Do", "to": "Ready"},
                        idempotency_key="changed-target-" + changed_target["type"],
                    )
                self.assertIn("not the bound target", str(caught.exception.__cause__))

    def test_completed_mutation_is_idempotency_bound_and_cannot_be_resubmitted(
        self,
    ) -> None:
        coordinator, adapter = self.coordinator("jira")
        result = coordinator.mutate(
            "issues.status.transition",
            operation_key="transition:FIX-1:ready",
            request_id="request-transition-1",
            external_object={"type": "issue", "id": "FIX-1"},
            input_value={"from": "Scoping", "to": "Ready"},
            idempotency_key="transition-FIX-1-ready-v1",
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(adapter.mutation_calls, 1)
        with self.assertRaisesRegex(AdapterError, "conflicts with existing intent"):
            coordinator.mutate(
                "issues.status.transition",
                operation_key="transition:FIX-1:ready",
                request_id="request-transition-retry",
                external_object={"type": "issue", "id": "FIX-1"},
                input_value={"from": "Scoping", "to": "Ready"},
                idempotency_key="transition-FIX-1-ready-v1",
            )
        self.assertEqual(adapter.mutation_calls, 1)

    def test_ambiguous_mutation_requires_bound_reconciliation_not_blind_retry(
        self,
    ) -> None:
        coordinator, adapter = self.coordinator("github", mutation_status="ambiguous")
        result = coordinator.mutate(
            "scm.comment.create",
            operation_key="comment:pr-17:review",
            request_id="request-comment-1",
            external_object={"type": "pull-request", "id": "17"},
            input_value={"body": "Sanitized fixture comment"},
            idempotency_key="comment-pr-17-review-v1",
        )
        self.assertEqual(result["status"], "ambiguous")
        with self.assertRaisesRegex(AdapterError, "requires reconciliation"):
            coordinator.mutate(
                "scm.comment.create",
                operation_key="comment:pr-17:review",
                request_id="request-comment-1",
                external_object={"type": "pull-request", "id": "17"},
                input_value={"body": "Sanitized fixture comment"},
                idempotency_key="comment-pr-17-review-v1",
            )
        reconciled = coordinator.reconcile(result["reconciliation_key"])
        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(adapter.mutation_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_ambiguous_intent_survives_restart_and_can_only_reconcile(self) -> None:
        store = self.intent_store("restart-ambiguous.json")
        capabilities = self.fixtures["capability_profiles"]["github"]
        adapter = DeterministicExternalServiceAdapter(
            capabilities=capabilities, mutation_status="ambiguous"
        )
        first = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=store,
            allow_test_adapter=True,
        )
        first.negotiate({"scm.comment.create": 1})
        ambiguous = first.mutate(
            "scm.comment.create",
            operation_key="comment:pr-17:restart",
            request_id="request-comment-restart",
            external_object={"type": "pull-request", "id": "17"},
            input_value={"body": "Sanitized fixture comment"},
            idempotency_key="comment-pr-17-restart-v1",
        )
        second = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=store,
            allow_test_adapter=True,
        )
        second.negotiate({"scm.comment.create": 1})
        with self.assertRaisesRegex(AdapterError, "conflicts with existing intent"):
            second.mutate(
                "scm.comment.create",
                operation_key="comment:pr-17:restart",
                request_id="request-comment-restart-again",
                external_object={"type": "pull-request", "id": "17"},
                input_value={"body": "Sanitized fixture comment"},
                idempotency_key="comment-pr-17-restart-v1",
            )
        self.assertEqual(adapter.mutation_calls, 1)
        self.assertEqual(
            second.reconcile(ambiguous["reconciliation_key"])["status"], "completed"
        )

    def test_rate_limit_metadata_is_normalized_and_credentials_are_rejected(
        self,
    ) -> None:
        coordinator, _adapter = self.coordinator(
            "github", mutation_status="rate_limited"
        )
        result = coordinator.mutate(
            "scm.pull-request.update",
            operation_key="pr:17:update",
            request_id="request-update-1",
            external_object={"type": "pull-request", "id": "17"},
            input_value={"title": "Fixture title"},
            idempotency_key="pr-17-update-v1",
        )
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["rate_limit"]["retry_after_seconds"], 60)
        with self.assertRaisesRegex(AdapterError, "forbidden key Authorization"):
            coordinator.mutate(
                "scm.pull-request.update",
                operation_key="pr:17:secret",
                request_id="request-update-2",
                external_object={"type": "pull-request", "id": "17"},
                input_value={"Authorization": "secret fixture value"},
                idempotency_key="pr-17-update-v2",
            )

    def test_rate_limited_intent_retries_only_with_same_bound_key_after_deadline(
        self,
    ) -> None:
        clock = [1000.0]
        store = self.intent_store("rate-limit-restart.json")
        capabilities = self.fixtures["capability_profiles"]["github"]
        adapter = DeterministicExternalServiceAdapter(
            capabilities=capabilities, mutation_status="rate_limited"
        )

        def coordinator() -> ExternalServiceCoordinator:
            value = ExternalServiceCoordinator(
                self.contract,
                adapter,
                repository_id=self.repository_id,
                intent_store=store,
                allow_test_adapter=True,
                now_seconds=lambda: clock[0],
            )
            value.negotiate({"scm.pull-request.update": 1})
            return value

        request = {
            "operation_key": "pr:17:update",
            "request_id": "request-rate-limit",
            "external_object": {"type": "pull-request", "id": "17"},
            "input_value": {"title": "Fixture title"},
            "idempotency_key": "pr-17-update-rate-limited",
        }
        first = coordinator()
        self.assertEqual(
            first.mutate("scm.pull-request.update", **request)["status"], "rate_limited"
        )
        self.assertEqual(adapter.mutation_calls, 1)
        with self.assertRaisesRegex(AdapterError, "not eligible for retry"):
            coordinator().mutate("scm.pull-request.update", **request)
        new_key = {**request, "idempotency_key": "pr-17-update-new-key"}
        with self.assertRaisesRegex(AdapterError, "equivalent mutation"):
            coordinator().mutate("scm.pull-request.update", **new_key)

        clock[0] += 60
        adapter.mutation_status = "completed"
        self.assertEqual(
            coordinator().mutate("scm.pull-request.update", **request)["status"],
            "completed",
        )
        self.assertEqual(adapter.mutation_calls, 2)

    def test_atomic_claim_prevents_two_coordinators_submitting_one_intent(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingAdapter(DeterministicExternalServiceAdapter):
            def mutate(self, intent):
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError("test did not release first submission")
                return super().mutate(intent)

        store = self.intent_store("atomic-claim.json")
        capabilities = self.fixtures["capability_profiles"]["jira"]
        adapter = BlockingAdapter(capabilities=capabilities)

        def make_coordinator() -> ExternalServiceCoordinator:
            value = ExternalServiceCoordinator(
                self.contract,
                adapter,
                repository_id=self.repository_id,
                intent_store=store,
                allow_test_adapter=True,
            )
            value.negotiate({"issues.status.transition": 1})
            return value

        request = {
            "operation_key": "transition:FIX-1:ready",
            "request_id": "request-atomic-claim",
            "external_object": {"type": "issue", "id": "FIX-1"},
            "input_value": {"from": "To Do", "to": "Ready"},
            "idempotency_key": "transition-atomic-claim",
        }
        first_error: list[Exception] = []

        def submit_first() -> None:
            try:
                make_coordinator().mutate("issues.status.transition", **request)
            except Exception as exc:  # pragma: no cover - asserted after joining
                first_error.append(exc)

        first = threading.Thread(target=submit_first)
        first.start()
        self.assertTrue(entered.wait(timeout=3))
        with self.assertRaisesRegex(AdapterError, "prior mutation submission"):
            make_coordinator().mutate("issues.status.transition", **request)
        release.set()
        first.join(timeout=3)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(adapter.mutation_calls, 1)

    def test_unacknowledged_submission_survives_restart_and_reconciles(self) -> None:
        class TimeoutAfterSubmissionAdapter(DeterministicExternalServiceAdapter):
            def mutate(self, intent):
                self.mutation_calls += 1
                raise TimeoutError("fixture transport lost after submission")

        store = self.intent_store("unacknowledged-restart.json")
        adapter = TimeoutAfterSubmissionAdapter(
            capabilities=self.fixtures["capability_profiles"]["github"]
        )

        def make_coordinator() -> ExternalServiceCoordinator:
            value = ExternalServiceCoordinator(
                self.contract,
                adapter,
                repository_id=self.repository_id,
                intent_store=store,
                allow_test_adapter=True,
            )
            value.negotiate({"scm.comment.create": 1})
            return value

        request = {
            "operation_key": "comment:pr-17:uncertain",
            "request_id": "request-comment-uncertain",
            "external_object": {"type": "pull-request", "id": "17"},
            "input_value": {"body": "Sanitized fixture comment"},
            "idempotency_key": "comment-pr-17-uncertain-v1",
        }
        with self.assertRaises(MutationUncertainError) as caught:
            make_coordinator().mutate("scm.comment.create", **request)
        reconciliation_key = caught.exception.reconciliation_key
        self.assertTrue(reconciliation_key.startswith("intent-"))

        with self.assertRaisesRegex(AdapterError, "prior mutation submission"):
            make_coordinator().mutate("scm.comment.create", **request)
        self.assertEqual(adapter.mutation_calls, 1)

        reconciled = make_coordinator().reconcile(reconciliation_key)
        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_reconciliation_key_collision_does_not_overwrite_first_intent(self) -> None:
        store = self.intent_store("reconciliation-collision.json")
        adapter = DeterministicExternalServiceAdapter(
            capabilities=self.fixtures["capability_profiles"]["github"],
            mutation_status="ambiguous",
        )
        coordinator = ExternalServiceCoordinator(
            self.contract,
            adapter,
            repository_id=self.repository_id,
            intent_store=store,
            allow_test_adapter=True,
        )
        coordinator.negotiate({"scm.comment.create": 1})
        first = coordinator.mutate(
            "scm.comment.create",
            operation_key="comment:pr-17:first",
            request_id="request-comment-first",
            external_object={"type": "pull-request", "id": "17"},
            input_value={"body": "First sanitized comment"},
            idempotency_key="comment-pr-17-first-v1",
        )
        with self.assertRaises(MutationUncertainError) as caught:
            coordinator.mutate(
                "scm.comment.create",
                operation_key="comment:pr-18:second",
                request_id="request-comment-second",
                external_object={"type": "pull-request", "id": "18"},
                input_value={"body": "Second sanitized comment"},
                idempotency_key="comment-pr-18-second-v1",
            )

        persisted = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["reconcilable"][first["reconciliation_key"]],
            "comment-pr-17-first-v1",
        )
        self.assertEqual(
            persisted["reconcilable"][caught.exception.reconciliation_key],
            "comment-pr-18-second-v1",
        )

    def test_file_store_fsyncs_file_and_parent_directory(self) -> None:
        calls: list[int] = []
        original_fsync = __import__("os").fsync

        def recording_fsync(descriptor: int) -> None:
            calls.append(descriptor)
            original_fsync(descriptor)

        with mock.patch("external_service_adapter.os.fsync", recording_fsync):
            self.intent_store("directory-fsync.json")
        self.assertGreaterEqual(len(calls), 2)

    def test_test_adapter_has_no_scheduler_or_merge_authority(self) -> None:
        capabilities = self.fixtures["capability_profiles"]["github"]
        adapter = DeterministicExternalServiceAdapter(capabilities=capabilities)
        with self.assertRaisesRegex(AdapterError, "explicit conformance mode"):
            ExternalServiceCoordinator(
                self.contract,
                adapter,
                repository_id=self.repository_id,
                intent_store=self.intent_store("test-adapter.json"),
            )
        self.assertFalse(hasattr(adapter, "schedule"))
        self.assertFalse(hasattr(adapter, "reserve_budget"))
        self.assertFalse(hasattr(adapter, "authorize_merge"))


if __name__ == "__main__":
    unittest.main()
