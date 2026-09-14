"""Offline budget admission and native streaming contract tests."""
import json
import shutil
import subprocess
import argparse
import importlib.util
import os
import signal
import contextlib
import io
import socket
import threading
from unittest.mock import Mock, patch
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.request
import urllib.error
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from api_agent import AgentError, BudgetError, ProviderAmbiguous, ProviderHTTPError, UsageLedger
import native_gateway
from native_gateway import NativeGateway, stream_events, claude_child_environment


class Transport:
    def __init__(self):
        self.paid = 0
        self.fail = False

    def request(self, provider, path, payload, **kwargs):
        if path.endswith("count_tokens"):
            return {"input_tokens": 100}
        self.paid += 1
        if self.fail:
            raise ProviderAmbiguous("connection lost")
        return {"id": f"msg_{self.paid}", "model": "test", "type": "message",
                "role": "assistant", "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 100}}


class NativeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"llm": {"pricing": {"test": dict(input_per_mtok=1000,
            cache_write_per_mtok=1000, cache_read_per_mtok=1000, output_per_mtok=1000)}}}
        self.transport = Transport()
        self.gateway = NativeGateway(self.root, self.config, "T-1", "1", "native-1", self.transport)
        self.gateway.limits.update(max_usd_per_ticket=Decimal(".3"),
                                   pause_usd_per_ticket=Decimal(".3"))
        self.payload = {"model": "test", "messages": [], "max_tokens": 100}

    def test_native_gateway_uses_configured_provider_read_timeout(self):
        config = {
            "llm": {
                "budgets": {"provider_read_timeout_seconds": 811},
            }
        }
        with patch.object(native_gateway, "HttpTransport") as transport:
            gateway = NativeGateway(
                self.root, config, "T-2", "1", "configured-timeout"
            )
        transport.assert_called_once_with(timeout=811)
        self.assertIs(gateway.transport, transport.return_value)

    def test_http_gateway_authenticates_and_signals_budget_stop(self):
        endpoint = self.gateway.start()
        self.addCleanup(self.gateway.close)
        def request(token):
            return urllib.request.Request(endpoint + "/v1/messages",
                data=json.dumps({**self.payload, "stream": True}).encode(),
                headers={"x-api-key": token, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request("wrong"), timeout=2)
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()
        self.assertFalse(self.gateway.stopped.is_set())
        with urllib.request.urlopen(request(self.gateway.token), timeout=2) as response:
            self.assertIn(b"event: message_stop", response.read())
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(request(self.gateway.token), timeout=2)
        self.assertEqual(blocked.exception.code, 402)
        blocked.exception.close()
        self.assertTrue(self.gateway.stopped.is_set())
        self.assertEqual(self.transport.paid, 1)

    def test_rejected_startup_is_distinct_from_paid_or_uncertain_work(self):
        with patch.object(self.transport, "request", side_effect=ProviderHTTPError(429, "limited")):
            with self.assertRaises(ProviderHTTPError):
                self.gateway.model_request("anthropic", "/messages", {})
        self.assertTrue(self.gateway.startup_retryable())
        # The flag contract also covers another already-admitted response settling.
        with patch.object(self.gateway.health, "status", return_value={"state":"healthy"}), patch.object(self.transport, "request", return_value={"id": "accepted"}):
            self.gateway.model_request("anthropic", "/messages", {})
        self.assertFalse(self.gateway.startup_retryable())

    def test_token_count_rate_limit_is_an_unpaid_startup_failure(self):
        with patch.object(self.transport, "request", side_effect=ProviderHTTPError(429, "limited")):
            with self.assertRaises(ProviderHTTPError):
                self.gateway.request("/v1/messages", self.payload)
        self.assertTrue(self.gateway.startup_retryable())
        self.assertEqual(self.gateway.ledger.snapshot(), [])

    def test_uncertain_startup_never_receives_rejection_credit(self):
        with patch.object(self.transport, "request", side_effect=ProviderAmbiguous("lost")):
            with self.assertRaises(ProviderAmbiguous):
                self.gateway.model_request("anthropic", "/messages", {})
        with patch.object(self.transport, "request", side_effect=ProviderHTTPError(429, "limited")):
            with self.assertRaises(ProviderHTTPError):
                self.gateway.model_request("anthropic", "/messages", {})
        self.assertFalse(self.gateway.startup_retryable())

    def test_claude_environment_removes_accidental_alternate_provider_routing(self):
        env = claude_child_environment(dict(ANTHROPIC_API_KEY="real", CLAUDE_CODE_USE_BEDROCK="1",
            CLAUDE_CODE_OAUTH_TOKEN="real"), "temporary", "http://localhost")
        self.assertNotIn("real", env.values())
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)

    @unittest.skipUnless(shutil.which("claude"), "native Claude CLI not installed")
    def test_installed_claude_uses_gateway_for_main_and_auxiliary_models(self):
        model = "claude-sonnet-4-5-20250929"
        prices = dict(input_per_mtok=1, output_per_mtok=1, cache_read_per_mtok=1, cache_write_per_mtok=1)
        class MockProvider:
            def request(self, provider, path, payload, **kwargs):
                if "count_tokens" in path:
                    return dict(input_tokens=100)
                return dict(id="mock", type="message", role="assistant", model=payload["model"],
                    stop_reason="end_turn", stop_sequence=None, usage=dict(input_tokens=100, output_tokens=5),
                    content=[dict(type="text", text="ORKA_OFFLINE_OK")])
        gateway = NativeGateway(self.root, {"llm": {"pricing": {model: prices,
            "claude-haiku-4-5-20251001": prices}}}, "CLI-1", "1", "cli", MockProvider())
        endpoint = gateway.start()
        self.addCleanup(gateway.close)
        env = claude_child_environment(os.environ, gateway.token, endpoint)
        env.update(HTTP_PROXY=endpoint, HTTPS_PROXY=endpoint, ALL_PROXY=endpoint,
            http_proxy=endpoint, https_proxy=endpoint, all_proxy=endpoint,
            NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        with (self.root / "cli-output").open("w+") as output:
            process = subprocess.Popen([shutil.which("claude"), "-p", "Reply ORKA_OFFLINE_OK", "--model", model,
                "--output-format", "json", "--setting-sources", "", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--disallowedTools", "WebSearch"], cwd=self.root, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
            try:
                code = process.wait(timeout=30)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            output.seek(0)
            text = output.read()
        self.assertEqual(code, 0, text[-1000:])
        self.assertIn("ORKA_OFFLINE_OK", text)
        self.assertTrue(any(event["kind"] == "usage" for event in gateway.ledger.snapshot()))

    def test_ticket_over_budget_never_reaches_provider_and_other_ticket_finishes(self):
        self.gateway.request("/v1/messages", self.payload)
        with self.assertRaises(BudgetError):
            self.gateway.request("/v1/messages", self.payload)
        self.assertEqual(self.transport.paid, 1)
        other = NativeGateway(self.root, self.config, "T-2", "1", "native-2", self.transport)
        self.assertEqual(other.request("/v1/messages", self.payload)["stop_reason"], "end_turn")
        self.assertEqual(self.transport.paid, 2)

    def test_ambiguous_request_retains_envelope(self):
        self.transport.fail = True
        with self.assertRaises(ProviderAmbiguous):
            self.gateway.request("/v1/messages", self.payload)
        _, pending = UsageLedger._totals(self.gateway.ledger._events())
        self.assertEqual(len(pending), 1)
        with self.assertRaises(BudgetError):
            self.gateway.request("/v1/messages", self.payload)
        self.assertEqual(self.transport.paid, 1)

    def test_recovery_fence_atomically_blocks_and_releases_reservations(self):
        ledger = self.gateway.ledger
        ledger.fence_recovery("T-1", "recovery-test")
        with self.assertRaisesRegex(BudgetError, "fenced for preserved-PR recovery"):
            ledger.reserve(
                projected=Decimal(".01"),
                limits=self.gateway.limits,
                run_id="stale-worker",
                ticket="T-1",
                sprint="1",
                provider="anthropic",
                model="test",
                role="implementer",
            )
        ledger.release_recovery_fence("T-1", "recovery-test")
        ledger.release_recovery_fence("T-1", "recovery-test")
        reservation = ledger.reserve(
            projected=Decimal(".01"),
            limits=self.gateway.limits,
            run_id="current-worker",
            ticket="T-1",
            sprint="1",
            provider="anthropic",
            model="test",
            role="implementer",
        )
        self.assertTrue(reservation.startswith("resv_"))

    def test_malformed_cache_usage_retains_reservation_without_poisoning_ledger(self):
        original = self.transport.request
        for field in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
            for value in (-300, True, '100', 1.5, None):
                with self.subTest(field=field, value=value):
                    root = self.root / f'{field}-{value}'
                    gateway = NativeGateway(root, self.config, 'T-1', '1', 'bad', self.transport)
                    def malformed(*args, **kwargs):
                        result = original(*args, **kwargs)
                        if 'usage' in result:
                            result['usage'][field] = value
                        return result
                    with patch.object(self.transport, 'request', side_effect=malformed):
                        with self.assertRaisesRegex(AgentError, 'settlement evidence'):
                            gateway.request('/v1/messages', self.payload)
                    events = gateway.ledger.snapshot()
                    self.assertFalse(any(e['kind'] == 'usage' for e in events))
                    self.assertEqual(len(UsageLedger._totals(events)[1]), 1)
                    other = NativeGateway(root, self.config, 'T-2', '1', 'good', self.transport)
                    self.assertEqual(other.request('/v1/messages', self.payload)['stop_reason'], 'end_turn')

    def test_claude_final_settings_preserve_configuration_and_own_routing(self):
        settings = dict(env=dict(ANTHROPIC_BASE_URL='http://other',
            CLAUDE_CODE_USE_BEDROCK='1', ANTHROPIC_DEFAULT_HAIKU_MODEL='priced-custom-model',
            CUSTOM='keep'), permissions=dict(deny=['WebSearch']))
        source = self.root / 'settings.json'
        source.write_text(json.dumps(settings))
        for value in (str(source), json.dumps(settings)):
            command = native_gateway.claude_launch_arguments(['claude', '-p', 'hello', '--settings', value],
                'temporary', 'http://gateway')
            final = json.loads(command[command.index('--settings') + 1])
            self.assertEqual(final['permissions'], settings['permissions'])
            self.assertEqual(final['env']['CUSTOM'], 'keep')
            self.assertEqual(final['env']['ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'priced-custom-model')
            self.assertEqual(final['env']['ANTHROPIC_BASE_URL'], 'http://gateway')
            self.assertEqual(final['env']['ANTHROPIC_API_KEY'], 'temporary')
            self.assertEqual(final['env']['CLAUDE_CODE_USE_BEDROCK'], '0')

    def test_shared_sprint_reservations_block_other_ticket(self):
        self.transport.fail = True
        with self.assertRaises(ProviderAmbiguous):
            self.gateway.request("/v1/messages", self.payload)
        other = NativeGateway(self.root, self.config, "T-2", "1", "native-2", self.transport)
        other.limits["max_usd_per_sprint"] = Decimal(".3")
        with self.assertRaises(BudgetError):
            other.request("/v1/messages", self.payload)
        self.assertEqual(self.transport.paid, 1)

    def test_stream_preserves_tool_input_and_thinking_signature(self):
        response = self.transport.request("anthropic", "/messages", {})
        response["content"] = [{"type": "tool_use", "id": "tool", "name": "read", "input": {"path": "x"}},
                               {"type": "thinking", "thinking": "reason", "signature": "sig"}]
        events = list(stream_events(response))
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(json.loads(events[2]["delta"]["partial_json"]), {"path": "x"})
        self.assertTrue(any(e.get("delta", {}).get("signature") == "sig" for e in events))
        self.assertEqual(events[-1]["type"], "message_stop")

    def test_supervisor_kills_term_ignoring_worker_at_deadline(self):
        spec = importlib.util.spec_from_file_location("native_test_controller",
            Path(__file__).resolve().parents[1] / "scripts/sprint-controller.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        config = self.root / "config.yaml"
        config.write_text("max_worker_seconds: 1\n")
        ack = self.root / "ack"
        ack.touch()
        args = argparse.Namespace(command=[sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"],
            ready=str(self.root / "ready.json"), ack=str(ack),
            tombstone=str(self.root / "terminal.json"), output=str(self.root / "output"),
            invocation_id="unit", stdin_file=None, ticket="T-1", sprint="1")
        cfg = dict(config=config, shared_root=self.root, state_dir=self.root / "state")
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            controller.supervise_local(args, cfg)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
        result = json.loads(Path(args.tombstone).read_text())
        self.assertEqual(result["stop_reason"], "max_worker_seconds")
        self.assertEqual(result["returncode"], -signal.SIGKILL)

    def supervisor_fixture(self, command):
        spec = importlib.util.spec_from_file_location('repair_controller',
            Path(__file__).resolve().parents[1] / 'scripts/sprint-controller.py')
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        config = self.root / 'config.yaml'
        config.write_text('max_worker_seconds: 10\n')
        ack = self.root / 'ack'
        ack.touch()
        args = argparse.Namespace(command=command, ready=str(self.root / 'ready.json'), ack=str(ack),
            tombstone=str(self.root / 'terminal.json'), output=str(self.root / 'output'),
            invocation_id='unit', stdin_file=None, ticket='T-1', sprint='1')
        cfg = dict(config=config, shared_root=self.root, state_dir=self.root / 'state',
            max_usd_without_progress=5, pause_usd_per_ticket=20, warn_usd_per_ticket=10,
            max_model_runs_per_ticket=12, max_reviewer_runs_per_ticket=6, max_lane_relaunches=2,
            concurrency_max=1, auto_decompose_large_tickets=False, done={'done'},
            cooperative_auto_recovery=True)
        ticket = dict(key='T-1', state='running', attempts=1, attempt_token='cap', dependencies=[],
            branch='preserve', pr='12', history=[], summary='test', reason='',
            launch_evidence=dict(invocation_id='unit', cooperative_auto_recovery=True),
            worker_identity=dict(kind='execution_unit', containment='cooperative-session',
                invocation_id='unit', tombstone_path=args.tombstone))
        path = controller.state_path(cfg['state_dir'], '1')
        controller.save(path, dict(schema_version=2, sprint=dict(id='1'), dependency_status={}, tickets={'T-1': ticket}))
        return controller, args, cfg, path

    def run_supervisor(self, controller, args, cfg, gateway):
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with patch.dict(os.environ, ANTHROPIC_API_KEY='offline-fixture'), patch.object(
                    native_gateway, 'NativeGateway', return_value=gateway):
                controller.supervise_local(args, cfg)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)

    def test_supervisor_shared_pressure_recovers_after_release(self):
        worker = self.root / 'claude'
        worker.write_text('#!/bin/sh\nsleep 30\n')
        worker.chmod(0o700)
        controller, args, cfg, path = self.supervisor_fixture([str(worker)])
        gateway = self.gateway
        gateway.limits['max_usd_per_sprint'] = Decimal('.3')
        context = dict(ticket='T-2', sprint='1', run_id='other', provider='anthropic', model='test', role='implementer')
        reservation = gateway.ledger.reserve(projected=Decimal('.2'), limits=gateway.limits, **context)
        with self.assertRaises(BudgetError) as denied:
            gateway.request('/v1/messages', self.payload)
        gateway.stop(str(denied.exception))
        self.run_supervisor(controller, args, cfg, gateway)
        self.assertEqual(controller.load(path)['tickets']['T-1']['state'], 'recoverable')
        gateway.ledger.release(reservation, 'other', 'competing request cancelled')
        with patch.object(controller, 'execution_unit_status', return_value='absent'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(controller.plan_value(controller.load(path), cfg)['recovery'], ['T-1'])
            controller.requeue(argparse.Namespace(sprint='1', ticket='T-1', attempt_token='cap',
                reason='capacity released', operator_capability=''), cfg)
        self.assertEqual(controller.plan_value(controller.load(path), cfg)['launch'], ['T-1'])
        self.assertEqual(self.transport.paid, 0)
        # A ticket-local pause still requires authority after the same cleanup.
        state = controller.load(path)
        state['tickets']['T-1'].update(state='running', launch_evidence=dict(invocation_id='unit'))
        controller.save(path, state)
        local = NativeGateway(self.root, self.config, 'T-1', '1', 'local', self.transport)
        local.stop('ticket_budget_pause is active for T-1; operator reset required')
        self.run_supervisor(controller, args, cfg, local)
        self.assertEqual(controller.load(path)['tickets']['T-1']['state'], 'operator_decision')

    def test_supervisor_retains_rate_limit_when_child_exits_first(self):
        controller, args, cfg, path = self.supervisor_fixture(['claude'])
        gateway = NativeGateway(self.root, self.config, 'T-1', '1', 'unit', self.transport)
        with patch.object(self.transport, 'request', side_effect=ProviderHTTPError(429, 'limited')):
            with self.assertRaises(ProviderHTTPError):
                gateway.model_request('anthropic', '/messages', {})
        gateway.stop('provider_rate_limited')
        child = Mock(pid=987654, poll=Mock(return_value=1), wait=Mock(return_value=1))
        with patch.object(controller.subprocess, 'Popen', return_value=child), patch.object(controller.os, 'killpg'):
            self.run_supervisor(controller, args, cfg, gateway)
        child.poll.assert_called_once_with()
        terminal = json.loads(Path(args.tombstone).read_text())
        self.assertEqual(terminal['stop_reason'], 'provider_rate_limited')
        self.assertTrue(terminal['startup_retryable'])
        ticket = controller.load(path)['tickets']['T-1']
        self.assertEqual(ticket['state'], 'recoverable')
        self.assertEqual(ticket['reason'], 'provider_rate_limited')
        with patch.object(controller, 'execution_unit_status', return_value='absent'):
            self.assertIsNotNone(controller.current_startup_failure(ticket, cfg))
            self.assertEqual(controller.startup_credits(ticket, cfg), 1)

    def test_close_waits_for_inflight_provider_settlement(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                entered, release, published, closing = (threading.Event() for _ in range(4))
                original = self.transport.request
                def provider(provider, path, payload, **kwargs):
                    if path.endswith('count_tokens'):
                        return {'input_tokens': 100}
                    if payload['messages'] == ['reject']:
                        raise ProviderHTTPError(429, 'limited')
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('test did not release provider')
                    if ambiguous:
                        raise ProviderAmbiguous('accepted response lost')
                    return original(provider, path, payload, **kwargs)
                gateway = NativeGateway(self.root / str(ambiguous), self.config, 'T-1', '1', 'concurrent', self.transport)
                endpoint = gateway.start()
                observations, errors = {}, []
                def request(messages):
                    req = urllib.request.Request(endpoint + '/v1/messages',
                        data=json.dumps({**self.payload, 'messages': messages}).encode(),
                        headers={'x-api-key': gateway.token})
                    try:
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return response.status
                    except urllib.error.HTTPError as exc:
                        exc.close()
                        return exc.code
                def held_request():
                    try:
                        observations['status'] = request(['held'])
                    except Exception as exc:
                        errors.append(exc)
                # Observe server_close entry, after shutdown stops accepting
                # clients, so a premature publication is deterministic.
                original_close = gateway.server.server_close
                def server_close():
                    closing.set()
                    original_close()
                def publish_terminal():
                    gateway.close()
                    observations['retryable'] = gateway.startup_retryable()
                    observations['events'] = gateway.ledger.snapshot()
                    published.set()
                worker = threading.Thread(target=held_request)
                closer = threading.Thread(target=publish_terminal)
                with patch.object(self.transport, 'request', side_effect=provider), patch.object(
                        gateway.server, 'server_close', side_effect=server_close):
                    try:
                        worker.start()
                        self.assertTrue(entered.wait(3))
                        self.assertEqual(request(['reject']), 429)
                        self.assertEqual(gateway.provider_inflight, 1)
                        closer.start()
                        self.assertTrue(closing.wait(3))
                        self.assertFalse(published.wait(.2), 'terminal published before handler settlement')
                    finally:
                        release.set()
                        worker.join(5)
                        if closer.ident is not None:
                            closer.join(5)
                        else:
                            gateway.close()
                self.assertFalse(worker.is_alive())
                self.assertFalse(closer.is_alive())
                self.assertFalse(errors)
                self.assertTrue(published.is_set())
                self.assertFalse(observations['retryable'])
                events = observations['events']
                self.assertEqual(len(UsageLedger._totals(events)[1]), 1 if ambiguous else 0)
                self.assertEqual(sum(e['kind'] == 'usage' for e in events), 0 if ambiguous else 1)

    def test_close_bounds_abandoned_client(self):
        gateway = self.gateway
        gateway.start()
        accepted = threading.Event()
        original_setup = gateway.server.RequestHandlerClass.setup
        def setup(handler):
            original_setup(handler)
            accepted.set()
        closed = threading.Event()
        def close():
            gateway.close()
            closed.set()
        with patch.object(gateway.server.RequestHandlerClass, 'setup', setup):
            client = socket.create_connection(gateway.server.server_address, timeout=2)
            closer = threading.Thread(target=close)
            try:
                client.sendall(b'POST /v1/messages HTTP/1.1\r\n')
                self.assertTrue(accepted.wait(2))
                closer.start()
                self.assertTrue(closed.wait(12), 'abandoned request prevented bounded shutdown')
            finally:
                client.close()
                if closer.ident is not None:
                    closer.join(3)
        self.assertFalse(closer.is_alive())

    def test_supervisor_claude_settings_cannot_bypass_gateway(self):
        executable = shutil.which('claude')
        if executable is None:
            # Portable supervisor coverage only; native compatibility evidence
            # requires running this same case with an installed Claude CLI.
            fixture = self.root / 'claude'
            fixture.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, urllib.request
settings = json.loads(sys.argv[sys.argv.index('--settings') + 1])
env = {**os.environ, **settings.get('env', {})}
payload = dict(model='claude-sonnet-4-5-20250929', messages=[], max_tokens=100)
request = urllib.request.Request(env['ANTHROPIC_BASE_URL'] + '/v1/messages',
    data=json.dumps(payload).encode(), headers={'x-api-key': env['ANTHROPIC_API_KEY']})
urllib.request.urlopen(request, timeout=3).read()
''')
            fixture.chmod(0o700)
            executable = str(fixture)
        model = 'claude-sonnet-4-5-20250929'
        prices = dict(input_per_mtok=1, output_per_mtok=1, cache_read_per_mtok=1, cache_write_per_mtok=1)
        config = {'llm': {'pricing': {model: prices, 'claude-haiku-4-5-20251001': prices}}}
        alternate_transport = Transport()
        alternate = NativeGateway(self.root / 'alternate', config, 'other', '1', 'alternate', alternate_transport)
        alternate_endpoint = alternate.start()
        self.addCleanup(alternate.close)
        gateway = NativeGateway(self.root, config, 'T-1', '1', 'unit', self.transport)
        gateway.limits['max_usd_per_ticket'] = Decimal('.000001')
        settings = json.dumps({'env': dict(ANTHROPIC_BASE_URL=alternate_endpoint,
            ANTHROPIC_API_KEY=alternate.token, ANTHROPIC_AUTH_TOKEN=alternate.token)})
        controller, args, cfg, path = self.supervisor_fixture([executable, '-p', 'Reply OK', '--model', model,
            '--setting-sources', '', '--settings', settings, '--strict-mcp-config', '--mcp-config',
            '{"mcpServers":{}}', '--disallowedTools', 'WebSearch'])
        proxy = dict(HTTP_PROXY=alternate_endpoint, HTTPS_PROXY=alternate_endpoint, ALL_PROXY=alternate_endpoint,
            http_proxy=alternate_endpoint, https_proxy=alternate_endpoint, all_proxy=alternate_endpoint,
            NO_PROXY='localhost,127.0.0.1', no_proxy='localhost,127.0.0.1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
        with patch.dict(os.environ, proxy):
            self.run_supervisor(controller, args, cfg, gateway)
        self.assertEqual(alternate_transport.paid, 0)
        self.assertEqual(self.transport.paid, 0)
        self.assertTrue(gateway.stopped.is_set())
        self.assertRegex(gateway.reason, 'max_usd_per_ticket|ticket_budget_pause')
        self.assertFalse(any(e['kind'] == 'usage' for e in gateway.ledger.snapshot()))

    def test_design_attempts_do_not_exhaust_implementation_capacity(self):
        ledger = self.gateway.ledger
        for index in range(12):
            reservation = ledger.reserve(projected=Decimal(".001"), limits=self.gateway.limits,
                run_id=f"design-{index}", ticket="R-1", sprint="1", provider="anthropic",
                model="test", role="design-reviewer", logical_review_id=f"design-{index // 3}")
            ledger.release(reservation, f"design-{index}", "provider rejected")
        reservation = ledger.reserve(projected=Decimal(".001"), limits=self.gateway.limits,
            run_id="implementation", ticket="R-1", sprint="1", provider="anthropic",
            model="test", role="implementer")
        self.assertTrue(reservation)

    def test_code_phase_cap_preserves_security_review_capacity(self):
        ledger = self.gateway.ledger
        for index in range(3):
            context = dict(run_id=f"code-{index}", ticket="R-1", sprint="1",
                           provider="anthropic", model="test", role="code-reviewer")
            reservation = ledger.reserve(projected=Decimal(".001"), limits=self.gateway.limits,
                                         logical_review_id=f"code-{index}", **context)
            ledger.settle(reservation, response_id=f"response-{index}", usage={}, cost=Decimal(".001"), **context)
        with self.assertRaisesRegex(BudgetError, "logical review round ceiling"):
            ledger.reserve(projected=Decimal(".001"), limits=self.gateway.limits,
                logical_review_id="code-4", run_id="code-4", ticket="R-1", sprint="1",
                provider="anthropic", model="test", role="code-reviewer")
        self.assertTrue(ledger.reserve(projected=Decimal(".001"), limits=self.gateway.limits,
            logical_review_id="security-1", run_id="security-1", ticket="R-1", sprint="1",
            provider="anthropic", model="test", role="security-reviewer"))

    def test_logical_review_retries_share_capacity_but_remain_bounded(self):
        ledger = self.gateway.ledger
        for index in range(3):
            reservation = ledger.reserve(projected=Decimal(".01"), limits=self.gateway.limits,
                run_id=f"review-{index}", ticket="R-1", sprint="1", provider="anthropic",
                model="test", role="code-reviewer", logical_review_id="round-1")
            ledger.release(reservation, f"review-{index}", "invalid output")
        with self.assertRaisesRegex(BudgetError, "retry ceiling"):
            ledger.reserve(projected=Decimal(".01"), limits=self.gateway.limits,
                run_id="review-3", ticket="R-1", sprint="1", provider="anthropic",
                model="test", role="code-reviewer", logical_review_id="round-1")


if __name__ == "__main__":
    unittest.main()
