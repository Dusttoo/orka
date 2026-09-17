"""Native Codex budget admission, Responses framing, and optional CLI smoke test."""
import argparse
import importlib.util
import signal
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from api_agent import AgentError, BudgetError, ProviderAmbiguous, ProviderHTTPError, UsageLedger
from native_gateway import ClientIncompatibleError
from codex_gateway import CodexGateway, child_environment, install_launcher, launch_arguments, response_events


class FakeTransport:
    def __init__(self, tools=False):
        self.calls = []
        self.paid = 0
        self.tools = tools
        self.saw_tool_result = False
        self.failure = None

    def request(self, provider, path, payload, **kwargs):
        self.calls.append((path, copy.deepcopy(payload)))
        if path.endswith("input_tokens"):
            return {"input_tokens": 100}
        self.paid += 1
        if self.failure:
            raise self.failure
        if path == "responses/compact":
            return dict(id=f"cmp_{self.paid}", object="response.compaction", created_at=1,
                        output=[dict(type="compaction", encrypted_content="opaque")],
                        usage=dict(input_tokens=100, output_tokens=7, total_tokens=107,
                                   input_tokens_details=dict(cached_tokens=10),
                                   output_tokens_details=dict(reasoning_tokens=1)))
        output = [dict(id=f"msg_{self.paid}", type="message", role="assistant", status="completed",
                       content=[dict(type="output_text", text="ORKA_OFFLINE_OK", annotations=[])])]
        if self.tools and self.paid == 1:
            output = [dict(id="fc_1", type="function_call", call_id="call_1", name="exec_command",
                           arguments=json.dumps(dict(cmd="printf ORKA_TOOL_OK", max_output_tokens=100)), status="completed")]
        if self.tools and self.paid == 2:
            self.saw_tool_result = any(item.get("type") == "function_call_output" and
                "ORKA_TOOL_OK" in str(item.get("output")) for item in payload.get("input", []) if isinstance(item, dict))
        return dict(id=f"resp_{self.paid}", object="response", created_at=1, model=payload["model"],
                    status="completed", output=output, usage=dict(input_tokens=100, output_tokens=5,
                    total_tokens=105, input_tokens_details=dict(cached_tokens=20), output_tokens_details=dict(reasoning_tokens=2)))


class CodexGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"llm": {"pricing": {"gpt-5.5": dict(input_per_mtok=1, cache_write_per_mtok=1,
                                cache_read_per_mtok=1, output_per_mtok=1)}}}
        self.transport = FakeTransport()
        self.gateway = CodexGateway(self.root, self.config, "T-1", "1", "native", self.transport)
        self.payload = dict(model="gpt-5.5", input="hello", stream=True, tools=[])

    def test_envelope_caps_output_and_settles_cached_and_reasoning_tokens(self):
        response = self.gateway.request("/v1/responses", self.payload)
        forwarded = self.transport.calls[-1][1]
        self.assertFalse(forwarded["stream"])
        self.assertFalse(forwarded["store"])
        self.assertEqual(forwarded["max_output_tokens"], 4096)
        self.assertEqual(forwarded["service_tier"], "default")
        usage = self.gateway.ledger.snapshot()[-1]
        self.assertEqual(usage["input_tokens"], 80)
        self.assertEqual(usage["cache_read_tokens"], 20)
        self.assertEqual(usage["reasoning_tokens"], 2)
        self.assertEqual(list(response_events(response))[-1]["type"], "response.completed")

    def test_over_budget_ticket_does_not_reach_upstream_but_another_can(self):
        self.gateway.limits["max_usd_per_ticket"] = Decimal(".001")
        with self.assertRaises(BudgetError):
            self.gateway.request("/v1/responses", self.payload)
        self.assertEqual(self.transport.paid, 0)
        other = CodexGateway(self.root, self.config, "T-2", "1", "other", self.transport)
        self.assertEqual(other.request("/v1/responses", self.payload)["status"], "completed")

    def test_ambiguous_submission_stays_reserved_and_rejection_releases(self):
        self.transport.failure = ProviderAmbiguous("lost connection")
        with self.assertRaises(ProviderAmbiguous):
            self.gateway.request("/v1/responses", self.payload)
        self.assertEqual(len(UsageLedger._totals(self.gateway.ledger.snapshot())[1]), 1)
        other = CodexGateway(self.root, self.config, "T-2", "1", "other", self.transport)
        self.transport.failure = ProviderHTTPError(429, "rejected")
        with self.assertRaises(ProviderHTTPError):
            other.request("/v1/responses", self.payload)
        self.assertEqual(len(UsageLedger._totals(self.gateway.ledger.snapshot())[1]), 1)

    def test_missing_or_invalid_terminal_usage_keeps_reservation(self):
        original = self.transport.request
        for malformed in (None, {"input_tokens": 100, "output_tokens": 5,
                                 "input_tokens_details": {"cached_tokens": 101}}):
            def request(provider, path, payload, **kwargs):
                result = original(provider, path, payload, **kwargs)
                if path == "responses":
                    result["usage"] = malformed
                return result
            with patch.object(self.transport, "request", side_effect=request), self.assertRaises(AgentError):
                self.gateway.request("/v1/responses", self.payload)
        self.assertEqual(len(UsageLedger._totals(self.gateway.ledger.snapshot())[1]), 2)

    def test_unpriced_paid_or_stateful_features_are_rejected_before_submission(self):
        cases = [dict(tools=[dict(type="web_search")]), dict(tools=[dict(type="tool_search", execution="server")]),
                 dict(service_tier="priority"), dict(previous_response_id="old"), dict(background=True),
                 dict(context_management=[dict(type="compaction")]), dict(model="unpriced"),
                 dict(input=[dict(type="tool_search_output", execution="client", tools=[dict(type="web_search")])])]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(AgentError):
                self.gateway.request("/v1/responses", {**self.payload, **case})
        compact = {key: value for key, value in self.payload.items() if key != "stream"}
        for case in (dict(previous_response_id="old"), dict(stream=True), dict(background=True),
                     dict(tools=[dict(type="web_search")]), dict(service_tier="priority")):
            with self.subTest(compact=case), self.assertRaises(AgentError):
                self.gateway.request("/v1/responses/compact", {**compact, **case})
        with self.assertRaises(ClientIncompatibleError):
            self.gateway.request("/v1/responses/other", self.payload)
        self.assertEqual(self.transport.calls, [])

    def test_remote_compaction_is_metered_like_responses(self):
        payload = dict(model="gpt-5.5", input=[dict(type="message", role="user", content="history")],
                       instructions="summarize", tools=[], parallel_tool_calls=False,
                       client_metadata={"x-codex-installation-id": "local"})
        endpoint = self.gateway.start()
        self.addCleanup(self.gateway.close)
        request = urllib.request.Request(endpoint + "/v1/responses/compact", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.gateway.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as reply:
            self.assertEqual(reply.headers["Content-Type"], "application/json")
            body = json.loads(reply.read())
        self.assertEqual(body["output"], [dict(type="compaction", encrypted_content="opaque")])
        self.assertEqual([path for path, _ in self.transport.calls], ["responses/input_tokens", "responses/compact"])
        count, forwarded = self.transport.calls[0][1], self.transport.calls[1][1]
        self.assertEqual(count["input"], payload["input"])
        self.assertNotIn("client_metadata", forwarded)
        self.assertNotIn("max_output_tokens", forwarded)
        events = self.gateway.ledger.snapshot()
        reservation = next(e for e in events if e["kind"] == "reservation")
        usage = next(e for e in events if e["kind"] == "usage")
        self.assertEqual(reservation["origin"], "codex-gateway")
        self.assertEqual(usage["reservation_id"], reservation["reservation_id"])
        self.assertEqual((usage["response_id"], usage["input_tokens"], usage["cache_read_tokens"],
                          usage["output_tokens"]), ("cmp_1", 90, 10, 7))
        self.assertEqual(UsageLedger._totals(events)[1], {})
        self.assertFalse(self.gateway.stopped.is_set())

    def test_compaction_without_usage_evidence_retains_reservation(self):
        original = self.transport.request
        payload = dict(model="gpt-5.5", input="history")
        for malformed in (None, {"input_tokens": 100}, {"input_tokens": 1, "output_tokens": 1,
                          "input_tokens_details": {"cached_tokens": 2}}):
            def request(provider, path, body, **kwargs):
                result = original(provider, path, body, **kwargs)
                if path == "responses/compact":
                    result["usage"] = malformed
                return result
            with self.subTest(usage=malformed), patch.object(self.transport, "request", side_effect=request), \
                    self.assertRaises(AgentError):
                self.gateway.request("/v1/responses/compact", payload)
        events = self.gateway.ledger.snapshot()
        self.assertFalse(any(e["kind"] == "usage" for e in events))
        self.assertEqual(len(UsageLedger._totals(events)[1]), 3)
        self.transport.failure = ProviderHTTPError(400, "rejected")
        with self.assertRaises(ProviderHTTPError):
            self.gateway.request("/v1/responses/compact", payload)
        self.assertEqual(len(UsageLedger._totals(self.gateway.ledger.snapshot())[1]), 3)

    def test_unsupported_endpoint_incident_is_scoped_to_the_gateway_route(self):
        from provider_health import ProviderHealth, ProviderTransport
        gateway = CodexGateway(self.root, self.config, "T-1", "1", "native", self.transport, route_scope="route-a")
        sibling = CodexGateway(self.root, self.config, "T-2", "1", "sibling", self.transport, route_scope="route-a")
        endpoint = gateway.start()
        self.addCleanup(gateway.close)
        request = urllib.request.Request(endpoint + "/v1/responses/unknown?x=1", data=json.dumps(self.payload).encode(),
            headers={"Authorization": "Bearer " + gateway.token, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request, timeout=5)
        rejected.exception.close()
        self.assertTrue(gateway.stopped.is_set())
        self.assertTrue(gateway.reason.startswith("client_incompatible:"))
        self.assertIn("/v1/responses/unknown", gateway.reason)
        self.assertNotIn("x=1", gateway.reason)
        health = ProviderHealth(self.root)
        self.assertEqual(health.status("openai")["state"], "unverified")
        held = health.status("openai", route="route-a")
        self.assertEqual((held["state"], held["scope"], held["client"]), ("incompatible", "route", "codex-gateway"))
        self.assertEqual(health.status("openai", route="api-reviewer-route")["state"], "unverified")
        # A running sibling lane and an API role never touched the endpoint.
        self.assertEqual(sibling.request("/v1/responses", self.payload)["status"], "completed")
        self.assertEqual(ProviderTransport(self.root, self.transport).request(
            "openai", "responses", self.payload)["status"], "completed")

    def test_client_tool_search_and_namespaced_local_tools_are_allowed(self):
        payload = {**self.payload, "tools": [dict(type="tool_search", execution="client"),
                   dict(type="namespace", name="local", tools=[dict(type="function", name="read")])]}
        self.assertEqual(self.gateway.request("/v1/responses", payload)["status"], "completed")

    def test_stream_preserves_custom_tool_input_and_incomplete_terminal(self):
        response = self.transport.request("openai", "responses", self.payload)
        response["status"] = "incomplete"
        response["output"] = [dict(id="custom", type="custom_tool_call", call_id="call", name="apply_patch", input="patch")]
        events = list(response_events(response))
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))
        self.assertTrue(any(e.get("type") == "response.custom_tool_call_input.done" and e.get("input") == "patch" for e in events))
        self.assertEqual(events[-1]["type"], "response.incomplete")

    def test_routing_overrides_rejected_without_rejecting_prompt_text(self):
        for args in (["--config", 'model_provider="other"'], ["--config=openai_base_url=elsewhere"], ["--oss"]):
            with self.assertRaises(AgentError):
                launch_arguments(["codex", "exec", *args], "http://localhost:1")
        self.assertTrue(launch_arguments(["codex", "exec", "Explain model_provider configuration"], "http://localhost:1"))
        env = child_environment({"OPENAI_API_KEY": "real", "CODEX_API_KEY": "real", "PATH": "bin"}, "temporary", "http://localhost:1")
        self.assertNotIn("real", env.values())
        self.assertNotIn("CODEX_API_KEY", env)

    def test_supervisor_injects_metered_provider_and_replaces_real_credential(self):
        spec = importlib.util.spec_from_file_location("codex_test_controller", ROOT / "scripts/sprint-controller.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        executable = self.root / "codex"
        executable.write_text("#!" + sys.executable + "\nimport os,sys,json\n" +
            "print(json.dumps({'args': sys.argv[1:], 'key': os.environ['OPENAI_API_KEY'], " +
            "'gateway': os.environ['ORKA_NATIVE_GATEWAY_URL']}))\n")
        executable.chmod(0o700)
        config = self.root / "config.yaml"
        config.write_text("max_worker_seconds: 10\n")
        ack = self.root / "ack"
        ack.touch()
        args = argparse.Namespace(command=[str(executable), "exec", "hello"],
            ready=str(self.root / "ready.json"), ack=str(ack), tombstone=str(self.root / "terminal.json"),
            output=str(self.root / "output"), invocation_id="unit", stdin_file=None, ticket="T-1", sprint="1")
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-controller-secret"}), patch(
                    "codex_gateway.CodexGateway", return_value=self.gateway):
                controller.supervise_local(args, dict(config=config, shared_root=self.root, state_dir=self.root / "state"))
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
        output = json.loads(Path(args.output).read_text())
        self.assertEqual(output["key"], self.gateway.token)
        self.assertIn('model_provider="orka_metered"', output["args"])
        self.assertTrue(output["gateway"].startswith("http://127.0.0.1:"))
        self.assertEqual(json.loads(Path(args.tombstone).read_text())["returncode"], 0)

    def test_supervisor_requeues_only_the_lane_that_hit_an_unsupported_endpoint(self):
        from context_pipeline import llm_route_from_config
        from provider_health import ProviderHealth, route_identity
        spec = importlib.util.spec_from_file_location("codex_scope_controller", ROOT / "scripts/sprint-controller.py")
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        executable = self.root / "codex"
        executable.write_text("#!" + sys.executable + "\nimport os,time,urllib.request,urllib.error\n" +
            "request = urllib.request.Request(os.environ['ORKA_NATIVE_GATEWAY_URL'] + '/v1/unmetered', data=b'{}', " +
            "headers={'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'], 'Content-Type': 'application/json'})\n" +
            "try:\n    urllib.request.urlopen(request, timeout=5)\nexcept urllib.error.HTTPError:\n    pass\n" +
            "time.sleep(30)\n")
        executable.chmod(0o700)
        config = self.root / "config.yaml"
        config.write_text("max_worker_seconds: 20\n")
        ack = self.root / "ack"
        ack.touch()
        args = argparse.Namespace(command=[str(executable), "exec", "hello"],
            ready=str(self.root / "ready.json"), ack=str(ack), tombstone=str(self.root / "terminal.json"),
            output=str(self.root / "output"), invocation_id="unit", stdin_file=None, ticket="T-1", sprint="1")
        cfg = dict(config=config, shared_root=self.root, state_dir=self.root / "state",
            max_usd_without_progress=5, pause_usd_per_ticket=20, warn_usd_per_ticket=10,
            max_model_runs_per_ticket=12, max_reviewer_runs_per_ticket=6, max_lane_relaunches=2)
        lanes = {key: dict(key=key, state="running", attempts=1, attempt_token="cap-" + key, history=[],
                           launch_evidence=dict(invocation_id=invocation))
                 for key, invocation in (("T-1", "unit"), ("T-2", "sibling"))}
        path = controller.state_path(cfg["state_dir"], "1")
        controller.save(path, dict(schema_version=2, sprint=dict(id="1"), dependency_status={}, tickets=lanes))
        created = []
        def build(*args, **kwargs):
            created.append(kwargs)
            return CodexGateway(*args, **{**kwargs, "transport": self.transport})
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-controller-secret"}), patch(
                    "codex_gateway.CodexGateway", side_effect=build):
                controller.supervise_local(args, cfg)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
        scope = route_identity(llm_route_from_config(config, "sprint-worker"))
        self.assertEqual(created[0]["route_scope"], scope)
        state = controller.load(path)["tickets"]
        self.assertEqual(state["T-1"]["state"], "recoverable")
        self.assertTrue(state["T-1"]["reason"].startswith("client_incompatible:"))
        self.assertEqual(state["T-1"]["attempt_token"], "cap-T-1")
        self.assertEqual(state["T-2"]["state"], "running")
        health = ProviderHealth(self.root)
        self.assertEqual(health.status("openai")["state"], "unverified")
        self.assertEqual(health.status("openai", route=scope)["state"], "incompatible")
        self.assertEqual(self.transport.paid, 0)

    @unittest.skipUnless(shutil.which("codex"), "native Codex CLI not installed")
    def test_installed_cli_executes_local_tool_through_nested_launcher(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.transport.tools = True
        endpoint = self.gateway.start()
        self.addCleanup(self.gateway.close)
        executable = shutil.which("codex")
        env = child_environment(os.environ, self.gateway.token, endpoint)
        env.update(HTTP_PROXY=endpoint, HTTPS_PROXY=endpoint, ALL_PROXY=endpoint,
                   http_proxy=endpoint, https_proxy=endpoint, all_proxy=endpoint,
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        install_launcher(self.root / "bin", executable, env)
        # Invoke the same PATH shim inherited by ordinary shell children.
        result = subprocess.run([str(self.root / "bin/codex"), "exec", "--ignore-user-config", "--ephemeral",
            "--sandbox", "read-only", "--json", "--model", "gpt-5.5", "-c", 'approval_policy="never"',
            "Run the requested local tool, then reply ORKA_OFFLINE_OK."], cwd=self.root,
            env=env, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-1000:])
        self.assertIn("ORKA_OFFLINE_OK", result.stdout)
        self.assertTrue(self.transport.saw_tool_result)
        self.assertEqual(self.transport.paid, 2)
        usage = [e for e in self.gateway.ledger.snapshot() if e["kind"] == "usage"]
        self.assertEqual(len(usage), 2)
        self.assertTrue(all(e["ticket"] == "T-1" for e in usage))


if __name__ == "__main__":
    unittest.main()
