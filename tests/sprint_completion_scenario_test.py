"""Offline lifecycle scenario using real ledgers, commits, and controller transitions."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import api_agent
import github_progress
import sprint_metrics
SPEC = importlib.util.spec_from_file_location('scenario_controller', ROOT / 'scripts/sprint-controller.py')
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ResponseFixture:
    def __init__(self, text): self.text = text
    def request(self, provider, path, payload, **kwargs):
        if path.endswith('count_tokens'): return {'input_tokens': 40}
        return dict(id='fixture-response', stop_reason='end_turn', usage=dict(input_tokens=40, output_tokens=5),
                    content=[dict(type='text', text=self.text)])


class SprintCompletionScenario(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        self.commit('baseline')
        config = self.root / '.orchestration/config.yaml'
        config.parent.mkdir()
        config.write_text('''schema_version: 1
require_review_authorization: false
llm:
  execution: api
  provider: anthropic
  model: test-model
  fallback: none
  pricing:
    test-model:
      input_per_mtok: 1
      cache_read_per_mtok: 1
      cache_write_per_mtok: 1
      output_per_mtok: 1
''')
        with patch.object(controller, 'project_root', return_value=self.root):
            self.cfg = controller.settings(argparse.Namespace(config=str(config), state_dir=None))
        from provider_health import ProviderHealth, route_identity
        from context_pipeline import llm_route_from_config
        route=llm_route_from_config(config, 'sprint-worker')
        health=ProviderHealth(self.root); probe_token=health.claim_probe(route['provider'])
        health.complete_probe(route['provider'],probe_token,'healthy',route_identity(route))
        self.path = controller.state_path(self.cfg['state_dir'], '1')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = dict(schema_version=2, sprint=dict(id='1'), dependency_status={}, tickets={})
        for key, dependencies in [('PROJ-1', []), ('PROJ-2', ['PROJ-1']), ('PROJ-3', [])]:
            self.state['tickets'][key] = dict(key=key, state='pending', attempts=0, dependencies=dependencies,
                scope_assessment={"verdict":"ready"}, summary=key, reason='', branch='', pr='', run_ref='', history=[], progress=[])
        controller.save(self.path, self.state)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, value):
        (self.root / 'code').write_text(value)
        self.git('add', 'code')
        self.git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm',value)
        return self.git('rev-parse','HEAD')

    def led(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/review-ledger.py'), *args],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip().startswith('{') else result.stdout

    def reserve(self, key):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.reserve(argparse.Namespace(sprint='1',ticket=key,run_ref=key,run_id=key,
                role='implementer',worker_ref=key), self.cfg)
        return controller.load(self.path)['tickets'][key]['attempt_token']

    def finish(self, key, token, outcome='completed', pr='1'):
        with contextlib.redirect_stdout(io.StringIO()):
            controller.finish(argparse.Namespace(sprint='1',ticket=key,attempt_token=token,
                outcome=outcome,summary='fixture result',pr=pr,branch='codex/'+key), self.cfg)

    def test_sprint_continues_through_design_invalid_review_repair_ci_and_dependency(self):
        scope = self.root / 'scope.json'
        scope.write_text(json.dumps(dict(schema_version=1,ticket='PROJ-1',verdict='ready',prerequisites=[],
            complexity_score=10,reasons=['bounded fixture'],slices=[])))
        with contextlib.redirect_stdout(io.StringIO()):
            controller.record_scope(argparse.Namespace(sprint='1',ticket='PROJ-1',assessment=str(scope)), self.cfg)
        token = self.reserve('PROJ-1')
        # A separate ticket exceeds its allowance; it cannot hold the completed lanes.
        other = self.reserve('PROJ-3')
        ledger = api_agent.UsageLedger(self.root)
        with self.assertRaises(api_agent.BudgetError):
            ledger.reserve(projected=api_agent.Decimal('.02'),limits=api_agent.budgets_from_config(
                    {'llm': {'budgets': {'max_usd_per_ticket': '.01'}}}),
                run_id='expensive',ticket='PROJ-3',sprint='1',provider='anthropic',model='test-model',role='implementer')
        self.finish('PROJ-3',other,outcome='operator_decision',pr='')
        baseline = self.git('rev-parse','HEAD')
        self.led('design-open','design-PROJ-1')
        for reason in ['boundary incomplete','test contract incomplete']:
            self.assertEqual(self.led('design-record','design-PROJ-1','--verdict','FAIL','--evidence',reason)['next_action'], 'redesign')
        design = self.root / 'design.md'
        design.write_text('Reviewed test boundary')
        permit = self.led('permit-review','design-PROJ-1','--role','design-reviewer','--head',baseline)['review_phase_permit']
        artifact = self.root / 'design.json'
        artifact.write_text(json.dumps(dict(schema_version=1,gate='design-review',verdict='PASS',source_sha=baseline,
            artifact='design.md',artifact_sha256=hashlib.sha256(design.read_bytes()).hexdigest(),phase_permit=permit,
            checks=[dict(name='boundary',status='pass')])))
        self.led('complete-review','design-PROJ-1','--role','design-reviewer','--phase-permit',permit,'--result',str(artifact))
        self.assertEqual(self.led('design-record','design-PROJ-1','--result',str(artifact))['next_action'],'implement')
        implementation = self.commit('implementation')
        self.led('open','1')
        # A real API runner must release the unusable verdict permit before retry.
        permit = self.led('permit-review','1','--role','code-reviewer','--head',implementation)['review_phase_permit']
        invalid = api_agent.ApiAgent(root=self.root,config_path=self.cfg['config'],role='code-reviewer',
            ticket='PROJ-1',sprint='1',run_id='invalid',transport=ResponseFixture('VERDICT PASS'),
            review_authorization=permit,review_pr='1')
        with self.assertRaises(api_agent.AgentError):
            invalid.run(dict(model='test-model',max_tokens=100,messages=[dict(role='user',content='review')]))
        self.led('record','1','--gate','code-review','--verdict','FAIL','--blocking','code:boundary','--head',implementation)
        repaired = self.commit('repair')
        repair = self.root / 'repair.json'
        repair.write_text(json.dumps(dict(schema_version=1,head=repaired,findings=[dict(component='code:boundary',
            status='closed',root_cause='fixture boundary',change='fixed boundary',verification='regression passes')])))
        self.led('record-repair','1','--report',str(repair))
        # Both gate permits coexist; independent fixture results must each complete.
        permits = {role:self.led('permit-review','1','--role',role,'--head',repaired)['review_phase_permit']
                   for role in ['code-reviewer','security-reviewer']}
        for role, permit in permits.items():
            gate = role.removesuffix('er')
            result = self.root / (role+'.json')
            result.write_text(json.dumps(dict(schema_version=1,gate=gate,verdict='PASS',
                checks=[dict(name='regression',status='pass')],findings=[])))
            self.led('complete-review','1','--role',role,'--phase-permit',permit,'--result',str(result))
            self.led('record','1','--gate',gate,'--result',str(result),'--head',repaired,'--phase-permit',permit)
        self.assertEqual(self.led('complete-repair-review','1')['next_action'],'gates-clear')
        state = controller.load(self.path)
        lane = state['tickets']['PROJ-1']
        lane['launch_evidence'] = dict(
            base_commit=baseline, worker_cwd=str(self.root)
        )
        controller.save(self.path,state)
        args = argparse.Namespace(sprint='1',ticket='PROJ-1',attempt_token=token,milestone='implementation_commit',evidence=repaired)
        pr = dict(number=1,state='open',base=dict(repo=dict(id=7)),head=dict(sha=repaired,ref='codex/PROJ-1'))
        def github(root, host, endpoint, **kwargs):
            if endpoint == 'repos/example/project': return dict(id=7,full_name='example/project')
            if endpoint.endswith('/pulls/1'): return pr
            if 'check-runs?' in endpoint: return [dict(check_runs=[dict(id=1,head_sha=repaired,name='tests',
                app=dict(id=1),status='completed',conclusion='success')])]
            if '/statuses?' in endpoint: return [[]]
            raise AssertionError(endpoint)
        with patch.object(github_progress,'command_json',side_effect=github), contextlib.redirect_stdout(io.StringIO()):
            controller.record_progress(args,self.cfg)
            for milestone in ['pr_opened','ci_advanced']:
                args.milestone, args.evidence = milestone,'1'
                controller.record_progress(args,self.cfg)
            pr.update(merged=True,merged_at='2026-01-01T00:00:00Z',merge_commit_sha=repaired)
            self.finish('PROJ-1',token)
            state = controller.load(self.path)
            metrics = sprint_metrics.verify_merges(self.root,state,sprint_metrics.summarize(state,controller.usage_snapshots(self.cfg)))
            self.assertEqual(metrics['verified_merged_tickets'],1)
        self.assertIn('PROJ-2', controller.plan_value(controller.load(self.path),self.cfg)['launch'])
        self.finish('PROJ-2',self.reserve('PROJ-2'),pr='2')
        summary = controller.summary_value(controller.load(self.path),self.cfg)
        self.assertTrue(summary['finished'])
        self.assertEqual(len(summary['completed']),2)
        self.assertEqual(len(summary['decision_queue']),1)


if __name__ == '__main__': unittest.main()
