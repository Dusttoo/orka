#!/usr/bin/env python3
"""Synthetic conformance tests for the redacted runtime observability surface."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_store import RepositoryBinding, TransactionalEventStore  # noqa: E402
import runtime_observability  # noqa: E402


REPOSITORY_ID = "00000000-0000-4000-8000-000000000133"
ACTIVATION_ID = "cutover-observability"
WRITER = "supervisor:synthetic"
NOW = "2026-10-15T12:00:00Z"


class RuntimeObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "orka-state.sqlite3"
        self.store = TransactionalEventStore(self.database, writer_identity=WRITER)
        self.store.bind_repository(
            RepositoryBinding(
                repository_id=REPOSITORY_ID,
                common_directory="/private/SECRET/repository.git",
                object_directory_id="private-object-id",
                policy_ref="refs/heads/main",
                policy_path=".orchestration/config.yaml",
                policy_commit="private-commit",
                policy_blob="private-blob",
                policy_digest="private-policy-digest",
                created_at=NOW,
            ),
            writer_identity=WRITER,
        )
        self.store._database.execute(
            """
            INSERT INTO runtime_cutovers(
                repository_id, activation_id, marker_digest, minimum_version,
                legacy_snapshot_digest, state, activation_generation,
                activated_at, first_authoritative_sequence, rolled_back_at
            ) VALUES (?, ?, 'marker', '1.8.39', 'legacy', 'active', 1, ?, NULL, NULL)
            """,
            (REPOSITORY_ID, ACTIVATION_ID, NOW),
        )
        self.patches = [
            patch.object(
                runtime_observability,
                "runtime_cutover_marker",
                return_value={"activation_id": ACTIVATION_ID},
            ),
            patch.object(
                runtime_observability,
                "repository_identity",
                return_value={"repository_uuid": REPOSITORY_ID},
            ),
            patch.object(
                runtime_observability, "database_path", return_value=self.database
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.store.close()
        self.temporary.cleanup()

    def create(self, ticket: str) -> None:
        self.store.create_job(
            repository_id=REPOSITORY_ID,
            sprint_id="99",
            ticket_id=ticket,
            idempotency_key=f"secret-create:{ticket}",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def running(self, ticket: str) -> None:
        self.create(ticket)
        self.store.reserve_job(
            repository_id=REPOSITORY_ID,
            sprint_id="99",
            ticket_id=ticket,
            expected_version=1,
            attempt_token=f"secret-attempt:{ticket}",
            dispatch_id=f"secret-dispatch:{ticket}",
            execution_unit_id=f"secret-unit:{ticket}",
            supervisor_fence="secret-fence",
            claims=[
                {
                    "claim_key": f"secret-claim:{ticket}",
                    "resource_type": "worktree",
                    "resource_id": f"/private/SECRET/{ticket}",
                    "exclusive": True,
                    "units": 1,
                }
            ],
            idempotency_key=f"secret-reserve:{ticket}",
            writer_identity=WRITER,
            occurred_at=NOW,
        )
        self.store.launch_job(
            attempt_token=f"secret-attempt:{ticket}",
            dispatch_id=f"secret-dispatch:{ticket}",
            execution_unit_id=f"secret-unit:{ticket}",
            supervisor_fence="secret-fence",
            expected_job_version=2,
            idempotency_key=f"secret-launch:{ticket}",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def complete(self, ticket: str, state: str) -> None:
        self.store.complete_job(
            attempt_token=f"secret-attempt:{ticket}",
            dispatch_id=f"secret-dispatch:{ticket}",
            execution_unit_id=f"secret-unit:{ticket}",
            supervisor_fence="secret-fence",
            expected_job_version=3,
            terminal_state=state,
            terminal_envelope_digest="private-terminal-envelope",
            idempotency_key=f"secret-complete:{ticket}",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def reserve_recovery(self, ticket: str) -> None:
        self.store.reserve_job(
            repository_id=REPOSITORY_ID,
            sprint_id="99",
            ticket_id=ticket,
            expected_version=4,
            attempt_token=f"secret-attempt:{ticket}:2",
            dispatch_id=f"secret-dispatch:{ticket}:2",
            execution_unit_id=f"secret-unit:{ticket}:2",
            supervisor_fence="secret-fence",
            claims=[
                {
                    "claim_key": f"secret-claim:{ticket}:2",
                    "resource_type": "worktree",
                    "resource_id": f"/private/SECRET/{ticket}:2",
                    "exclusive": True,
                    "units": 1,
                }
            ],
            idempotency_key=f"secret-reserve:{ticket}:2",
            writer_identity=WRITER,
            occurred_at=NOW,
        )

    def test_event_pages_are_redacted_ordered_and_restart_stable(self) -> None:
        self.running("SECRET-1")
        self.complete("SECRET-1", "recovery_ready")
        first = runtime_observability.event_page(self.root, limit=2)
        second = runtime_observability.event_page(
            self.root, cursor=first["next_cursor"], limit=2
        )
        replay = runtime_observability.event_page(
            self.root, cursor=first["next_cursor"], limit=2
        )

        self.assertEqual(second, replay)
        sequences = [item["sequence"] for item in first["events"] + second["events"]]
        self.assertEqual(sequences, sorted(sequences))
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        encoded = json.dumps([first, second], sort_keys=True)
        for secret in (
            "SECRET-1",
            "secret-attempt",
            "secret-dispatch",
            "secret-fence",
            "private-terminal-envelope",
            "payload_json",
            "aggregate_id",
            "idempotency_key",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(
            set(first["events"][0]),
            {
                "schema",
                "sequence",
                "event_id",
                "repository_binding",
                "occurred_at",
                "transition",
                "subject",
            },
        )

    def test_machine_contract_matches_the_runtime_schemas(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/runtime-observability-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(contract["contract"], "orka.runtime-observability")
        self.assertEqual(contract["version"], 1)
        self.assertEqual(
            contract["events"]["schema"], runtime_observability.EVENT_SCHEMA
        )
        self.assertEqual(
            contract["cursor"]["schema"], runtime_observability.CURSOR_SCHEMA
        )
        self.assertEqual(
            contract["metrics"]["schema"], runtime_observability.METRICS_SCHEMA
        )
        self.assertEqual(
            contract["metrics"]["work_categories"],
            [
                "running",
                "queued",
                "waiting",
                "parked",
                "blocked",
                "recovered",
                "completed",
            ],
        )

    def test_cursor_rejects_tampering_wrong_repository_and_expiry(self) -> None:
        self.store._database.execute(
            "INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('events', 9)"
        )
        self.create("SECRET-2")
        page = runtime_observability.event_page(self.root)
        cursor = page["next_cursor"]
        replacement = "A" if cursor[-1] != "A" else "B"
        with self.assertRaisesRegex(
            runtime_observability.ObservabilityError, "integrity|malformed"
        ):
            runtime_observability.event_page(
                self.root, cursor=cursor[:-1] + replacement
            )
        wrong = runtime_observability._encode_cursor("wrong-repository", 1)
        with self.assertRaisesRegex(
            runtime_observability.ObservabilityError, "different repository"
        ):
            runtime_observability.event_page(self.root, cursor=wrong)
        expired = runtime_observability._encode_cursor(
            page["repository_binding"], 1
        )
        with self.assertRaisesRegex(
            runtime_observability.ObservabilityError, "expired"
        ):
            runtime_observability.event_page(self.root, cursor=expired)

    def test_metrics_use_stable_categories_and_unmetered_desktop_cost(self) -> None:
        self.running("SECRET-RUNNING")
        self.running("SECRET-RECOVERY")
        self.complete("SECRET-RECOVERY", "recovery_ready")
        self.running("SECRET-RECOVERED")
        self.complete("SECRET-RECOVERED", "recovery_ready")
        self.reserve_recovery("SECRET-RECOVERED")
        self.running("SECRET-DONE")
        self.complete("SECRET-DONE", "completed")
        for ticket, state in (
            ("SECRET-PARKED", "operator_action"),
            ("SECRET-BLOCKED", "external_blocked"),
            ("SECRET-WAIT", "retry_wait"),
        ):
            self.store._database.execute(
                "INSERT INTO jobs VALUES (?, '99', ?, ?, 0, NULL, ?)",
                (REPOSITORY_ID, ticket, state, NOW),
            )
        supervisor = {
            "planning": {
                "concurrency_max": 3,
                "provider_holds": [
                    {
                        "state": "rate_limited",
                        "scope": "route",
                        "credential": "DO-NOT-LEAK",
                        "raw_body": "PRIVATE-BODY",
                    }
                ],
                "budget": {
                    "spent_usd": "12.50",
                    "reserved_usd": "1.25",
                    "projected_usd": "13.75",
                    "private_receipt": "DO-NOT-LEAK",
                },
            }
        }
        self.store._database.execute(
            """
            INSERT INTO runtime_documents VALUES (
                ?, 'supervisor', 'primary', 1, ?, ?,
                'private-fence', NULL, ?
            )
            """,
            (
                REPOSITORY_ID,
                json.dumps(supervisor),
                hashlib.sha256(
                    runtime_observability.canonical_json(supervisor).encode("utf-8")
                ).hexdigest(),
                NOW,
            ),
        )

        metrics = runtime_observability.metrics_snapshot(self.root)
        self.assertEqual(metrics["work"]["running"], 2)
        self.assertEqual(metrics["work"]["queued"], 1)
        self.assertEqual(metrics["work"]["waiting"], 1)
        self.assertEqual(metrics["work"]["parked"], 1)
        self.assertEqual(metrics["work"]["blocked"], 1)
        self.assertEqual(metrics["work"]["recovered"], 1)
        self.assertEqual(metrics["work"]["completed"], 1)
        self.assertEqual(metrics["lanes"], {"active": 2, "capacity": 3})
        self.assertEqual(metrics["provider_holds"]["total"], 1)
        self.assertEqual(metrics["cost"]["api"]["spent_usd"], "12.50")
        self.assertEqual(
            metrics["cost"]["desktop_subscription"],
            {"measurement": "unmetered", "spent_usd": None},
        )
        encoded = json.dumps(metrics, sort_keys=True)
        self.assertNotIn("DO-NOT-LEAK", encoded)
        self.assertNotIn("PRIVATE-BODY", encoded)
        self.assertNotIn("SECRET-", encoded)

    def test_metrics_reject_tampered_runtime_document_digest(self) -> None:
        supervisor = {"planning": {"budget": {"spent_usd": "1.00"}}}
        self.store._database.execute(
            """
            INSERT INTO runtime_documents VALUES (
                ?, 'supervisor', 'primary', 1, ?, 'wrong-digest',
                'private-fence', NULL, ?
            )
            """,
            (REPOSITORY_ID, json.dumps(supervisor), NOW),
        )
        with self.assertRaisesRegex(
            runtime_observability.ObservabilityError, "digest is invalid"
        ):
            runtime_observability.metrics_snapshot(self.root)

    def test_jsonl_is_materialized_after_the_read_snapshot(self) -> None:
        self.create("SECRET-JSONL")
        page = runtime_observability.event_page(self.root)
        # The reader connection is closed when event_page returns, so the sole
        # writer can immediately append even if a caller emits records slowly.
        self.create("SECRET-AFTER-SNAPSHOT")
        records = runtime_observability.jsonl_records(page)
        self.assertEqual(records[0]["record"], "metadata")
        self.assertEqual(records[-1]["record"], "cursor")
        self.assertTrue(all(item.get("record") == "event" for item in records[1:-1]))


if __name__ == "__main__":
    unittest.main()
