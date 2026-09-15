"""Offline GitHub observations with real local commit identities."""
import argparse
import contextlib
import copy
import fcntl
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
sys.path.insert(0, str(ROOT / "scripts"))
import github_progress as progress
SPEC = importlib.util.spec_from_file_location("github_progress_controller", ROOT / "scripts/sprint-controller.py")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class GitHubProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("remote", "add", "origin", "git@github.com:org/old-name.git")
        (self.root / "code").write_text("base")
        self.git("add", "code")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
        self.sha = self.git("rev-parse", "HEAD")
        self.tree = self.git("rev-parse", "HEAD^{tree}")
        self.ticket = dict(key="T-1", state="running", attempts=1, attempt_token="cap", branch="", pr="",
            history=[], progress=[dict(milestone="implementation_commit", evidence=self.sha,
                                      fingerprint=self.tree, verified=True, spent_usd=0)])
        self.pr = dict(number=12, state="open", base=dict(repo=dict(id=10)),
                       head=dict(sha=self.sha, ref="codex/T-1"))
        self.checks = [{"check_runs": []}]
        self.statuses = [[]]
        self.calls = []
        self.patch = patch.object(progress, "command_json", side_effect=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def api(self, root, host, endpoint, *, pages=False):
        self.calls.append((host, endpoint, pages))
        if endpoint == "repos/org/old-name":
            return dict(id=10, full_name="org/new-name")
        if endpoint.endswith("/pulls/12"):
            return copy.deepcopy(self.pr)
        if "/check-runs?" in endpoint:
            return copy.deepcopy(self.checks)
        if "/statuses?" in endpoint:
            return copy.deepcopy(self.statuses)
        raise AssertionError(endpoint)

    def ci(self):
        self.ticket["pr"] = "https://github.com/org/new-name/pull/12"
        return progress.observe(self.root, self.ticket, "ci_advanced", "12")

    def test_pr_is_bound_to_verified_commit_and_renamed_repository(self):
        result = progress.observe(self.root, self.ticket, "pr_opened", "12")
        self.assertTrue(result["verified"])
        self.assertEqual(result["receipt"]["url"], "https://github.com/org/new-name/pull/12")
        self.assertEqual(result["receipt"]["tree"], self.tree)

    def test_foreign_repo_unverified_head_and_wrong_branch_are_rejected(self):
        self.pr["base"]["repo"]["id"] = 11
        with self.assertRaises(progress.ProgressError):
            progress.observe(self.root, self.ticket, "pr_opened", "12")
        self.pr["base"]["repo"]["id"] = 10
        self.ticket["progress"] = []
        with self.assertRaisesRegex(progress.ProgressError, "verified implementation"):
            progress.observe(self.root, self.ticket, "pr_opened", "12")
        self.ticket["branch"] = "different"
        with self.assertRaisesRegex(progress.ProgressError, "branch differs"):
            progress.observe(self.root, self.ticket, "pr_opened", "12")

    def test_wrong_pr_url_is_rejected(self):
        for value in ("-1", "12?x=y", "https://github.com/other/repo/pull/12"):
            with self.assertRaises(progress.ProgressError):
                progress.observe(self.root, self.ticket, "pr_opened", value)

    def test_ci_requires_bound_pr(self):
        with self.assertRaisesRegex(progress.ProgressError, "pr_opened"):
            progress.observe(self.root, self.ticket, "ci_advanced", "12")

    def test_ci_only_credits_forward_progress_per_tree_and_check(self):
        for index, (state, conclusion, expected) in enumerate([
            ("queued", None, False), ("in_progress", None, True),
            ("completed", "failure", True), ("in_progress", None, False),
            ("completed", "failure", False), ("completed", "success", True),
            ("completed", "success", False),
        ]):
            self.checks = [{"check_runs": [dict(id=index+1, head_sha=self.sha, name="test",
                app=dict(id=7), status=state, conclusion=conclusion)]}]
            result = self.ci()
            self.assertEqual(result["verified"], expected, (state, conclusion))
            self.ticket.setdefault("ci_progress", {})[self.tree] = result["ci_highest"]
        self.assertTrue(all(pages for _, endpoint, pages in self.calls if "commits/" in endpoint))

    def test_latest_status_wins_across_all_pages(self):
        self.statuses = [[dict(id=20, context="build", state="pending")],
                         [dict(id=10, context="build", state="success")]]
        self.assertFalse(self.ci()["verified"])

    def test_wrong_check_head_and_moving_pr_head_are_rejected(self):
        self.checks = [{"check_runs": [dict(id=1, head_sha="a"*40, name="test", app=dict(id=7), status="completed", conclusion="success")]}]
        with self.assertRaisesRegex(progress.ProgressError, "this PR head"):
            self.ci()
        self.checks = [{"check_runs": []}]
        count = 0
        def moving(root, host, endpoint, **kwargs):
            nonlocal count
            result = self.api(root, host, endpoint, **kwargs)
            if "/pulls/" in endpoint:
                count += 1
                if count == 2:
                    result["head"]["sha"] = "b"*40
            return result
        with patch.object(progress, "command_json", side_effect=moving):
            with self.assertRaisesRegex(progress.ProgressError, "changed during"):
                self.ci()

    def checkpoint(self):
        cfg = dict(shared_root=self.root, state_dir=self.root / "state")
        path = controller.state_path(cfg["state_dir"], "1")
        path.parent.mkdir(parents=True)
        controller.save(path, dict(schema_version=2, sprint=dict(id="1"), tickets={"T-1": self.ticket}))
        args = argparse.Namespace(sprint="1", ticket="T-1", attempt_token="cap", milestone="pr_opened", evidence="12")
        return cfg, path, args

    def test_controller_binds_pr_and_replay_does_not_reset_spend(self):
        cfg, path, args = self.checkpoint()
        with patch.object(controller, "usage_snapshots", side_effect=[{"T-1": {"spent_usd": 1}}, {"T-1": {"spent_usd": 2}}]), contextlib.redirect_stdout(io.StringIO()):
            controller.record_progress(args, cfg)
            controller.record_progress(args, cfg)
        ticket = controller.load(path)["tickets"]["T-1"]
        self.assertEqual(ticket["pr"], "https://github.com/org/new-name/pull/12")
        self.assertEqual(ticket["branch"], "codex/T-1")
        receipts = [p for p in ticket["progress"] if p["milestone"] == "pr_opened"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["spent_usd"], 1)

    def test_amended_commit_is_bound_without_granting_duplicate_tree_progress(self):
        base = self.sha
        (self.root / "code").write_text("changed")
        self.git("add", "code")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "implementation")
        first = self.git("rev-parse", "HEAD")
        self.ticket["progress"] = []
        self.ticket["launch_evidence"] = dict(
            base_commit=base, worker_cwd=str(self.root)
        )
        cfg, path, args = self.checkpoint()
        args.milestone, args.evidence = "implementation_commit", first
        with patch.object(controller, "usage_snapshots", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            controller.record_progress(args, cfg)
            self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--amend", "-qm", "amended message")
            amended = self.git("rev-parse", "HEAD")
            args.evidence = amended
            controller.record_progress(args, cfg)
        ticket = controller.load(path)["tickets"]["T-1"]
        self.assertEqual(len(ticket["progress"]), 1)
        self.assertIn(amended, ticket["verified_commits"])
        self.pr["head"]["sha"] = amended
        self.assertTrue(progress.observe(self.root, ticket, "pr_opened", "12")["verified"])
        tree = self.git("rev-parse", "HEAD^{tree}")
        ticket["pr"] = "12"
        ticket["ci_progress"] = {tree: {"check:7:test": 3}}
        self.checks = [{"check_runs": [dict(id=10, head_sha=amended, name="test", app=dict(id=7), status="completed", conclusion="success")]}]
        self.assertFalse(progress.observe(self.root, ticket, "ci_advanced", "12")["verified"])

    def test_missing_check_conclusion_does_not_grant_progress(self):
        self.checks = [{"check_runs": [dict(id=1, head_sha=self.sha, name="test", app=dict(id=7), status="completed", conclusion=None)]}]
        with self.assertRaisesRegex(progress.ProgressError, "known conclusion"):
            self.ci()

    def test_sync_preserves_test_and_commit_ci_progress(self):
        fixture_spec = importlib.util.spec_from_file_location('repair_test_fixture', ROOT / 'tests/test_progress_test.py')
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
        (self.root / 'runner.py').write_text(fixture.RUNNER)
        (self.root / 'value').write_text('broken')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'failing test')
        first = self.git('rev-parse', 'HEAD')
        self.ticket.update(
            progress=[],
            launch_evidence=dict(base_commit=self.sha, worker_cwd=str(self.root)),
        )
        cfg, path, args = self.checkpoint()
        cfg['ready'], cfg['blocked'], cfg['done'] = {'ready'}, {'blocked'}, {'done'}
        cfg['config'] = self.root / 'config.yaml'
        cfg['config'].write_text('progress_tests:\n  unit:\n    command: ' + json.dumps([sys.executable, 'runner.py']) + '\n')
        with patch.object(controller, 'usage_snapshots', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            args.milestone, args.evidence = 'implementation_commit', first
            controller.record_progress(args, cfg)
            self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--amend', '-qm', 'amended')
            amended = self.git('rev-parse', 'HEAD')
            args.evidence = amended
            controller.record_progress(args, cfg)
            args.milestone, args.evidence = 'failing_test', json.dumps(dict(check='unit', commit=amended))
            controller.record_progress(args, cfg)
            self.pr['head']['sha'] = amended
            args.milestone, args.evidence = 'pr_opened', '12'
            controller.record_progress(args, cfg)
            self.checks = [{'check_runs': [dict(id=10, head_sha=amended, name='test', app=dict(id=7), status='completed', conclusion='success')]}]
            args.milestone = 'ci_advanced'
            controller.record_progress(args, cfg)
            before = controller.load(path)['tickets']['T-1']
            fresh = dict(schema_version=2, sprint=dict(id='1'), project='T', source_query='q',
                subtask_source_query='q', subtask_keys=[], dependency_status={},
                tickets={'T-1': dict(key='T-1', state='pending', raw_status='Ready', history=[], reason='')})
            inventory = self.root / 'inventory.json'
            inventory.write_text('{}')
            with patch.object(controller, 'normalized_inventory', return_value=fresh):
                controller.sync(argparse.Namespace(inventory=str(inventory), inventory_template=None), cfg)
            after = controller.load(path)['tickets']['T-1']
            for field in ('verified_commits', 'ci_progress', 'test_progress'):
                self.assertEqual(after.get(field), before[field], field)
            self.assertFalse(progress.observe(self.root, after, 'ci_advanced', '12')['verified'])
            (self.root / 'value').write_text('fixed')
            self.git('add', 'value')
            self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fix')
            args.milestone, args.evidence = 'tests_repaired', json.dumps(dict(check='unit', commit=self.git('rev-parse', 'HEAD')))
            controller.record_progress(args, cfg)
        repairs = [p for p in controller.load(path)['tickets']['T-1']['progress'] if p['milestone'] == 'tests_repaired']
        self.assertTrue(repairs[0]['verified'])

    def test_implementation_progress_rejects_descendant_that_is_not_worker_head(self):
        base = self.sha
        (self.root / "code").write_text("first")
        self.git("add", "code")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "first descendant",
        )
        unrelated = self.git("rev-parse", "HEAD")
        (self.root / "code").write_text("worker head")
        self.git("add", "code")
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "actual worker head",
        )
        self.ticket["progress"] = []
        self.ticket["launch_evidence"] = dict(
            base_commit=base, worker_cwd=str(self.root)
        )
        cfg, _, args = self.checkpoint()
        args.milestone, args.evidence = "implementation_commit", unrelated
        with self.assertRaisesRegex(
            controller.SprintError, "authenticated worker checkout HEAD"
        ):
            controller.record_progress(args, cfg)

    def test_network_observation_releases_lock_and_fences_attempt_change(self):
        cfg, path, args = self.checkpoint()
        def observe(*_args):
            with path.with_suffix(path.suffix + ".lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = controller.load(path)
                state["tickets"]["T-1"]["attempt_token"] = "replacement"
                controller.save(path, state)
            return dict(verified=True, fingerprint="receipt", receipt=dict(url="url", branch="branch"))
        with patch.object(progress, "observe", side_effect=observe):
            with self.assertRaises(controller.SprintError):
                controller.record_progress(args, cfg)
        self.assertEqual(controller.load(path)["tickets"]["T-1"]["pr"], "")


if __name__ == "__main__":
    unittest.main()
