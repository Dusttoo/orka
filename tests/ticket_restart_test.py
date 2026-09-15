"""Root allowances preserve lifetime evidence and only relax explicitly approved limits."""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import api_agent
import operator_authority
from native_gateway import NativeGateway


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


controller = module('restart_controller', ROOT / 'scripts/sprint-controller.py')
review = module('restart_review', ROOT / 'scripts/review-ledger.py')
ALLOWANCES = dict(attempts=6, model_runs=30, review_runs=14, design_rounds=8,
                  code_rounds=7, security_rounds=7, repair_cycles=8,
                  ticket_usd='70', design_usd='20', implementation_usd='30',
                  code_review_usd='10', security_review_usd='10', progress_baseline_usd='15')


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.grant = dict(grant_id='grant-1', allowances=copy.deepcopy(ALLOWANCES),
                          reason='Bounded second work cycle', expires_at=time.time() + 3600)
        self.cfg = dict(shared_root=self.root, state_dir=self.root / '.orchestration/.sprint-state',
                        max_lane_relaunches=2, concurrency_max=1, max_usd_without_progress=5,
                        auto_decompose_large_tickets=False, pause_usd_per_ticket=20, warn_usd_per_ticket=10,
                        max_model_runs_per_ticket=12, max_reviewer_runs_per_ticket=6,
                        ready={'ready'}, done={'done'}, blocked={'blocked'})
        self.ticket = dict(key='T-1', state='pending', raw_status='Ready', reason='', attempts=3,
                           history=[], dependencies=[], branch='feature/T-1', pr='', run_ref='', summary='test',
                           attempt_token='old-token', worker_identity='')
        self.state = dict(schema_version=2, sprint={'id': '1'}, tickets={'T-1': self.ticket}, dependency_status={})
        self.path = controller.state_path(self.cfg['state_dir'], '1')
        controller.save(self.path, self.state)

    def test_supervisor_remains_bound_after_attach_consumes_launch_token(self):
        ticket = {"worker_identity": {"invocation_id": "new"}, "launch_evidence": {}}
        self.assertTrue(controller.lane_invocation_matches(ticket, "new"))
        self.assertFalse(controller.lane_invocation_matches(ticket, "old"))
        self.assertFalse(controller.lane_invocation_matches(ticket, ""))

    def test_attach_preserves_attempt_scoped_evidence_for_cooperative_recovery(self):
        tombstone = self.root / "terminal.json"
        identity = {
            "pid": "123",
            "start_identity": "start",
            "invocation_id": "invocation",
            "containment": "cooperative-session",
            "tombstone_path": str(tombstone),
        }
        self.ticket.update(
            state="running",
            attempts=3,
            attached_at="",
            attach_capability="attach",
            launch_evidence={
                "token": "launch",
                "status": "launched",
                "repository": str(self.root),
                "sprint": "1",
                "ticket": "T-1",
                "attempt": 3,
                "attempt_token": "old-token",
                "invocation_id": "invocation",
                "containment": "cooperative-session",
                "tombstone_path": str(tombstone),
                "cooperative_auto_recovery": True,
                "base_commit": "abc123",
                "identity": identity,
            },
        )
        controller.save(self.path, self.state)
        args = argparse.Namespace(sprint="1", ticket="T-1", launch_evidence="launch")
        with patch.object(controller, "execution_unit_status", return_value="live"), contextlib.redirect_stdout(io.StringIO()):
            controller.attach(args, self.cfg)
        attached = controller.load(self.path)["tickets"]["T-1"]
        self.assertEqual(attached["launch_evidence"]["token"], "")
        self.assertEqual(attached["launch_evidence"]["invocation_id"], "invocation")
        self.assertEqual(attached["launch_evidence"]["base_commit"], "abc123")
        with patch.object(controller, "execution_unit_status", return_value="live"), self.assertRaises(controller.SprintError):
            controller.attach(args, self.cfg)

        tombstone.write_text(json.dumps({
            "phase": "terminal",
            "invocation_id": "invocation",
            "cooperative_cleanup": {"worker_pgid": 43210, "gateway_closed": True},
        }))
        self.cfg["cooperative_auto_recovery"] = True
        with patch.object(controller.os, "killpg", side_effect=ProcessLookupError):
            self.assertTrue(
                controller.automatic_recovery_available(attached, self.cfg, "absent")
            )

    def test_watchdog_admission_and_authorized_baseline(self):
        with patch.object(controller, 'authorized_restart_grant', return_value=None):
            self.assertIn('max_usd_without_progress', controller.spending_admission_reason(self.ticket, self.cfg, {'spent_usd': 15}))
        self.ticket["restart_grant_id"] = self.grant["grant_id"]
        with patch.object(controller, 'authorized_restart_grant', return_value=self.grant):
            self.assertIsNone(controller.spending_admission_reason(self.ticket, self.cfg, {'spent_usd': 19}))
            self.assertIsNotNone(controller.spending_admission_reason(self.ticket, self.cfg, {'spent_usd': 20}))
            # Ordinary retries cannot move the approved baseline.
            self.ticket['attempts'] += 1
            self.assertEqual(controller.progress_spending(self.ticket, self.cfg, 21), 6)

    def test_watchdog_blocks_before_reservation_and_independent_ticket_continues(self):
        self.ticket['attempts'] = 0
        other = dict(self.ticket, key='T-2', history=[])
        self.state['tickets']['T-2'] = other
        controller.save(self.path, self.state)
        args = argparse.Namespace(sprint='1', ticket='T-1', run_ref='new')
        with patch.object(controller, 'usage_snapshots', return_value={'T-1': {'spent_usd': 15}}):
            with self.assertRaisesRegex(controller.SprintError, 'max_usd_without_progress'):
                controller.reserve(args, self.cfg)
            self.assertEqual(controller.load(self.path)['tickets']['T-1']['attempts'], 0)
            self.assertEqual(controller.plan_value(self.state, self.cfg)['launch'], ['T-2'])

    def test_restart_preserves_history_and_is_idempotent(self):
        self.ticket.update(state='operator_decision', reason='old dollar limit', attempts=4, worker_identity={'kind': 'execution_unit', 'containment': 'test-supervisor'})
        controller.save(self.path, self.state)
        args = argparse.Namespace(sprint='1', ticket='T-1', operator_capability='')
        with patch.object(controller, 'automatic_recovery_available', return_value=True), patch.object(controller, 'authorized_restart_grant', return_value=self.grant), patch.object(
                controller, 'usage_snapshots', return_value={'T-1': {'spent_usd': 15}}), contextlib.redirect_stdout(io.StringIO()):
            controller.restart_ticket(args, self.cfg)
            controller.restart_ticket(args, self.cfg)
        ticket = controller.load(self.path)['tickets']['T-1']
        self.assertEqual(ticket['state'], 'pending')
        self.assertEqual(ticket['attempts'], 4)
        self.assertEqual(ticket['attempt_token'], 'old-token')
        self.assertEqual(len(ticket['history']), 1)
        self.assertEqual(ticket['history'][0]['previous_reason'], 'old dollar limit')

    def test_restart_refuses_live_unknown_and_uncertain_provider_work(self):
        args = argparse.Namespace(sprint='1', ticket='T-1', operator_capability='')
        for identity in ('unverified-worker', {'kind': 'execution_unit'}):
            self.ticket['worker_identity'] = identity
            controller.save(self.path, self.state)
            with patch.object(controller, 'execution_unit_status', return_value='unknown'), self.assertRaises(controller.SprintError):
                controller.restart_ticket(args, self.cfg)
        self.ticket['worker_identity'] = ''
        self.ticket['attempts'] = 0
        controller.save(self.path, self.state)
        with patch.object(controller, 'usage_snapshots', return_value={'T-1': {'reserved_usd': 1}}), self.assertRaisesRegex(controller.SprintError, 'reservations'):
            controller.restart_ticket(args, self.cfg)

    def test_review_grant_expires_without_erasing_findings(self):
        subject = dict(kind='jira', id='T-1', repository=str(self.root))
        state = review.new_state('1', 3, subject)
        state['design'].update(rounds=[{'verdict': 'FAIL'}] * 5, escalated=True)
        state['escalated'] = True
        before = copy.deepcopy(state)
        with patch.object(review, 'project_root', return_value=self.root), patch.object(review, 'shared_repository_root', return_value=self.root):
            with patch.object(review, 'authorized_restart_grant', return_value=self.grant):
                self.assertEqual(review._design_plan(state)['next_action'], 'redesign')
                self.assertEqual(review._design_plan(state)['design_rounds_remaining'], 3)
                self.assertEqual(review.decide(state)['max_rounds'], 8)
            with patch.object(review, 'authorized_restart_grant', return_value=None):
                self.assertEqual(review._design_plan(state)['next_action'], review.ACTION_ESCALATE)
                self.assertEqual(review.decide(state)['max_rounds'], 3)
        self.assertEqual(state, before)

    def test_legacy_review_sequence_is_bound_to_repair_generations(self):
        state = {
            "review_generation": 3,
            "repair_pending_review": True,
            "repair_attempts": [
                {
                    "attempt": 1,
                    "recorded_at": "2026-09-15T10:00:00+00:00",
                    "required_gates": ["code-review", "security-review"],
                    "reviewed_gates": ["code-review", "security-review"],
                },
                {
                    "attempt": 2,
                    "recorded_at": "2026-09-15T12:00:00+00:00",
                    "required_gates": ["code-review"],
                    "reviewed_gates": [],
                },
            ],
            "review_permits": [
                {
                    "role": role,
                    "review_generation": generation,
                    "head": f"head-{generation}",
                    "receipt_consumed_at": "done",
                }
                for generation in (1, 2)
                for role in ("code-reviewer", "security-reviewer")
            ],
            "rounds": [
                {"round": 1, "gate": "code-review", "head": "head-1", "recorded_at": "2026-09-15T09:00:00+00:00", "claimed_verdict": "FAIL", "effective_verdict": "FAIL"},
                {"round": 2, "gate": "security-review", "head": "head-1", "recorded_at": "2026-09-15T09:01:00+00:00", "claimed_verdict": "PASS", "effective_verdict": "PASS"},
                {"round": 3, "gate": "code-review", "head": "head-2", "recorded_at": "2026-09-15T11:00:00+00:00", "claimed_verdict": "FAIL", "effective_verdict": "FAIL"},
                {"round": 4, "gate": "security-review", "head": "head-2", "recorded_at": "2026-09-15T11:01:00+00:00", "claimed_verdict": "PASS", "effective_verdict": "PASS"},
            ],
        }
        review._migrate_legacy_repair_generations(state)
        self.assertEqual(
            [entry["generation"] for entry in state["rounds"]],
            [1, 1, 2, 2],
        )
        self.assertEqual([entry["round"] for entry in state["rounds"]], [1, 2, 3, 4])
        self.assertEqual(
            state["repair_attempts"][-1]["required_gates"],
            ["code-review", "security-review"],
        )
        self.assertFalse(
            any(entry["generation"] == 3 for entry in state["rounds"])
        )
        self.assertEqual(state["rounds"][2]["effective_verdict"], "FAIL")

    def test_legacy_demoted_fail_is_not_silently_generation_migrated(self):
        state = {
            "review_generation": 2,
            "repair_attempts": [
                {
                    "recorded_at": "2026-09-15T10:00:00+00:00",
                    "required_gates": ["code-review"],
                }
            ],
            "review_permits": [
                {
                    "role": "code-reviewer",
                    "review_generation": 1,
                    "head": "head-1",
                    "receipt_consumed_at": "done",
                }
            ],
            "rounds": [
                {
                    "round": 1,
                    "gate": "code-review",
                    "head": "head-1",
                    "recorded_at": "2026-09-15T09:00:00+00:00",
                    "claimed_verdict": "FAIL",
                    "effective_verdict": "PASS",
                }
            ],
        }
        with self.assertRaisesRegex(review.LedgerError, "blocker-restoring"):
            review._migrate_legacy_repair_generations(state)
        self.assertNotIn("generation", state["rounds"][0])

    def test_api_restart_relaxes_phase_and_count_but_not_shared_run_limits(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = api_agent.budgets_from_config({})
        ledger.directory.mkdir(parents=True, exist_ok=True)
        events = []
        for i in range(12):
            events.extend([dict(kind='reservation', ticket='T-1', run_id=f'old-{i}', reservation_id=f'r-{i}', role='implementer', projected_cost_usd='1'),
                           dict(kind='release', reservation_id=f'r-{i}')])
        ledger.path.write_text(''.join(json.dumps(e)+'\n' for e in events))
        args = dict(projected=Decimal('1'), limits=limits, run_id='new', ticket='T-1', sprint='1', provider='openai', model='test', role='implementer')
        with patch.object(api_agent, 'authorized_restart_grant', return_value=None), self.assertRaises(api_agent.BudgetError):
            ledger.reserve(**args)
        with patch.object(api_agent, 'authorized_restart_grant', return_value=self.grant):
            self.assertTrue(ledger.reserve(**args))
            with self.assertRaisesRegex(api_agent.BudgetError, 'max_usd_per_run'):
                ledger.reserve(**{**args, 'projected': Decimal('11'), 'run_id': 'too-big'})
        self.assertEqual(ledger.snapshot()[:len(events)], events)

    def test_phase_allowance_is_independent_and_expires(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = api_agent.budgets_from_config({"llm": {"budgets": {"max_usd_per_code_review_phase": 1}}})
        args = dict(projected=Decimal("2"), limits=limits, run_id="review", ticket="T-1", sprint="1", provider="openai", model="test", role="code-reviewer")
        with patch.object(api_agent, "authorized_restart_grant", return_value=None), self.assertRaisesRegex(api_agent.BudgetError, "code_review_phase"):
            ledger.reserve(**args)
        with patch.object(api_agent, "authorized_restart_grant", return_value=self.grant):
            reservation = ledger.reserve(**args)
        ledger.release(reservation, "review", "provider rejected")
        with patch.object(api_agent, "authorized_restart_grant", return_value=None), self.assertRaisesRegex(api_agent.BudgetError, "code_review_phase"):
            ledger.reserve(**{**args, "run_id": "review2"})

    def test_unapplied_restart_does_not_move_watchdog_baseline(self):
        with patch.object(controller, "authorized_restart_grant", return_value=self.grant):
            self.assertEqual(controller.progress_spending(self.ticket, self.cfg, 15), 15)

    def test_startup_credit_requires_rejection_receipt_and_cannot_grow_unbounded(self):
        tombstone = self.root / 'terminal.json'
        terminal = dict(invocation_id='invocation', startup_retryable=True, stop_reason='provider_rate_limited', finished_at=controller.now())
        tombstone.write_text(json.dumps(terminal))
        self.ticket.update(state='recoverable', worker_identity=dict(kind='execution_unit', invocation_id='invocation', tombstone_path=str(tombstone)))
        events = [dict(kind='reservation', run_id='invocation', reservation_id='r'), dict(kind='release', reservation_id='r')]
        with patch.object(controller, 'execution_unit_status', return_value='absent'), patch.object(controller.UsageLedger, 'snapshot', return_value=events):
            self.assertEqual(controller.startup_credits(self.ticket, self.cfg), 1)
            with patch.object(controller.UsageLedger, 'snapshot', return_value=[]):
                self.assertIsNotNone(controller.current_startup_failure(self.ticket, self.cfg))
            self.assertIsNone(controller.attempt_limit_reason(self.ticket, self.cfg))
            self.ticket['startup_retry_receipts'] = [{'invocation_id': str(i)} for i in range(10)]
            self.assertEqual(controller.startup_credits(self.ticket, self.cfg), 2)
            self.ticket['attempts'] = 5
            self.assertIn('ceiling exhausted', controller.attempt_limit_reason(self.ticket, self.cfg))
            self.ticket['startup_retry_receipts'] = []
            events.append(dict(kind='usage', run_id='invocation'))
            self.assertIsNone(controller.current_startup_failure(self.ticket, self.cfg))

    def test_startup_cooldown_does_not_occupy_an_independent_lane(self):
        self.ticket.update(state='recoverable')
        self.state['tickets']['T-2'] = dict(self.ticket, key='T-2', state='pending', attempts=0)
        with patch.object(controller, 'current_startup_failure', side_effect=lambda ticket, cfg: {'invocation_id': 'i', 'finished_at': controller.now()} if ticket['key']=='T-1' else None):
            plan = controller.plan_value(self.state, self.cfg)
        self.assertEqual(plan['launch'], ['T-2'])
        self.assertEqual(plan['retry_waiting'][0]['key'], 'T-1')
        self.assertTrue(plan['autonomous_work_remaining'])

    def test_legacy_classification_preserves_work_and_does_not_launch(self):
        self.ticket.update(state='user_action', reason='old worker report', pr='123')
        controller.save(self.path, self.state)
        args = argparse.Namespace(sprint='1', ticket='T-1', classification='external_blocked', reason='Verified missing dependency')
        with contextlib.redirect_stdout(io.StringIO()):
            controller.reconcile_legacy(args, self.cfg)
        actual = controller.load(self.path)['tickets']['T-1']
        self.assertEqual(actual['pr'], '123')
        self.assertEqual(actual['attempts'], 3)
        self.assertEqual(actual['history'][-1]['previous_reason'], 'old worker report')
        self.assertEqual(controller.plan_value(controller.load(self.path), self.cfg)['launch'], [])

    def test_authority_scope_expiry_revocation_and_single_use(self):
        if os.getuid() == 0:
            self.skipTest('host authority deliberately ignores test paths under root')
        helper = ROOT / 'host-tools/orchestration-recovery-authority.py'
        env = dict(os.environ, ORCHESTRATION_AUTHORITY_TEST_MODE='1', ORCHESTRATION_AUTHORITY_STATE_DIR=str(self.root/'authority'))
        file = self.root/'allowances.json'; file.write_text(json.dumps(ALLOWANCES))
        def run(*args, token=None):
            return subprocess.run([sys.executable, str(helper), *args], input=token, text=True, capture_output=True, env=env)
        token = run('issue-restart', '--repository', str(self.root), '--ticket', 'T-1', '--allowances', str(file), '--reason', 'operator approved').stdout.strip()
        self.assertEqual(len(token), 64)
        scope = operator_authority._scope('restart', self.root, 'T-1')
        self.assertNotEqual(run('activate-restart', '--scope', operator_authority._scope('restart', self.root, 'T-2'), token=token).returncode, 0)
        self.assertEqual(run('activate-restart', '--scope', scope, token=token).returncode, 0)
        self.assertNotEqual(run('activate-restart', '--scope', scope, token=token).returncode, 0)
        self.assertEqual(run('restart-grant', '--scope', scope).returncode, 0)
        active = next((self.root/'authority/active').glob('*.json'))
        value = json.loads(active.read_text()); value['expires_at']=0; active.write_text(json.dumps(value))
        self.assertEqual(run('restart-grant', '--scope', scope).returncode, 3)
        self.assertEqual(run('revoke-restart', '--repository', str(self.root), '--ticket', 'T-1').returncode, 0)
        self.assertFalse(active.exists())

    def test_restart_preserves_explicit_legacy_product_decision(self):
        self.ticket.update(state='user_action', attempts=0)
        controller.save(self.path, self.state)
        reason = 'Product owner must choose retention policy'
        with contextlib.redirect_stdout(io.StringIO()):
            controller.reconcile_legacy(argparse.Namespace(sprint='1', ticket='T-1',
                classification='operator_decision', reason=reason), self.cfg)
            with patch.object(controller, 'authorized_restart_grant', return_value=self.grant), patch.object(
                    controller, 'usage_snapshots', return_value={'T-1': {'spent_usd': 15}}):
                controller.restart_ticket(argparse.Namespace(sprint='1', ticket='T-1', operator_capability=''), self.cfg)
        state = controller.load(self.path)
        self.assertFalse(state['tickets']['T-1'].get('scope_assessment'))
        self.assertEqual(state['tickets']['T-1']['state'], 'operator_decision')
        self.assertEqual(state['tickets']['T-1']['reason'], reason)
        self.assertEqual(controller.plan_value(state, self.cfg)['launch'], [])

    def seed_usage(self, events):
        ledger = api_agent.UsageLedger(self.root)
        ledger.directory.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text(''.join(json.dumps(event) + '\n' for event in events))
        return ledger

    def reserve_args(self, **overrides):
        return dict(projected=Decimal('.01'), limits=api_agent.budgets_from_config({}),
                    run_id='next', ticket='T-1', sprint='1', provider='openai',
                    model='test', role='implementer', **overrides)

    def test_grant_absolute_run_and_review_ceilings(self):
        for role, count, error in [('implementer', 30, 'max_model_runs_per_ticket=30'),
                                   ('code-reviewer', 14, 'max_reviewer_runs_per_ticket=14')]:
            with self.subTest(role=role):
                events = []
                for i in range(count):
                    events.extend([dict(kind='reservation', ticket='T-1', run_id=f'old-{i}',
                        reservation_id=f'r-{i}', role=role, projected_cost_usd='.01'),
                        dict(kind='usage', ticket='T-1', run_id=f'old-{i}', reservation_id=f'r-{i}',
                             role=role, cost_usd='.01')])
                ledger = self.seed_usage(events)
                args = self.reserve_args()
                args['role'] = role
                with patch.object(api_agent, 'authorized_restart_grant', return_value=self.grant), self.assertRaisesRegex(api_agent.BudgetError, error):
                    ledger.reserve(**args)

    def test_grant_absolute_attempt_ceiling(self):
        with patch.object(controller, 'authorized_restart_grant', return_value=self.grant), patch.object(
                controller, 'authorized_relaunch_ceiling', return_value=None):
            self.ticket['attempts'] = 5
            self.assertIsNone(controller.attempt_limit_reason(self.ticket, self.cfg))
            self.ticket['attempts'] = 6
            reason = controller.attempt_limit_reason(self.ticket, self.cfg)
            self.assertIsNotNone(reason)
            self.assertIn('ceiling exhausted', reason)

    def test_grant_absolute_dollar_ceilings(self):
        cases = [('ticket', 'implementer', '70', 'max_usd_per_ticket'),
                 ('design', 'design-reviewer', '20', 'max_usd_per_design_phase'),
                 ('implementation', 'implementer', '30', 'max_usd_per_implementation_phase'),
                 ('code_review', 'code-reviewer', '10', 'max_usd_per_code_review_phase'),
                 ('security_review', 'security-reviewer', '10', 'max_usd_per_security_review_phase')]
        for phase, role, ceiling, error in cases:
            with self.subTest(phase=phase):
                # Ticket-only case spreads historic cost across phases, leaving
                # this request below its implementation-phase ceiling.
                events = ([dict(kind='usage', ticket='T-1', role='design-reviewer', cost_usd='20'),
                           dict(kind='usage', ticket='T-1', role='implementer', cost_usd='30'),
                           dict(kind='usage', ticket='T-1', role='security-reviewer', cost_usd='20')]
                          if phase == 'ticket' else [dict(kind='usage', ticket='T-1', role=role, cost_usd=ceiling)])
                ledger = self.seed_usage(events)
                args = self.reserve_args()
                args['role'] = 'code-reviewer' if phase == 'ticket' else role
                with patch.object(api_agent, 'authorized_restart_grant', return_value=self.grant), self.assertRaisesRegex(api_agent.BudgetError, error):
                    ledger.reserve(**args)

    def test_grant_keeps_code_and_security_rounds_separate(self):
        for exhausted, other in [('code-reviewer', 'security-reviewer'), ('security-reviewer', 'code-reviewer')]:
            with self.subTest(exhausted=exhausted):
                ledger = self.seed_usage([dict(kind='usage', ticket='T-1', run_id=f'r-{i}',
                    logical_review_id=f'round-{i}', role=exhausted, cost_usd='.01') for i in range(7)])
                args = self.reserve_args(logical_review_id='round-8')
                args['role'] = exhausted
                with patch.object(api_agent, 'authorized_restart_grant', return_value=self.grant):
                    with self.assertRaisesRegex(api_agent.BudgetError, exhausted + ' logical review round ceiling'):
                        ledger.reserve(**args)
                    args['role'] = other
                    self.assertTrue(ledger.reserve(**args))

    def test_summary_distinguishes_exhaustion_from_completion(self):
        self.ticket.update(state='external_blocked', reason='Waiting for dependency')
        result = controller.summary_value(self.state, self.cfg)
        self.assertTrue(result['finished'])
        self.assertTrue(result['autonomous_work_exhausted'])
        self.assertFalse(result['sprint_complete'])
        self.ticket.update(state='completed', reason='Verified complete')
        self.assertTrue(controller.summary_value(self.state, self.cfg)['sprint_complete'])
        self.ticket.update(state='decomposed', decomposition_children=['T-2', 'T-3'], subtasks=['T-2', 'T-3'])
        self.state['tickets']['T-2'] = dict(self.ticket, key='T-2', state='completed', dependencies=[])
        self.state['tickets']['T-3'] = dict(self.ticket, key='T-3', state='external_blocked', dependencies=[])
        self.assertFalse(controller.summary_value(self.state, self.cfg)['sprint_complete'])
        self.state['tickets']['T-3']['state'] = 'completed'
        self.assertTrue(controller.summary_value(self.state, self.cfg)['sprint_complete'])


if __name__ == '__main__':
    unittest.main()
