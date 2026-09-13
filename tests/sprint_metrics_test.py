import sys
from pathlib import Path
from datetime import datetime, timezone
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sprint_metrics as metrics
import github_progress


class SprintMetricsTests(unittest.TestCase):
    def test_recorded_intervals_and_decisions_are_not_poll_counts(self):
        state = dict(tickets={'A': dict(attempts=1, state='completed', history=[
            dict(at='2026-01-01T00:00:00Z', event='reserved'),
            dict(at='2026-01-01T00:01:00Z', event='finished', outcome='operator_decision'),
            dict(at='2026-01-01T00:02:00Z', event='progress'),
            dict(at='2026-01-01T00:03:00Z', event='requeued'),
            dict(at='2026-01-01T00:04:00Z', event='finished', outcome='completed')]),
            'B': dict(attempts=0, state='completed', history=[])})
        result = metrics.summarize(state, {'A': {'spent_usd': 4}}, datetime(2026,1,1,0,5,tzinfo=timezone.utc))
        self.assertEqual(result['observed_blocked_seconds'], 120)
        self.assertEqual(result['observed_operator_decision_entries'], 1)
        self.assertEqual(result['reported_completion_rate'], 1)
        self.assertIsNone(result['state_seconds_by_ticket']['B'])
        self.assertIsNone(result['verified_merged_tickets'])

    def test_merge_verification_checks_identity_and_deduplicates_prs(self):
        state = dict(tickets={key: dict(attempts=1, state='completed', pr='1', branch='branch') for key in ['A','B']})
        result = metrics.summarize(state, {'A': {'spent_usd': 2}, 'B': {'spent_usd': 3}})
        pr = dict(number=1, base=dict(repo=dict(id=7)), head=dict(ref='branch'), merged=True,
                  merged_at='2026-01-01', merge_commit_sha='a'*40)
        with patch.object(github_progress, 'repository', return_value=('github.com','org/repo',7)), patch.object(
                github_progress, 'command_json', return_value=pr) as lookup:
            verified = metrics.verify_merges(Path('.'), state, result)
            self.assertEqual(verified['verified_unique_merges'], 1)
            self.assertEqual(verified['spend_per_verified_merge_usd'], 5)
            self.assertEqual(lookup.call_count, 1)
            pr['head']['ref'] = 'other'
            self.assertEqual(len(metrics.verify_merges(Path('.'), state, result)['merge_verification_errors']), 2)

    def test_pipeline_and_spend_coverage_expose_throughput_loss(self):
        state = dict(tickets={
            'A': dict(attempts=1, state='needs_repair', pr='1', ci_progress={'tree': 2}, history=[
                dict(event='supervisor-stopped', reason='max_worker_idle_seconds')]),
            'B': dict(attempts=1, state='completed', pr='2', history=[]),
        })
        result = metrics.summarize(state, {'A': {'spent_usd': 1}})
        self.assertEqual(result['pipeline'], {
            'attempted': 2, 'pr_opened': 2, 'ci_progress_recorded': 1,
            'merged_or_completed': 1, 'unfinished_prs': 1, 'worker_timeout_stops': 1,
        })
        self.assertFalse(result['spend_coverage']['desktop_subscription_included'])


if __name__ == '__main__': unittest.main()
