"""Recovery scheduling across failures, budgets, and process lifetimes."""

import argparse
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "resilience_controller", ROOT / "scripts/sprint-controller.py"
)
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = dict(
            shared_root=root,
            state_dir=root / ".orchestration/.sprint-state",
            pause_usd_per_ticket=20,
            warn_usd_per_ticket=10,
            max_model_runs_per_ticket=12,
            max_reviewer_runs_per_ticket=6,
            max_lane_relaunches=2,
            concurrency_max=1,
            max_worker_continuations=6,
            max_unmerged_prs=10,
            auto_decompose_large_tickets=False,
            max_usd_without_progress=5,
            done={"done"},
            ready={"ready"},
            blocked={"blocked"},
        )
        self.state = dict(
            schema_version=2, sprint={"id": "1"}, dependency_status={}, tickets={}
        )

    def ticket(self, key, state="pending", **extra):
        value = dict(
            key=key,
            state=state,
            dependencies=[],
            attempts=0,
            attempt_token="token",
            branch="feature",
            pr="123",
            run_ref="worker",
            reason="test",
            summary="test",
            history=[],
        )
        value.update(extra)
        self.state["tickets"][key] = value
        return value

    def test_cooperative_recovery_requires_opt_in_cleanup_and_absent_group(self):
        receipt = self.cfg["shared_root"] / "terminal.json"
        receipt.write_text(
            json.dumps(
                dict(
                    phase="terminal",
                    invocation_id="launch",
                    cooperative_cleanup=dict(worker_pgid=43210, gateway_closed=True),
                )
            )
        )
        ticket = self.ticket(
            "PROJ-1",
            "recoverable",
            worker_identity=dict(
                kind="execution_unit",
                containment="cooperative-session",
                invocation_id="launch",
                tombstone_path=str(receipt),
            ),
            launch_evidence=dict(
                invocation_id="launch", cooperative_auto_recovery=True
            ),
        )
        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            patch.object(controller.os, "killpg", side_effect=ProcessLookupError),
        ):
            self.assertFalse(controller.automatic_recovery_available(ticket, self.cfg))
            self.cfg["cooperative_auto_recovery"] = True
            self.assertTrue(controller.automatic_recovery_available(ticket, self.cfg))
            self.assertEqual(
                controller.plan_value(self.state, self.cfg)["recovery"], ["PROJ-1"]
            )
            path = controller.state_path(self.cfg["state_dir"], "1")
            path.parent.mkdir(parents=True)
            controller.save(path, self.state)
            with contextlib.redirect_stdout(io.StringIO()):
                controller.requeue(
                    argparse.Namespace(
                        sprint="1",
                        ticket="PROJ-1",
                        attempt_token="token",
                        reason="resume preserved work",
                        operator_capability="",
                    ),
                    self.cfg,
                )
            resumed = controller.load(path)["tickets"]["PROJ-1"]
            self.assertEqual(
                (resumed["state"], resumed["branch"], resumed["pr"]),
                ("pending", "feature", "123"),
            )
        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            patch.object(controller.os, "killpg", return_value=None),
        ):
            self.assertFalse(controller.automatic_recovery_available(ticket, self.cfg))
        receipt.write_text(json.dumps(dict(phase="terminal", invocation_id="other")))
        with patch.object(controller, "execution_unit_status", return_value="absent"):
            self.assertFalse(controller.automatic_recovery_available(ticket, self.cfg))

    def test_design_progress_requires_bound_receipt_and_replay_is_idempotent(self):
        self.ticket("PROJ-1", "running")
        root = self.cfg["shared_root"]
        self.cfg["config"] = root / "config.yaml"
        self.cfg["config"].write_text("llm: {}\n")
        directory = root / ".orchestration/.review-ledger"
        directory.mkdir(parents=True)
        ledger = directory / "subject-PROJ-1.json"
        review = dict(
            work_subject=dict(id="PROJ-2", repository=str(root.resolve())),
            design=dict(
                rounds=[dict(verdict="PASS", result=dict(phase_permit="permit"))]
            ),
            review_permits=[dict(token="permit", receipt_consumed_at="now")],
        )
        ledger.write_text(json.dumps(review))
        path = controller.state_path(self.cfg["state_dir"], "1")
        path.parent.mkdir(parents=True)
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            milestone="design_passed",
            evidence=str(ledger),
            attempt_token="token",
        )
        with self.assertRaisesRegex(controller.SprintError, "another ticket"):
            controller.record_progress(args, self.cfg)
        review["work_subject"]["id"] = "PROJ-1"
        ledger.write_text(json.dumps(review))
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_progress(args, self.cfg)
            controller.record_progress(args, self.cfg)
        progress = controller.load(path)["tickets"]["PROJ-1"]["progress"]
        self.assertEqual(len(progress), 1)
        self.assertTrue(progress[0]["verified"])

    def test_finding_progress_waits_for_all_gates_and_cannot_be_replayed(self):
        self.ticket("PROJ-1", "running")
        root = self.cfg["shared_root"]
        self.cfg["config"] = root / "config.yaml"
        self.cfg["config"].write_text("llm: {}\n")
        ledger = root / ".orchestration/.review-ledger/subject-PROJ-1.json"
        ledger.parent.mkdir(parents=True)
        review = dict(
            work_subject=dict(id="PROJ-1", repository=str(root.resolve())),
            components={
                "finding-1": dict(
                    status="resolved",
                    claims={
                        "code-review": dict(status="resolved"),
                        "security-review": dict(status="open"),
                    },
                )
            },
            repair_attempts=[
                dict(
                    claims_finalized_at="now", completed_at="now", closed=["finding-1"]
                )
            ],
        )
        ledger.write_text(json.dumps(review))
        path = controller.state_path(self.cfg["state_dir"], "1")
        path.parent.mkdir(parents=True)
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            milestone="review_finding_closed",
            evidence=json.dumps(dict(ledger=str(ledger), finding="finding-1")),
            attempt_token="token",
        )
        with self.assertRaisesRegex(controller.SprintError, "no open gate claims"):
            controller.record_progress(args, self.cfg)
        review["components"]["finding-1"]["claims"]["security-review"]["status"] = (
            "resolved"
        )
        review["repair_pending_review"] = True
        ledger.write_text(json.dumps(review))
        with self.assertRaises(controller.SprintError):
            controller.record_progress(args, self.cfg)
        review["repair_pending_review"] = False
        ledger.write_text(json.dumps(review))
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_progress(args, self.cfg)
            # Different JSON whitespace and path spellings cannot manufacture progress.
            args.evidence = json.dumps(
                dict(finding="finding-1", ledger=str(ledger.relative_to(root))),
                indent=2,
            )
            controller.record_progress(args, self.cfg)
        progress = controller.load(path)["tickets"]["PROJ-1"]["progress"]
        self.assertEqual(len(progress), 1)
        self.assertTrue(progress[0]["verified"])

    def test_phase_exhaustion_is_visible_without_stopping_other_phases(self):
        root = self.cfg["shared_root"]
        self.cfg["config"] = root / "config.yaml"
        self.cfg["config"].write_text(
            "llm:\n  budgets:\n    max_usd_per_design_phase: 1\n"
        )
        ledger = root / ".orchestration/.llm-usage/usage.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            json.dumps(
                dict(
                    kind="usage",
                    ticket="PROJ-1",
                    run_id="design",
                    role="design-reviewer",
                    cost_usd="1",
                )
            )
            + "\n"
        )
        snapshot = controller.usage_snapshots(self.cfg)["PROJ-1"]
        self.assertEqual(snapshot["phase_budgets"]["design"]["state"], "exhausted")
        self.assertEqual(
            snapshot["phase_budgets"]["implementation"]["state"], "available"
        )
        self.assertEqual(snapshot["state"], "ok")

    def test_unrecoverable_ticket_does_not_stop_independent_work_or_loop(self):
        self.ticket("PROJ-1", "needs_repair")
        other = self.ticket("PROJ-2")
        plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["repair"], [])
        self.assertEqual(plan["launch"], ["PROJ-2"])
        self.assertEqual([item["key"] for item in plan["decision_queue"]], ["PROJ-1"])
        other["state"] = "completed"
        self.assertFalse(
            controller.plan_value(self.state, self.cfg)["autonomous_work_remaining"]
        )
        summary = controller.summary_value(self.state, self.cfg)
        self.assertTrue(summary["finished"])
        self.assertEqual(len(summary["decision_queue"]), 1)

    def test_finish_first_pauses_fresh_launch_for_recovery(self):
        self.ticket("PROJ-1", "recoverable")
        self.ticket("PROJ-2")
        with patch.object(
            controller, "automatic_recovery_available", return_value=True
        ):
            plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["recovery"], ["PROJ-1"])
        self.assertEqual(plan["launch"], [])
        self.assertTrue(plan["work_in_progress"]["fresh_launch_paused"])

    def test_unfinished_pr_limit_pauses_fresh_launch(self):
        self.cfg["max_unmerged_prs"] = 1
        self.ticket(
            "PROJ-1", "recoverable", pr="https://example/pr/1", attempt_token=""
        )
        self.ticket("PROJ-2", pr="")
        plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["launch"], [])
        self.assertEqual(plan["work_in_progress"]["unfinished_prs"], ["PROJ-1"])

    def test_operator_held_pr_does_not_globally_block_independent_work(self):
        self.cfg["max_unmerged_prs"] = 1
        self.ticket("PROJ-1", "user_action", pr="https://example/pr/1")
        self.ticket("PROJ-2", pr="")
        plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["launch"], ["PROJ-2"])
        self.assertEqual(plan["work_in_progress"]["total_visible"], 1)
        self.assertEqual(plan["work_in_progress"]["count"], 0)

    def test_pr_drain_orders_existing_pr_before_fresh_ticket(self):
        self.cfg["pr_drain_first"] = True
        self.ticket("PROJ-1", pr="")
        self.ticket("PROJ-2", pr="https://example/pr/2")
        self.assertEqual(
            controller.plan_value(self.state, self.cfg)["launch"], ["PROJ-2"]
        )

    def test_preserved_pr_reconciliation_remains_autonomous_when_opted_in(self):
        self.cfg["preserved_pr_auto_recovery"] = True
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            worker_identity={"kind": "execution_unit", "pid": "123"},
            verified_commits={"a" * 40: "b" * 40},
        )
        plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["pr_reconciliation"], ["PROJ-1"])
        self.assertEqual(plan["decision_queue"], [])
        self.assertTrue(plan["autonomous_work_remaining"])

    def test_reconcile_preserved_pr_creates_bounded_repair_continuation(self):
        self.cfg.update(preserved_pr_auto_recovery=True)
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            worker_identity={"kind": "execution_unit", "pid": "123"},
            verified_commits={"a" * 40: "b" * 40},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        receipt = {
            "receipt": {
                "branch": "feature",
                "url": "https://example/pr/123",
                "head": "a" * 40,
                "tree": "b" * 40,
            }
        }
        worktree = self.cfg["shared_root"] / ".worktrees/PROJ-1"
        with (
            patch("github_progress.observe", return_value=receipt),
            patch.object(controller, "branch_worktree", return_value=worktree),
            patch.object(controller, "worktree_is_quiescent", return_value=True),
            patch.object(
                controller,
                "worktree_revision",
                return_value={"head": "a" * 40, "tree": "b" * 40},
            ),
            patch.object(controller, "execution_unit_status", return_value="absent"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.reconcile_preserved_pr(
                argparse.Namespace(sprint="1", ticket="PROJ-1"), self.cfg
            )
        ticket = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(ticket["state"], "pending")
        self.assertTrue(ticket["next_launch_continuation"])
        self.assertEqual(ticket["recovery_binding"]["head"], "a" * 40)
        self.assertEqual(
            controller.plan_value(controller.load(path), self.cfg)["launch"], ["PROJ-1"]
        )

    def test_reconcile_preserved_pr_rejects_stale_clean_worktree(self):
        self.cfg.update(preserved_pr_auto_recovery=True)
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            worker_identity={"kind": "execution_unit", "pid": "123"},
            verified_commits={"a" * 40: "b" * 40},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        receipt = {
            "receipt": {
                "branch": "feature",
                "url": "https://example/pr/123",
                "head": "a" * 40,
                "tree": "b" * 40,
            }
        }
        worktree = self.cfg["shared_root"] / ".worktrees/PROJ-1"
        with (
            patch("github_progress.observe", return_value=receipt),
            patch.object(controller, "branch_worktree", return_value=worktree),
            patch.object(controller, "worktree_is_quiescent", return_value=True),
            patch.object(
                controller,
                "worktree_revision",
                return_value={"head": "c" * 40, "tree": "d" * 40},
            ),
            self.assertRaisesRegex(controller.SprintError, "differs"),
        ):
            controller.reconcile_preserved_pr(
                argparse.Namespace(sprint="1", ticket="PROJ-1"), self.cfg
            )
        self.assertEqual(
            controller.load(path)["tickets"]["PROJ-1"]["state"], "recoverable"
        )

    def test_reconcile_preserved_pr_rechecks_reservations_before_mutation(self):
        self.cfg.update(preserved_pr_auto_recovery=True)
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            worker_identity={"kind": "execution_unit", "pid": "123"},
            verified_commits={"a" * 40: "b" * 40},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        receipt = {
            "receipt": {
                "branch": "feature",
                "url": "https://example/pr/123",
                "head": "a" * 40,
                "tree": "b" * 40,
            }
        }
        worktree = self.cfg["shared_root"] / ".worktrees/PROJ-1"
        with (
            patch("github_progress.observe", return_value=receipt),
            patch.object(controller, "branch_worktree", return_value=worktree),
            patch.object(controller, "worktree_is_quiescent", return_value=True),
            patch.object(
                controller,
                "worktree_revision",
                return_value={"head": "a" * 40, "tree": "b" * 40},
            ),
            patch.object(
                controller.UsageLedger,
                "fence_recovery",
                side_effect=RuntimeError("open usage reservation"),
            ),
            patch.object(controller, "execution_unit_status", return_value="absent"),
            self.assertRaisesRegex(
                controller.SprintError, "open usage reservation"
            ),
        ):
            controller.reconcile_preserved_pr(
                argparse.Namespace(sprint="1", ticket="PROJ-1"), self.cfg
            )
        self.assertEqual(
            controller.load(path)["tickets"]["PROJ-1"]["state"], "recoverable"
        )

    def test_reconcile_preserved_pr_rejects_remote_head_move(self):
        self.cfg.update(preserved_pr_auto_recovery=True)
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            worker_identity={"kind": "execution_unit", "pid": "123"},
            verified_commits={"a" * 40: "b" * 40},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        first = {"receipt": {"branch": "feature", "url": "https://example/pr/123", "head": "a" * 40, "tree": "b" * 40}}
        moved = {"receipt": {**first["receipt"], "head": "c" * 40, "tree": "d" * 40}}
        worktree = self.cfg["shared_root"] / ".worktrees/PROJ-1"
        with (
            patch("github_progress.observe", side_effect=[first, moved]),
            patch.object(controller, "branch_worktree", return_value=worktree),
            patch.object(controller, "worktree_is_quiescent", return_value=True),
            patch.object(controller, "worktree_revision", return_value={"head": "a" * 40, "tree": "b" * 40}),
            patch.object(controller, "execution_unit_status", return_value="absent"),
            self.assertRaisesRegex(controller.SprintError, "changed during authentication"),
        ):
            controller.reconcile_preserved_pr(argparse.Namespace(sprint="1", ticket="PROJ-1"), self.cfg)
        self.assertEqual(controller.load(path)["tickets"]["PROJ-1"]["state"], "recoverable")

    def test_reconcile_preserved_pr_requires_execution_unit_proven_absent(self):
        self.cfg.update(preserved_pr_auto_recovery=True)
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1", "recoverable", attempts=2,
            worker_identity={"kind": "execution_unit", "pid": "123"},
            verified_commits={"a" * 40: "b" * 40},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        receipt = {"receipt": {"branch": "feature", "url": "https://example/pr/123", "head": "a" * 40, "tree": "b" * 40}}
        with (
            patch("github_progress.observe", return_value=receipt),
            patch.object(controller, "branch_worktree", return_value=self.cfg["shared_root"] / ".worktrees/PROJ-1"),
            patch.object(controller, "worktree_is_quiescent", return_value=True),
            patch.object(controller, "worktree_revision", return_value={"head": "a" * 40, "tree": "b" * 40}),
            patch.object(controller, "execution_unit_status", return_value="unknown"),
            self.assertRaisesRegex(controller.SprintError, "not proven absent"),
        ):
            controller.reconcile_preserved_pr(argparse.Namespace(sprint="1", ticket="PROJ-1"), self.cfg)

    def test_reconcile_preserved_pr_rejects_missing_identity_without_operator_attestation(self):
        self.cfg.update(preserved_pr_auto_recovery=True)
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1", "recoverable", attempts=2, worker_identity="",
            verified_commits={"a" * 40: "b" * 40},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        receipt = {"receipt": {"branch": "feature", "url": "https://example/pr/123", "head": "a" * 40, "tree": "b" * 40}}
        with (
            patch("github_progress.observe", return_value=receipt),
            patch.object(controller, "branch_worktree", return_value=self.cfg["shared_root"] / ".worktrees/PROJ-1"),
            patch.object(controller, "worktree_is_quiescent", return_value=True),
            patch.object(controller, "worktree_revision", return_value={"head": "a" * 40, "tree": "b" * 40}),
            self.assertRaisesRegex(controller.SprintError, "one-shot recovery capability"),
        ):
            controller.reconcile_preserved_pr(argparse.Namespace(sprint="1", ticket="PROJ-1"), self.cfg)

    def test_legacy_progress_binding_is_not_treated_as_preserved_pr_receipt(self):
        ticket = self.ticket(
            "PROJ-1",
            recovery_binding={
                "attempt": 1,
                "invocation_id": "old",
                "branch": "feature",
                "pr": "123",
                "head": "a" * 40,
                "tree": "b" * 40,
            },
        )
        with patch("github_progress.observe") as observe:
            controller.verify_recovery_binding(self.cfg, ticket)
        observe.assert_not_called()

    def test_reservation_keeps_recovery_fence_until_launch_ack_boundary(self):
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("worktree_base: .worktrees\n")
        self.cfg["config"] = config
        ticket = self.ticket(
            "PROJ-1",
            "pending",
            attempts=1,
            next_launch_continuation=True,
            recovery_binding={"kind": "preserved_pr", "recovery_id": "recovery-test"},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        ledger = controller.UsageLedger(self.cfg["shared_root"])
        ledger.fence_recovery("PROJ-1", "recovery-test")
        args = argparse.Namespace(
            sprint="1", ticket="PROJ-1", run_ref="recovery-run", run_id="",
            role="sprint-worker", worker_ref="",
        )
        with (
            patch.object(controller, "verify_recovery_binding"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.reserve(args, self.cfg)
        self.assertEqual(ticket["key"], "PROJ-1")
        events = ledger.snapshot()
        self.assertEqual(
            ledger._active_recovery_fence(events, "PROJ-1")["recovery_id"],
            "recovery-test",
        )

    def test_worktree_revision_reads_real_git_head_and_tree(self):
        worktree = self.cfg["shared_root"] / "real-worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
        (worktree / "file.txt").write_text("verified\n")
        subprocess.run(["git", "add", "file.txt"], cwd=worktree, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test"],
            cwd=worktree,
            check=True,
        )
        revision = controller.worktree_revision(worktree)
        self.assertEqual(
            revision["head"],
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip(),
        )
        self.assertEqual(
            revision["tree"],
            subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=worktree, check=True, capture_output=True, text=True).stdout.strip(),
        )

    def test_signal_process_group_suppresses_permission_only_after_exit(self):
        child = Mock(pid=123)
        with patch.object(controller.os, "killpg", side_effect=PermissionError):
            child.poll.return_value = 0
            controller.signal_process_group(child, signal.SIGTERM)
            child.poll.return_value = None
            with self.assertRaises(PermissionError):
                controller.signal_process_group(child, signal.SIGTERM)

    def test_verified_progress_continuation_does_not_consume_relaunch(self):
        ticket = self.ticket(
            "PROJ-1",
            attempts=3,
            charged_attempts=3,
            continuations=1,
            next_launch_continuation=True,
        )
        self.assertIsNone(controller.attempt_limit_reason(ticket, self.cfg))
        ticket["continuations"] = 6
        self.assertIn(
            "attempt ceiling", controller.attempt_limit_reason(ticket, self.cfg)
        )

    def test_exhausted_continuation_allowance_becomes_a_charged_attempt(self):
        self.ticket(
            "PROJ-1",
            attempts=7,
            charged_attempts=1,
            continuations=6,
            next_launch_continuation=True,
            scope_assessment={"verdict": "ready"},
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            run_ref="charged-after-cap",
            run_id="",
            role="sprint-worker",
            worker_ref="",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            controller.reserve(args, self.cfg)
        resumed = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(resumed["continuations"], 6)
        self.assertEqual(resumed["charged_attempts"], 2)

    def test_requeue_and_reserve_account_a_verified_timeout_as_continuation(self):
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=3,
            charged_attempts=3,
            progress=[{"verified": True, "attempt": 3, "milestone": "pr_opened"}],
            last_terminal={
                "attempt": 3,
                "invocation_id": "invocation-3",
                "stop_reason": "max_worker_lifetime_seconds",
            },
            worker_identity={
                "kind": "execution_unit",
                "containment": "test-supervisor",
                "invocation_id": "invocation-3",
            },
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        requeue = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            reason="continue verified work",
            attempt_token="token",
            operator_capability="",
        )
        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.requeue(requeue, self.cfg)
        resumed = controller.load(path)["tickets"]["PROJ-1"]
        self.assertTrue(resumed["next_launch_continuation"])
        reserve = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            run_ref="continuation",
            run_id="",
            role="sprint-worker",
            worker_ref="",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            controller.reserve(reserve, self.cfg)
        relaunched = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(
            (
                relaunched["attempts"],
                relaunched["charged_attempts"],
                relaunched["continuations"],
            ),
            (4, 3, 1),
        )
        self.assertEqual(relaunched["last_terminal"], {})

    def test_stale_terminal_cannot_credit_a_later_attempt(self):
        self.ticket(
            "PROJ-1",
            "recoverable",
            attempts=2,
            charged_attempts=2,
            progress=[{"verified": True, "attempt": 2, "milestone": "tests_passed"}],
            last_terminal={
                "attempt": 1,
                "invocation_id": "invocation-1",
                "stop_reason": "max_worker_lifetime_seconds",
            },
            worker_identity={
                "kind": "execution_unit",
                "containment": "test-supervisor",
                "invocation_id": "invocation-2",
            },
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            reason="recover later crash",
            attempt_token="token",
            operator_capability="",
        )
        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.requeue(args, self.cfg)
        self.assertFalse(
            controller.load(path)["tickets"]["PROJ-1"]["next_launch_continuation"]
        )

    def test_wip_ceiling_allows_only_the_pending_continuation(self):
        self.cfg["max_unmerged_prs"] = 1
        self.ticket("PROJ-1", pr="https://example/pr/1", next_launch_continuation=True)
        self.ticket("PROJ-2", pr="")
        plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["launch"], ["PROJ-1"])

    def test_direct_reserve_cannot_bypass_finish_first_plan(self):
        self.ticket("PROJ-1", "needs_repair")
        self.ticket("PROJ-2", scope_assessment={"verdict": "ready"})
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-2",
            run_ref="fresh",
            run_id="",
            role="sprint-worker",
            worker_ref="",
        )
        with (
            patch.object(controller, "automatic_recovery_available", return_value=True),
            self.assertRaisesRegex(controller.SprintError, "current launch plan"),
        ):
            controller.reserve(args, self.cfg)

    def test_sibling_decomposition_requires_fresh_parent_evidence(self):
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("jira_subtask_decomposition_mode: sibling\n")
        self.cfg["config"] = config
        self.ticket(
            "PROJ-1",
            "needs_decomposition",
            parent="PROJ-9",
            subtasks=[],
            scope_assessment={"slices": [{"id": "foundation"}, {"id": "cutover"}]},
        )
        foundation = {"id": "foundation", "summary": "Foundation"}
        cutover = {"id": "cutover", "summary": "Cutover"}
        self.state["tickets"]["PROJ-1"]["scope_assessment"]["slices"] = [
            foundation,
            cutover,
        ]
        self.ticket(
            "PROJ-2",
            parent="PROJ-9",
            summary="Foundation",
            issue_type="Sub-task",
            is_subtask=True,
            labels=[
                "orchestration-slice-proj-1-foundation",
                controller.decomposition_provenance("PROJ-1", foundation),
            ],
        )
        self.ticket(
            "PROJ-3",
            parent="PROJ-9",
            summary="Cutover",
            issue_type="Sub-task",
            is_subtask=True,
            labels=[
                "orchestration-slice-proj-1-cutover",
                controller.decomposition_provenance("PROJ-1", cutover),
            ],
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        args = argparse.Namespace(sprint="1", ticket="PROJ-1", children="PROJ-2,PROJ-3")
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_decomposition(args, self.cfg)
        self.assertEqual(
            controller.load(path)["tickets"]["PROJ-1"]["state"], "decomposed"
        )

    def test_decomposition_binding_requires_authenticated_slice_dependencies(self):
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("jira_subtask_decomposition_mode: sibling\n")
        self.cfg["config"] = config
        foundation = {"id": "foundation", "summary": "Foundation"}
        cutover = {
            "id": "cutover",
            "summary": "Cutover",
            "depends_on": ["foundation"],
        }
        self.ticket(
            "PROJ-1",
            "needs_decomposition",
            parent="PROJ-9",
            subtasks=[],
            scope_assessment={"slices": [foundation, cutover]},
        )
        self.ticket(
            "PROJ-2",
            parent="PROJ-9",
            summary="Foundation",
            issue_type="Sub-task",
            is_subtask=True,
            labels=[
                "orchestration-slice-proj-1-foundation",
                controller.decomposition_provenance("PROJ-1", foundation),
            ],
        )
        self.ticket(
            "PROJ-3",
            parent="PROJ-9",
            summary="Cutover",
            issue_type="Sub-task",
            is_subtask=True,
            labels=[
                "orchestration-slice-proj-1-cutover",
                controller.decomposition_provenance("PROJ-1", cutover),
            ],
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        args = argparse.Namespace(sprint="1", ticket="PROJ-1", children="PROJ-2,PROJ-3")
        with self.assertRaisesRegex(controller.SprintError, "dependency links"):
            controller.record_decomposition(args, self.cfg)
        self.state["tickets"]["PROJ-3"]["dependencies"] = ["PROJ-2"]
        controller.save(path, self.state)
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_decomposition(args, self.cfg)
        self.assertEqual(
            controller.load(path)["tickets"]["PROJ-1"]["state"], "decomposed"
        )

    def test_required_slice_contracts_block_incomplete_decomposition(self):
        raw = {
            "schema_version": 1,
            "ticket": "PROJ-1",
            "verdict": "decompose",
            "complexity_score": 90,
            "reasons": ["large"],
            "slices": [
                {
                    "id": identifier,
                    "summary": identifier,
                    "behavior": "behavior",
                    "migration_owner": "none",
                    "test_plan": ["test"],
                    "acceptance_criteria": ["accepted"],
                    "depends_on": [],
                    "contracts": {},
                }
                for identifier in ("one", "two")
            ],
        }
        with self.assertRaisesRegex(controller.SprintError, "product_behavior"):
            controller.validated_scope_assessment(
                raw, "PROJ-1", 6, 70, ["product_behavior"]
            )
        for item in raw["slices"]:
            item["contracts"] = {"product_behavior": "Explicit behavior"}
        result = controller.validated_scope_assessment(
            raw, "PROJ-1", 6, 70, ["product_behavior"]
        )
        self.assertEqual(result["verdict"], "decompose")

    def test_approved_repository_decision_rescopes_instead_of_stopping(self):
        self.cfg.update(
            max_auto_slices=6,
            decomposition_threshold=70,
            required_slice_contracts=[],
            decision_registry={
                "aggregation.partition": {
                    "status": "approved",
                    "answer": "partition by organization and route",
                    "rationale": "prevent cross-tenant coalescing",
                }
            },
        )
        config = self.cfg["shared_root"] / "config.yaml"
        config.write_text("{}\n")
        self.cfg["config"] = config
        self.ticket("PROJ-1", attempt_token="", pr="", branch="", run_ref="")
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        assessment = self.cfg["shared_root"] / ".orchestration/scope.json"
        assessment.parent.mkdir(parents=True, exist_ok=True)
        assessment.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "ticket": "PROJ-1",
                    "verdict": "operator_decision",
                    "complexity_score": 20,
                    "reasons": ["aggregation boundary required"],
                    "slices": [],
                    "decision_key": "aggregation.partition",
                    "decision_question": "What is the aggregation partition?",
                }
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_scope(
                argparse.Namespace(
                    sprint="1", ticket="PROJ-1", assessment=str(assessment)
                ),
                self.cfg,
            )
        ticket = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(ticket["state"], "pending")
        self.assertEqual(ticket["scope_assessment"], {})

        # A scoper that ignores the supplied answer cannot create an unbounded
        # paid rescoping loop.
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_scope(
                argparse.Namespace(
                    sprint="1", ticket="PROJ-1", assessment=str(assessment)
                ),
                self.cfg,
            )
        ticket = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(ticket["state"], "operator_decision")
        self.assertIn("after its approved repository decision", ticket["reason"])

    def test_exhausted_repair_is_a_decision_not_autonomous_work(self):
        self.ticket("PROJ-1", "needs_repair", attempts=3)
        with patch.object(controller, "authorized_relaunch_ceiling", return_value=None):
            plan = controller.plan_value(self.state, self.cfg)
        self.assertFalse(plan["autonomous_work_remaining"])
        self.assertIn("attempt ceiling", plan["decision_queue"][0]["reasons"][0])

    def test_budget_paused_scope_and_repair_are_not_autonomous(self):
        self.cfg["auto_decompose_large_tickets"] = True
        for status in ("pending", "needs_repair", "recoverable", "needs_decomposition"):
            with self.subTest(status=status):
                self.ticket("PROJ-1", status)
                with patch.object(
                    controller,
                    "usage_snapshots",
                    return_value={"PROJ-1": {"state": "operator_action"}},
                ):
                    plan = controller.plan_value(self.state, self.cfg)
                self.assertFalse(plan["autonomous_work_remaining"])
                self.assertEqual(len(plan["decision_queue"]), 1)

    def test_live_recovery_retains_lane_until_unit_is_absent(self):
        self.ticket(
            "PROJ-1",
            "recoverable",
            worker_identity={
                "kind": "execution_unit",
                "containment": "test-supervisor",
            },
        )
        self.ticket("PROJ-2")
        with patch.object(controller, "execution_unit_status", return_value="live"):
            plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["launch"], [])
        self.assertEqual(plan["recovery_waiting"], ["PROJ-1"])
        self.assertEqual(plan["needs_reconcile"], ["PROJ-1"])
        with patch.object(controller, "execution_unit_status", return_value="absent"):
            plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan["recovery"], ["PROJ-1"])
        self.assertEqual(plan["launch"], [])

    def test_requeue_preserves_pr_and_branch(self):
        value = self.ticket(
            "PROJ-1",
            "recoverable",
            worker_identity={
                "kind": "execution_unit",
                "containment": "test-supervisor",
            },
        )
        path = controller.state_path(self.cfg["state_dir"], "1")
        controller.save(path, self.state)
        args = argparse.Namespace(
            sprint="1",
            ticket="PROJ-1",
            reason="stopped",
            attempt_token="token",
            operator_capability="",
        )
        with (
            patch.object(controller, "execution_unit_status", return_value="absent"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.requeue(args, self.cfg)
        resumed = controller.load(path)["tickets"]["PROJ-1"]
        self.assertEqual(
            (resumed["branch"], resumed["pr"]), (value["branch"], value["pr"])
        )
        self.assertEqual(resumed["state"], "pending")
        self.assertEqual(resumed["attempt_token"], "")

    def test_reserve_also_counts_live_recovery_units(self):
        self.ticket(
            "PROJ-1",
            "recoverable",
            worker_identity={
                "kind": "execution_unit",
                "containment": "test-supervisor",
            },
        )
        self.ticket("PROJ-2")
        controller.save(controller.state_path(self.cfg["state_dir"], "1"), self.state)
        args = argparse.Namespace(sprint="1", ticket="PROJ-2", run_ref="new-worker")
        with patch.object(controller, "execution_unit_status", return_value="live"):
            with self.assertRaisesRegex(controller.SprintError, "concurrency_max"):
                controller.reserve(args, self.cfg)

    def test_snapshot_releases_review_capacity_and_ignores_old_sprint_pressure(self):
        events = []
        for number in range(6):
            events.extend(
                [
                    dict(
                        kind="reservation",
                        ticket="PROJ-1",
                        run_id=str(number),
                        reservation_id=str(number),
                        role="code-reviewer",
                        projected_cost_usd="1",
                    ),
                    dict(kind="release", reservation_id=str(number)),
                ]
            )
        events.append(
            dict(
                kind="ticket_budget_pause", ticket="PROJ-1", reason="max_usd_per_sprint"
            )
        )
        events.append(
            dict(
                kind="ticket_budget_pause", ticket="PROJ-2", reason="max_usd_per_ticket"
            )
        )
        path = self.cfg["shared_root"] / ".orchestration/.llm-usage/usage.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(item) for item in events))
        result = controller.usage_snapshots(self.cfg)["PROJ-1"]
        self.assertEqual(result["reviewer_run_count"], 0)
        self.assertEqual(result["run_count"], 6)  # operational retries remain bounded
        self.assertEqual(result["state"], "ok")
        self.assertEqual(
            controller.usage_snapshots(self.cfg)["PROJ-2"]["state"], "operator_action"
        )

    def decomposed_family(self):
        self.ticket(
            "PROJ-1",
            "decomposed",
            subtasks=["PROJ-2", "PROJ-3"],
            decomposition_children=["PROJ-2", "PROJ-3"],
        )
        self.ticket("PROJ-2")
        self.ticket("PROJ-3", dependencies=["PROJ-2"])
        self.ticket("PROJ-4", dependencies=["PROJ-1"])

    def test_children_complete_then_release_parent_dependents(self):
        self.decomposed_family()
        for key in ("PROJ-2", "PROJ-3", "PROJ-4"):
            self.assertEqual(
                controller.plan_value(self.state, self.cfg)["launch"], [key]
            )
            self.state["tickets"][key]["state"] = "completed"
        self.assertFalse(
            controller.plan_value(self.state, self.cfg)["autonomous_work_remaining"]
        )
        summary = controller.summary_value(self.state, self.cfg)
        self.assertTrue(summary["decomposed"][0]["dependency_complete"])
        self.assertEqual(self.state["tickets"]["PROJ-1"]["state"], "decomposed")

    def test_children_inherit_parent_external_prerequisites(self):
        self.decomposed_family()
        self.state["tickets"]["PROJ-1"]["dependencies"] = ["EXT-1"]
        self.state["dependency_status"]["EXT-1"] = "Open"
        self.assertEqual(controller.plan_value(self.state, self.cfg)["launch"], [])
        self.state["dependency_status"]["EXT-1"] = "Done"
        self.assertEqual(
            controller.plan_value(self.state, self.cfg)["launch"], ["PROJ-2"]
        )

    def test_failed_missing_or_changed_children_cannot_release_parent(self):
        for failure in ("failed", "missing", "changed"):
            with self.subTest(failure=failure):
                self.decomposed_family()
                self.state["tickets"]["PROJ-2"]["state"] = "completed"
                self.state["tickets"]["PROJ-3"]["state"] = "completed"
                if failure == "failed":
                    self.state["tickets"]["PROJ-3"]["state"] = "blocked"
                elif failure == "missing":
                    del self.state["tickets"]["PROJ-3"]
                    self.state["dependency_status"]["PROJ-3"] = "Done"
                else:
                    self.state["tickets"]["PROJ-1"]["subtasks"].append("PROJ-5")
                self.assertFalse(
                    controller.dependency_complete(self.state, "PROJ-1", self.cfg)
                )
                self.assertNotIn(
                    "PROJ-4", controller.plan_value(self.state, self.cfg)["launch"]
                )

    def test_children_wait_until_decomposition_binding_is_recorded(self):
        self.decomposed_family()
        self.state["tickets"]["PROJ-1"]["state"] = "needs_decomposition"
        self.assertEqual(controller.plan_value(self.state, self.cfg)["launch"], [])

    def test_decomposition_dependency_cycle_cannot_release_work(self):
        self.decomposed_family()
        self.state["tickets"]["PROJ-2"]["dependencies"] = ["PROJ-1"]
        self.assertFalse(controller.dependency_complete(self.state, "PROJ-1", self.cfg))
        self.assertEqual(controller.plan_value(self.state, self.cfg)["launch"], [])

    def sync_fixture(self, incoming):
        incoming.update(
            project="PROJ", source_query="q", subtask_source_query="q", subtask_keys=[]
        )
        source = self.cfg["shared_root"] / "inventory.json"
        source.write_text("{}")
        args = argparse.Namespace(inventory=str(source), inventory_template=None)
        with (
            patch.object(controller, "normalized_inventory", return_value=incoming),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            controller.sync(args, self.cfg)
        self.state = controller.load(controller.state_path(self.cfg["state_dir"], "1"))

    def test_jira_readiness_refreshes_untouched_tickets_in_both_directions(self):
        state, reason = controller.initial_state("Backlog", self.cfg)
        self.ticket(
            "PROJ-1", state, raw_status="Backlog", reason=reason, attempt_token=""
        )
        controller.save(controller.state_path(self.cfg["state_dir"], "1"), self.state)
        for raw_status in ("Ready", "Blocked", "Ready"):
            fresh = copy.deepcopy(self.state)
            ticket = fresh["tickets"]["PROJ-1"]
            ticket["state"], ticket["reason"] = controller.initial_state(
                raw_status, self.cfg
            )
            ticket["raw_status"] = raw_status
            ticket["history"] = []
            self.sync_fixture(fresh)
            self.assertEqual(self.state["tickets"]["PROJ-1"]["state"], ticket["state"])

    def test_jira_refresh_does_not_erase_started_work_or_scope_decisions(self):
        for status, extra in (
            ("blocked", {"attempts": 1}),
            ("running", {"attempts": 1}),
            ("completed", {"attempts": 1}),
            (
                "completed",
                {"attempts": 0, "raw_status": "Done", "reason": "already Done in Jira"},
            ),
            (
                "operator_decision",
                {"scope_assessment": {"verdict": "operator_decision"}},
            ),
        ):
            with self.subTest(status=status):
                self.ticket("PROJ-1", status, **{"raw_status": "Backlog", **extra})
                controller.save(
                    controller.state_path(self.cfg["state_dir"], "1"), self.state
                )
                fresh = copy.deepcopy(self.state)
                fresh["tickets"]["PROJ-1"].update(
                    state="pending", reason="", raw_status="Ready"
                )
                self.sync_fixture(fresh)
                self.assertEqual(self.state["tickets"]["PROJ-1"]["state"], status)

    def test_authoritative_ready_sync_releases_untouched_legacy_hold(self):
        self.ticket(
            "PROJ-1",
            "user_action",
            raw_status="To Do",
            reason="verify_jira_readiness",
            attempt_token="",
            branch="",
            pr="",
            run_ref="",
            history=[{"event": "legacy-imported"}],
        )
        controller.save(controller.state_path(self.cfg["state_dir"], "1"), self.state)
        fresh = copy.deepcopy(self.state)
        fresh["tickets"]["PROJ-1"].update(
            state="pending", reason="", raw_status="Ready", history=[]
        )
        self.sync_fixture(fresh)
        ticket = self.state["tickets"]["PROJ-1"]
        self.assertEqual((ticket["state"], ticket["reason"]), ("pending", ""))
        self.assertEqual(ticket["history"][-1]["event"], "legacy-readiness-reconciled")

    def test_ready_sync_does_not_clear_an_unrelated_legacy_decision(self):
        self.ticket(
            "PROJ-1",
            "user_action",
            raw_status="To Do",
            reason="product decision required",
            attempt_token="",
            branch="",
            pr="",
            run_ref="",
            history=[{"event": "legacy-imported"}],
        )
        controller.save(controller.state_path(self.cfg["state_dir"], "1"), self.state)
        fresh = copy.deepcopy(self.state)
        fresh["tickets"]["PROJ-1"].update(
            state="pending", reason="", raw_status="Ready", history=[]
        )
        self.sync_fixture(fresh)
        ticket = self.state["tickets"]["PROJ-1"]
        self.assertEqual(
            (ticket["state"], ticket["reason"]),
            ("user_action", "product decision required"),
        )

    def excluded_ticket(self, **extra):
        return self.ticket(
            "PROJ-1",
            "user_action",
            **{
                "raw_status": "Ready",
                "attempt_token": "",
                "branch": "",
                "pr": "",
                "run_ref": "",
                "reason": "ticket disappeared from the refreshed Jira sprint query",
                "history": [{"event": "removed-from-query"}],
                **extra,
            },
        )

    def test_returning_inventory_refreshes_ready_done_and_nonready_tickets(self):
        for status in ("Ready", "Done", "To Do", "Blocked"):
            with self.subTest(status=status):
                self.excluded_ticket()
                controller.save(
                    controller.state_path(self.cfg["state_dir"], "1"), self.state
                )
                fresh = copy.deepcopy(self.state)
                expected = controller.initial_state(status, self.cfg)
                fresh["tickets"]["PROJ-1"].update(
                    raw_status=status, state=expected[0], reason=expected[1], history=[]
                )
                self.sync_fixture(fresh)
                actual = self.state["tickets"]["PROJ-1"]
                self.assertEqual((actual["state"], actual["reason"]), expected)
                self.assertEqual(actual["history"][-1]["event"], "returned-to-query")
                self.sync_fixture(fresh)
                self.assertEqual(len(self.state["tickets"]["PROJ-1"]["history"]), 2)

    def test_returning_inventory_preserves_execution_and_decision_evidence(self):
        for evidence in (
            {"attempts": 1},
            {"attempt_token": "fenced"},
            {"pr": "123"},
            {"worker_identity": {"kind": "execution_unit"}},
            {"scope_assessment": {"verdict": "operator_decision"}},
            {"history": [{"event": "removed-from-query"}, {"event": "finished"}]},
            {"history": []},
        ):
            with self.subTest(evidence=evidence):
                previous = copy.deepcopy(self.excluded_ticket(**evidence))
                controller.save(
                    controller.state_path(self.cfg["state_dir"], "1"), self.state
                )
                fresh = copy.deepcopy(self.state)
                fresh["tickets"]["PROJ-1"].update(
                    state="pending", reason="", history=[]
                )
                self.sync_fixture(fresh)
                actual = self.state["tickets"]["PROJ-1"]
                self.assertEqual(
                    (actual["state"], actual["reason"]),
                    (previous["state"], previous["reason"]),
                )

    def test_returning_ready_ticket_still_obeys_dependencies(self):
        self.excluded_ticket(dependencies=["PROJ-2"])
        self.ticket("PROJ-2", "blocked")
        controller.save(controller.state_path(self.cfg["state_dir"], "1"), self.state)
        fresh = copy.deepcopy(self.state)
        fresh["tickets"]["PROJ-1"].update(state="pending", reason="", history=[])
        self.sync_fixture(fresh)
        plan = controller.plan_value(self.state, self.cfg)
        self.assertNotIn("PROJ-1", plan["launch"])
        self.assertIn("PROJ-1", [item["key"] for item in plan["waiting"]])


if __name__ == "__main__":
    unittest.main()
