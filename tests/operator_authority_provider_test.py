#!/usr/bin/env python3
"""Offline conformance tests for the authority-provider contract and local map."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import operator_authority_provider as provider  # noqa: E402


class AuthorityProviderContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = provider.load_contract(
            ROOT / "contracts/operator-authority-provider-v1.json"
        )

    def test_contract_is_closed_and_fail_closed(self) -> None:
        result = provider.validate_contract(self.contract)
        self.assertEqual(result["authority_classes"], 5)

    def test_tampering_cannot_allow_worker_selected_provider(self) -> None:
        altered = json.loads(json.dumps(self.contract))
        altered["discovery"]["worker_configuration_may_select_provider"] = True
        with self.assertRaisesRegex(provider.ContractError, "host-owned"):
            provider.validate_contract(altered)

    def test_tampering_cannot_grant_merge(self) -> None:
        altered = json.loads(json.dumps(self.contract))
        altered["authority_classes"]["budget-extension"] = ["authorize-merge"]
        with self.assertRaisesRegex(provider.ContractError, "unsafe action"):
            provider.validate_contract(altered)

    def test_fixture_provider_negotiates_v1(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/operator-authority-provider-v1.json").read_text()
        )
        result = provider.validate_discovery(fixture["provider"], self.contract)
        self.assertEqual(result["provider_id"], "orka.local-root-helper")
        self.assertEqual(
            set(fixture["scenarios"]),
            {
                "issuance",
                "verification",
                "consumption",
                "revocation",
                "replay",
                "outage",
            },
        )
        self.assertEqual(
            provider.validate_capability_receipt(
                fixture["capability_receipt"],
                provider_id="orka.local-root-helper",
                authority_class="budget-extension",
                repository_id="/srv/example",
                scope={"ticket": "PROJ-17", "ticket_usd": "35.00"},
                action="raise-ticket-budget",
                now=150,
            )["capability_id"],
            "capability-17",
        )

    def test_receipt_cannot_widen_authority_or_scope(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/operator-authority-provider-v1.json").read_text()
        )
        altered = fixture["capability_receipt"]
        altered["authorized_actions"] = ["authorize-merge"]
        with self.assertRaisesRegex(provider.ContractError, "unsafe action"):
            provider.validate_capability_receipt(
                altered,
                provider_id="orka.local-root-helper",
                authority_class="budget-extension",
                repository_id="/srv/example",
                scope={"ticket": "PROJ-17", "ticket_usd": "35.00"},
                action="raise-ticket-budget",
                now=150,
            )

    def test_discovery_rejects_unknown_class_and_missing_v1(self) -> None:
        with self.assertRaisesRegex(provider.ContractError, "unknown authority"):
            provider.validate_discovery(
                {
                    "provider_id": "orka.test",
                    "protocol_versions": [1],
                    "authority_classes": ["authorize-merge"],
                    "issuer_identity": "test",
                },
                self.contract,
            )
        with self.assertRaisesRegex(provider.ContractError, "protocol v1"):
            provider.validate_discovery(
                {
                    "provider_id": "orka.test",
                    "protocol_versions": [2],
                    "authority_classes": ["budget-extension"],
                    "issuer_identity": "test",
                },
                self.contract,
            )

    def test_class_specific_scopes_reject_widening_and_future_receipts(self) -> None:
        fixture = json.loads(
            (ROOT / "tests/fixtures/operator-authority-provider-v1.json").read_text()
        )
        receipt = fixture["capability_receipt"]
        receipt["scope"]["extra"] = True
        with self.assertRaisesRegex(
            provider.ContractError, "missing, widened, or unknown"
        ):
            provider.validate_capability_receipt(
                receipt,
                provider_id="orka.local-root-helper",
                authority_class="budget-extension",
                repository_id="/srv/example",
                scope=receipt["scope"],
                action="raise-ticket-budget",
                now=150,
            )
        receipt.pop("scope")
        receipt["scope"] = {"ticket": "PROJ-17", "ticket_usd": "35.00"}
        receipt["issued_at"] = 151
        with self.assertRaisesRegex(provider.ContractError, "future-issued"):
            provider.validate_capability_receipt(
                receipt,
                provider_id="orka.local-root-helper",
                authority_class="budget-extension",
                repository_id="/srv/example",
                scope=receipt["scope"],
                action="raise-ticket-budget",
                now=150,
            )

    def test_restart_scope_rejects_malformed_dollar_without_leaking_decimal_error(
        self,
    ) -> None:
        allowances = {
            "attempts": 2,
            "model_runs": 2,
            "review_runs": 2,
            "design_rounds": 2,
            "code_rounds": 2,
            "security_rounds": 2,
            "repair_cycles": 2,
            "ticket_usd": "not-a-number",
            "design_usd": "2",
            "implementation_usd": "10",
            "code_review_usd": "3",
            "security_review_usd": "3",
            "progress_baseline_usd": "0",
        }
        with self.assertRaisesRegex(provider.ContractError, "invalid dollar ceiling"):
            provider.validate_scope(
                "restart", {"ticket": "PROJ-17", "allowances": allowances}
            )

    def test_direct_constructor_cannot_select_repository_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "authority"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o755)
            with self.assertRaisesRegex(provider.ContractError, "host-pinned"):
                provider.LocalRootAuthorityProvider(helper=helper)
            with self.assertRaisesRegex(provider.ContractError, "explicit test mode"):
                provider.LocalRootAuthorityProvider(helper=helper, test_mode=True)


class LocalRootHelperConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.helper = ROOT / "host-tools/orchestration-recovery-authority.py"
        self.env = dict(
            os.environ,
            ORCHESTRATION_AUTHORITY_TEST_MODE="1",
            ORCHESTRATION_AUTHORITY_STATE_DIR=str(self.root / "state"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(
        self, *args: str, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.helper), *args],
            input=input,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    def scope(self, kind: str, **extra: object) -> str:
        value: dict[str, object] = {
            "kind": kind,
            "repository": str(self.repo.resolve()),
            "ticket": "PROJ-17",
        }
        value.update(extra)
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def adapter(self) -> provider.LocalRootAuthorityProvider:
        return provider.LocalRootAuthorityProvider.for_test(self.helper, self.env)

    def provider_scope(self, authority_class: str) -> dict[str, object]:
        values: dict[str, object] = {"ticket": "PROJ-17"}
        if authority_class == "budget-extension":
            values["ticket_usd"] = "35.00"
        elif authority_class == "relaunch-extension":
            values["attempts"] = 4
        elif authority_class == "terminal-recovery":
            values["attempt"] = 2
        elif authority_class == "review-repair":
            values = {"pr": "17", "repair_cycles": 2}
        return values

    def test_adapter_executes_trusted_helper_lifecycle_and_pending_revocation(
        self,
    ) -> None:
        adapter = self.adapter()
        contract = provider.load_contract(
            ROOT / "contracts/operator-authority-provider-v1.json"
        )
        discovery = provider.validate_discovery(adapter.discover(), contract)
        self.assertEqual(discovery["provider_id"], "orka.local-root-helper")
        cases = (
            "budget-extension",
            "relaunch-extension",
            "terminal-recovery",
            "review-repair",
        )
        for authority_class in cases:
            with self.subTest(authority_class=authority_class):
                scope = self.provider_scope(authority_class)
                token = adapter.issue(self.repo, authority_class, scope)
                adapter.revoke(self.repo, authority_class, scope)
                with self.assertRaisesRegex(provider.ContractError, "denied consume"):
                    adapter.consume(self.repo, authority_class, scope, token)

                token = adapter.issue(self.repo, authority_class, scope)
                consumed = adapter.consume(self.repo, authority_class, scope, token)
                self.assertEqual(consumed["provider_id"], "orka.local-root-helper")
                if authority_class == "terminal-recovery":
                    self.assertIsNone(adapter.verify(self.repo, authority_class, scope))
                else:
                    self.assertIsNotNone(
                        adapter.verify(self.repo, authority_class, scope)
                    )

    def test_restart_pending_revocation_and_strict_scope(self) -> None:
        allowances = self.root / "allowances.json"
        allowances.write_text(
            json.dumps(
                {
                    "attempts": 2,
                    "model_runs": 2,
                    "review_runs": 2,
                    "design_rounds": 2,
                    "code_rounds": 2,
                    "security_rounds": 2,
                    "repair_cycles": 2,
                    "ticket_usd": "20",
                    "design_usd": "2",
                    "implementation_usd": "10",
                    "code_review_usd": "3",
                    "security_review_usd": "3",
                    "progress_baseline_usd": "0",
                }
            ),
            encoding="utf-8",
        )
        issued = self.invoke(
            "issue-restart",
            "--repository",
            str(self.repo),
            "--ticket",
            "PROJ-17",
            "--allowances",
            str(allowances),
            "--reason",
            "bounded test restart",
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        scope = {"ticket": "PROJ-17", "allowances": json.loads(allowances.read_text())}
        adapter = self.adapter()
        adapter.revoke(self.repo, "restart", scope)
        with self.assertRaisesRegex(provider.ContractError, "denied consume"):
            adapter.consume(self.repo, "restart", scope, issued.stdout.strip())
        with self.assertRaisesRegex(
            provider.ContractError, "missing, widened, or unknown"
        ):
            provider.validate_scope(
                "restart",
                {"ticket": "PROJ-17", "allowances": scope["allowances"], "extra": 1},
            )

    def test_budget_issue_verify_consume_replay_revoke_and_outage(self) -> None:
        issued = self.invoke(
            "issue-budget",
            "--repository",
            str(self.repo),
            "--ticket",
            "PROJ-17",
            "--ceiling-usd",
            "35.00",
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        token = issued.stdout.strip()
        scope = self.scope("budget")
        activated = self.invoke("activate-budget", "--scope", scope, input=token + "\n")
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertEqual(activated.stdout.strip(), "35.00")
        self.assertEqual(
            self.invoke("budget-ceiling", "--scope", scope).stdout.strip(), "35.00"
        )
        self.assertNotEqual(
            self.invoke(
                "activate-budget", "--scope", scope, input=token + "\n"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke(
                "revoke-budget", "--repository", str(self.repo), "--ticket", "PROJ-17"
            ).returncode,
            0,
        )
        self.assertEqual(self.invoke("budget-ceiling", "--scope", scope).returncode, 3)
        missing = dict(
            self.env, ORCHESTRATION_AUTHORITY_STATE_DIR=str(self.root / "missing")
        )
        outage = subprocess.run(
            [str(self.helper), "budget-ceiling", "--scope", scope],
            text=True,
            capture_output=True,
            env=missing,
            check=False,
        )
        self.assertNotEqual(outage.returncode, 0)
        self.assertEqual(outage.stdout, "")

    def test_terminal_recovery_is_exact_scope_and_one_use(self) -> None:
        issued = self.invoke(
            "issue-recovery",
            "--repository",
            str(self.repo),
            "--ticket",
            "PROJ-17",
            "--attempt",
            "2",
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        token = issued.stdout.strip()
        exact = self.scope("recovery", attempt=2)
        wrong = self.scope("recovery", attempt=3)
        self.assertNotEqual(
            self.invoke(
                "consume-recovery", "--scope", wrong, input=token + "\n"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke(
                "consume-recovery", "--scope", exact, input=token + "\n"
            ).returncode,
            0,
        )
        self.assertNotEqual(
            self.invoke(
                "consume-recovery", "--scope", exact, input=token + "\n"
            ).returncode,
            0,
        )

    def test_other_reference_authority_classes_are_bounded_and_revocable(self) -> None:
        relaunch_scope = self.scope("relaunch")
        relaunch = self.invoke(
            "issue-relaunch",
            "--repository",
            str(self.repo),
            "--ticket",
            "PROJ-17",
            "--ceiling-attempts",
            "4",
        )
        self.assertEqual(relaunch.returncode, 0, relaunch.stderr)
        self.assertEqual(
            self.invoke(
                "activate-relaunch", "--scope", relaunch_scope, input=relaunch.stdout
            ).stdout.strip(),
            "4",
        )
        self.assertEqual(
            self.invoke(
                "revoke-relaunch", "--repository", str(self.repo), "--ticket", "PROJ-17"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke("relaunch-ceiling", "--scope", relaunch_scope).returncode, 3
        )

        review_scope = json.dumps(
            {
                "kind": "review-repair",
                "repository": str(self.repo.resolve()),
                "pr": "17",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        review = self.invoke(
            "issue-review-repair",
            "--repository",
            str(self.repo),
            "--pr",
            "17",
            "--ceiling-repair-cycles",
            "2",
            "--reason",
            "bounded test repair",
        )
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertEqual(
            self.invoke(
                "activate-review-repair", "--scope", review_scope, input=review.stdout
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke(
                "revoke-review-repair", "--repository", str(self.repo), "--pr", "17"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke("review-repair-grant", "--scope", review_scope).returncode, 3
        )

        allowances = self.root / "allowances.json"
        allowances.write_text(
            json.dumps(
                {
                    "attempts": 2,
                    "model_runs": 2,
                    "review_runs": 2,
                    "design_rounds": 2,
                    "code_rounds": 2,
                    "security_rounds": 2,
                    "repair_cycles": 2,
                    "ticket_usd": "20",
                    "design_usd": "2",
                    "implementation_usd": "10",
                    "code_review_usd": "3",
                    "security_review_usd": "3",
                    "progress_baseline_usd": "0",
                }
            ),
            encoding="utf-8",
        )
        restart_scope = self.scope("restart")
        restart = self.invoke(
            "issue-restart",
            "--repository",
            str(self.repo),
            "--ticket",
            "PROJ-17",
            "--allowances",
            str(allowances),
            "--reason",
            "bounded test restart",
        )
        self.assertEqual(restart.returncode, 0, restart.stderr)
        self.assertEqual(
            self.invoke(
                "activate-restart", "--scope", restart_scope, input=restart.stdout
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke(
                "revoke-restart", "--repository", str(self.repo), "--ticket", "PROJ-17"
            ).returncode,
            0,
        )
        self.assertEqual(
            self.invoke("restart-grant", "--scope", restart_scope).returncode, 3
        )


if __name__ == "__main__":
    unittest.main()
