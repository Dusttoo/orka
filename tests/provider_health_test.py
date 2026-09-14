"""Shared failures block admission across tickets, without granting ticket capacity."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from provider_health import (
    bind_native_working_directory,
    ProviderHealth,
    HealthError,
    probe,
    subscription_child_environment,
    subscription_launch_command,
    validate_native_command,
)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.health = ProviderHealth(self.root)

    def test_outage_shared_and_probe_serialized(self):
        self.health.failure("openai", "rate_limited", retry_after=90)
        self.assertEqual(self.health.status("openai")["state"], "rate_limited")
        self.assertEqual(self.health.status("anthropic")["state"], "unverified")
        self.assertIsNone(self.health.claim_probe("openai"))
        with patch("provider_health.time.time", return_value=10**10):
            token = self.health.claim_probe("openai")
            self.assertTrue(token)
            self.assertIsNone(self.health.claim_probe("openai"))
            self.health.complete_probe("openai", token, "healthy", route="r")
            self.assertEqual(
                self.health.status("openai", route="r")["state"], "healthy"
            )

    def test_auth_never_cleared_by_normal_success_or_expiry(self):
        self.health.failure("anthropic", "authentication")
        with patch("provider_health.time.time", return_value=10**10):
            self.assertEqual(self.health.status("anthropic")["state"], "authentication")
        self.assertIsNone(self.health.claim_probe("anthropic"))
        token = self.health.claim_probe("anthropic", repair=True)
        self.health.complete_probe("anthropic", token, "healthy", route="r")
        self.assertEqual(
            self.health.status("anthropic", route="other")["state"], "unverified"
        )

    def test_old_probe_cannot_clear_new_incident(self):
        token = self.health.claim_probe("openai")
        self.health.failure("openai", "authentication")
        self.assertFalse(
            self.health.complete_probe("openai", token, "healthy", route="r")
        )
        self.assertEqual(self.health.status("openai")["state"], "authentication")

    def test_role_provider_model_and_overrides(self):
        route = dict(
            provider="anthropic",
            model="claude-test",
            execution="desktop",
            effort="high",
        )
        with self.assertRaises(HealthError):
            validate_native_command(["codex", "exec", "--model", "claude-test"], route)
        with self.assertRaises(HealthError):
            validate_native_command(["claude", "-p", "--model", "wrong"], route)
        with self.assertRaises(HealthError):
            validate_native_command(
                ["claude", "-p", "--model", "claude-test", "--settings", "evil.json"],
                route,
            )
        self.assertEqual(
            validate_native_command(["claude", "-p", "--model", "claude-test"], route)[
                -2:
            ],
            ["--effort", "high"],
        )

    def test_model_less_desktop_route_uses_client_default(self):
        route = dict(
            provider="openai", model="", execution="desktop", effort=""
        )
        with patch("provider_health.shutil.which", return_value="/bin/echo"):
            command = validate_native_command(["codex", "exec", "prompt"], route)
            self.assertEqual(command, ["/bin/echo", "exec", "prompt"])
            with self.assertRaisesRegex(HealthError, "default model"):
                validate_native_command(
                    ["codex", "exec", "--model", "gpt-test", "prompt"], route
                )

    def test_codex_working_directory_is_controller_bound(self):
        route = dict(provider="openai", model="", execution="desktop", effort="")
        expected = self.root / "authorized-worktree"
        command = bind_native_working_directory(
            [
                "codex",
                "exec",
                "--cd",
                "/wrong",
                "--cd=/also-wrong",
                "-C",
                "/short-wrong",
                "-Cattached-wrong",
                "prompt",
            ],
            route,
            expected,
        )
        self.assertEqual(command.count("--cd"), 1)
        self.assertEqual(command[command.index("--cd") + 1], str(expected.resolve()))
        self.assertNotIn("/wrong", command)
        self.assertNotIn("/short-wrong", command)
        self.assertFalse(any(arg.startswith("--cd=") for arg in command))
        self.assertFalse(any(arg == "-C" or arg.startswith("-C") for arg in command))

    def test_codex_worktree_override_is_rejected(self):
        route = dict(provider="openai", model="", execution="desktop", effort="")
        for command in (
            ["codex", "exec", "--worktree", "prompt"],
            ["codex", "exec", "--worktree=branch", "prompt"],
        ):
            with self.assertRaisesRegex(HealthError, "not controller-authorized"):
                bind_native_working_directory(command, route, self.root)

    def test_codex_options_after_sentinel_are_prompt_text(self):
        route = dict(provider="openai", model="", execution="desktop", effort="")
        command = bind_native_working_directory(
            ["codex", "exec", "--", "-C", "/prompt-text", "--worktree"],
            route,
            self.root,
        )
        self.assertEqual(command[-4:], ["--", "-C", "/prompt-text", "--worktree"])

    def test_codex_working_directory_rejects_missing_value(self):
        route = dict(provider="openai", model="", execution="desktop", effort="")
        with self.assertRaisesRegex(HealthError, "requires a value"):
            bind_native_working_directory(["codex", "exec", "--cd"], route, self.root)

    def test_model_less_desktop_probe_never_contacts_provider(self):
        config = self.root / "config.yaml"
        config.write_text(
            "llm:\n  execution: desktop\n  provider: openai\n  model: ''\n"
        )
        transport = Mock()
        login = Mock(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")
        with patch("provider_health.shutil.which", return_value="/bin/echo"), patch(
            "provider_health.subprocess.run", return_value=login
        ):
            state = probe(self.root, config, transport=transport)
        self.assertEqual(state["state"], "healthy")
        self.assertEqual(state["mode"], "subscription")
        self.assertEqual(state["client"], "codex")
        transport.request.assert_not_called()

    def test_model_less_codex_rejects_api_authentication(self):
        config = self.root / "config.yaml"
        config.write_text(
            "llm:\n  execution: desktop\n  provider: openai\n  model: ''\n"
        )
        login = Mock(returncode=0, stdout="Logged in using an API key\n", stderr="")
        with patch("provider_health.shutil.which", return_value="/bin/echo"), patch(
            "provider_health.subprocess.run", return_value=login
        ):
            state = probe(self.root, config)
        self.assertEqual(state["state"], "incompatible")
        self.assertIn("ChatGPT subscription", state["reason"])

    def test_subscription_environment_cannot_inherit_api_routing(self):
        environment = {
            "PATH": "/bin",
            "OPENAI_API_KEY": "secret",
            "CODEX_API_KEY": "secret",
            "OPENAI_BASE_URL": "https://api.example",
            "AZURE_OPENAI_API_KEY": "secret",
            "AZURE_OPENAI_ENDPOINT": "https://azure.example",
        }
        self.assertEqual(
            subscription_child_environment(environment, "openai"),
            {"PATH": "/bin"},
        )

    def test_subscription_launch_pins_client_configuration(self):
        login = Mock(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")
        with patch("provider_health.shutil.which", return_value="/bin/echo"), patch(
            "provider_health.subprocess.run", return_value=login
        ):
            codex = subscription_launch_command(
                ["codex", "exec", "--json", "prompt"],
                dict(provider="openai", model="", execution="desktop", effort=""),
            )
            with self.assertRaisesRegex(HealthError, "model-less"):
                subscription_launch_command(
                    ["claude", "-p", "prompt"],
                    dict(provider="anthropic", model="", execution="desktop", effort=""),
                )
        self.assertEqual(
            codex[2:5],
            ["--ignore-user-config", "-c", 'model_provider="openai"'],
        )
        self.assertEqual(
            subscription_child_environment(
                {
                    "PATH": "/bin",
                    "ANTHROPIC_API_KEY": "secret",
                    "ANTHROPIC_AUTH_TOKEN": "secret",
                    "ANTHROPIC_BASE_URL": "https://api.example",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "CLAUDE_CODE_USE_FOUNDRY": "1",
                },
                "anthropic",
            ),
            {"PATH": "/bin"},
        )

    def test_absent_token_never_clears_incident(self):
        self.health.failure("openai", "authentication")
        self.assertFalse(self.health.complete_probe("openai", None, "healthy"))
        self.assertEqual(self.health.status("openai")["state"], "authentication")

    def test_three_failed_probes_require_repair(self):
        for number in range(3):
            with patch("provider_health.time.time", return_value=1000 + number * 100):
                token = self.health.claim_probe("openai")
                self.assertTrue(token)
                self.health.complete_probe("openai", token, "transport")
        with patch("provider_health.time.time", return_value=2000):
            self.assertIsNone(self.health.claim_probe("openai"))
            self.assertTrue(self.health.claim_probe("openai", repair=True))

    def test_corrupt_evidence_fails_closed(self):
        self.health.directory.mkdir(parents=True)
        (self.health.directory / "openai.json").write_text("[]")
        with self.assertRaises(HealthError):
            self.health.status("openai")

    def test_authenticated_probe_honors_retry_after(self):
        from provider_health import probe
        from api_agent import ProviderHTTPError

        config = self.root / "config.yaml"
        config.write_text(
            "llm:\n  execution: api\n  provider: openai\n  model: gpt-test\n"
        )

        class Transport:
            def request(self, *args, **kwargs):
                raise ProviderHTTPError(429, "limited", retry_after_seconds=120)

        with patch("provider_health.time.time", return_value=1000):
            state = probe(self.root, config, transport=Transport())
        self.assertEqual(state["state"], "rate_limited")
        self.assertEqual(state["retry_at"], 1120)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        config = self.root / ".orchestration/config.yaml"
        config.parent.mkdir()
        config.write_text(
            "llm:\n  execution: desktop\n  provider: anthropic\n  model: claude-test\n  roles:\n    sprint-worker:\n      provider: openai\n      model: gpt-test\n"
        )
        spec = importlib.util.spec_from_file_location(
            "admission_controller",
            Path(__file__).resolve().parents[1] / "scripts/sprint-controller.py",
        )
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        from argparse import Namespace

        self.N = Namespace
        with patch.object(self.c, "project_root", return_value=self.root):
            self.cfg = self.c.settings(Namespace(config=str(config), state_dir=None))
        self.path = self.c.state_path(self.cfg["state_dir"], "1")
        self.ticket = dict(
            key="T-1",
            state="pending",
            raw_status="Ready",
            reason="",
            attempts=0,
            history=[],
            dependencies=[],
            subtasks=[],
            scope_assessment={"verdict": "ready"},
        )
        self.state = dict(
            schema_version=2,
            sprint={"id": "1"},
            tickets={"T-1": self.ticket},
            dependency_status={},
        )
        self.c.save(self.path, self.state)
        # Tests isolate absence of real root grants; no host authority is contacted.
        for name in ["authorized_restart_grant", "authorized_relaunch_ceiling"]:
            p = patch.object(self.c, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def healthy(self, role="sprint-worker"):
        from context_pipeline import llm_route_from_config
        from provider_health import route_identity

        route = llm_route_from_config(self.cfg["config"], role)
        health = ProviderHealth(self.root)
        token = health.claim_probe(route["provider"])
        self.assertTrue(token)
        health.complete_probe(
            route["provider"], token, "healthy", route_identity(route)
        )

    def reserve(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.c.reserve(
                self.N(
                    sprint="1",
                    ticket="T-1",
                    run_ref="first",
                    run_id="first",
                    role="implementer",
                    worker_ref="first",
                ),
                self.cfg,
            )

    def test_provider_hold_before_any_attempt_and_role_override(self):
        with self.assertRaisesRegex(self.c.SprintError, "provider admission held"):
            self.reserve()
        self.assertEqual(self.c.load(self.path)["tickets"]["T-1"]["attempts"], 0)
        self.healthy(
            "ticket-scoper"
        )  # Global Anthropic route cannot authorize the OpenAI override.
        with self.assertRaises(self.c.SprintError):
            self.reserve()
        self.healthy()
        self.reserve()
        stored = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(stored["reserved_route"]["provider"], "openai")
        self.assertEqual(stored["attempts"], 1)

    def test_wrong_launcher_preserves_unused_capability(self):
        self.healthy()
        self.reserve()
        ticket = self.c.load(self.path)["tickets"]["T-1"]
        args = self.N(
            sprint="1",
            ticket="T-1",
            command=["claude", "-p", "--model", "gpt-test"],
            output=str(self.root / "out"),
            stdin_file=None,
            attach_capability=ticket["attach_capability"],
        )
        with self.assertRaises(HealthError):
            self.c.launch_local(args, self.cfg)
        after = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(after["attach_capability"], ticket["attach_capability"])
        self.assertFalse(after["launch_evidence"])

    def test_model_less_desktop_admission_and_launch_use_subscription(self):
        config = Path(self.cfg["config"])
        config.write_text(
            "llm:\n  execution: desktop\n  provider: openai\n  model: ''\n"
        )
        binary = self.root / "bin/codex"
        binary.parent.mkdir()
        binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1 $2\" = \"login status\" ]; then\n"
            "  printf 'Logged in using ChatGPT\\n'\n"
            "  exit 0\n"
            "fi\n"
            "test -z \"${OPENAI_API_KEY+x}\" || exit 9\n"
            "test -z \"${CODEX_API_KEY+x}\" || exit 9\n"
            "test -z \"${OPENAI_BASE_URL+x}\" || exit 9\n"
            "test -z \"${AZURE_OPENAI_API_KEY+x}\" || exit 9\n"
            "test -z \"${AZURE_OPENAI_ENDPOINT+x}\" || exit 9\n"
            "printf '%s\\n' \"$@\" | grep -qx -- '--ignore-user-config' || exit 9\n"
            "printf '%s\\n' \"$@\" | grep -qx -- 'model_provider=\"openai\"' || exit 9\n"
            "printf subscription-clean\n"
        )
        binary.chmod(0o755)
        ready = self.root / "ready.json"
        ack = self.root / "ack"
        tombstone = self.root / "terminal.json"
        output = self.root / "output.log"
        ack.touch()
        args = self.N(
            command=[str(binary), "exec"],
            ready=str(ready),
            ack=str(ack),
            tombstone=str(tombstone),
            output=str(output),
            invocation_id="subscription-launch",
            ticket="T-1",
            sprint="1",
            stdin_file=None,
            subscription_route=True,
        )
        environment = {
            "PATH": str(binary.parent) + os.pathsep + os.environ.get("PATH", ""),
            "OPENAI_API_KEY": "must-not-reach-child",
            "CODEX_API_KEY": "must-not-reach-child",
            "OPENAI_BASE_URL": "https://api.example",
            "AZURE_OPENAI_API_KEY": "must-not-reach-child",
            "AZURE_OPENAI_ENDPOINT": "https://azure.example",
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertIsNone(self.c.runtime_admission(self.cfg))
            self.c.supervise_local(args, self.cfg)
        self.assertEqual(output.read_text(), "subscription-clean")
        self.assertEqual(self.c.read_json(ready, label="ready")["phase"], "terminal")

    def test_supervisor_rejects_worker_cwd_from_another_repository(self):
        other = self.root / "other"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.ticket.update(
            state="running",
            launch_evidence={
                "invocation_id": "wrong-worktree",
                "ticket": "T-1",
                "sprint": "1",
                "repository": str(self.root),
                "worker_cwd": str(other),
                "recovery_binding": None,
            },
        )
        self.c.save(self.path, self.state)
        args = self.N(
            command=["/bin/sh", "-c", "exit 0"],
            ready=str(self.root / "wrong-ready.json"),
            ack=str(self.root / "wrong-ack"),
            tombstone=str(self.root / "wrong-terminal.json"),
            output=str(self.root / "wrong-output.log"),
            invocation_id="wrong-worktree",
            ticket="T-1",
            sprint="1",
            stdin_file=None,
            subscription_route=False,
            worker_cwd=str(other),
        )
        with self.assertRaisesRegex(self.c.SprintError, "controller-authenticated checkout"):
            self.c.supervise_local(args, self.cfg)

    def test_supervisor_rejects_same_repository_wrong_worktree(self):
        self.ticket.update(
            state="running",
            launch_evidence={
                "invocation_id": "wrong-same-repo",
                "ticket": "T-1",
                "sprint": "1",
                "repository": str(self.root),
                "worker_cwd": str(self.root),
                "recovery_binding": None,
            },
        )
        self.c.save(self.path, self.state)
        args = self.N(
            command=["/bin/sh", "-c", "exit 0"],
            ready=str(self.root / "same-ready.json"),
            ack=str(self.root / "same-ack"),
            tombstone=str(self.root / "same-terminal.json"),
            output=str(self.root / "same-output.log"),
            invocation_id="wrong-same-repo",
            ticket="T-1",
            sprint="1",
            stdin_file=None,
            subscription_route=False,
            worker_cwd=str(self.root / "subdirectory"),
        )
        with self.assertRaisesRegex(self.c.SprintError, "persisted launch evidence"):
            self.c.supervise_local(args, self.cfg)

    def test_supervisor_revalidates_recovery_binding_immediately_before_spawn(self):
        binding = {"kind": "preserved_pr", "worktree": str(self.root)}
        self.ticket.update(
            state="running",
            recovery_binding=binding,
            launch_evidence={
                "invocation_id": "binding-drift",
                "ticket": "T-1",
                "sprint": "1",
                "repository": str(self.root),
                "worker_cwd": str(self.root),
                "recovery_binding": binding,
            },
        )
        self.c.save(self.path, self.state)
        ack = self.root / "binding-ack"
        ack.touch()
        marker = self.root / "worker-ran"
        args = self.N(
            command=["/bin/sh", "-c", f"touch {marker}"],
            ready=str(self.root / "binding-ready.json"),
            ack=str(ack),
            tombstone=str(self.root / "binding-terminal.json"),
            output=str(self.root / "binding-output.log"),
            invocation_id="binding-drift",
            ticket="T-1",
            sprint="1",
            stdin_file=None,
            subscription_route=False,
            worker_cwd=str(self.root),
        )
        with patch.object(
            self.c,
            "verify_recovery_binding",
            side_effect=[binding, self.c.SprintError("binding changed")],
        ) as verify:
            self.c.supervise_local(args, self.cfg)
        self.assertEqual(verify.call_count, 2)
        self.assertFalse(marker.exists())
        terminal = self.c.read_json(Path(args.tombstone), label="terminal")
        self.assertFalse(terminal["spawned"])
        self.assertIn("binding changed", terminal["error"])

    def test_subscription_route_drift_writes_terminal_without_spawning(self):
        config = Path(self.cfg["config"])
        config.write_text(
            "llm:\n  execution: desktop\n  provider: openai\n  model: gpt-test\n"
        )
        ready = self.root / "drift-ready.json"
        ack = self.root / "drift-ack"
        tombstone = self.root / "drift-terminal.json"
        output = self.root / "drift-output.log"
        ack.touch()
        args = self.N(
            command=["codex", "exec", "prompt"],
            ready=str(ready),
            ack=str(ack),
            tombstone=str(tombstone),
            output=str(output),
            invocation_id="subscription-drift",
            ticket="T-1",
            sprint="1",
            stdin_file=None,
            subscription_route=True,
        )
        self.c.supervise_local(args, self.cfg)
        terminal = self.c.read_json(tombstone, label="terminal")
        self.assertFalse(terminal["spawned"])
        self.assertIn("no longer matches", terminal["error"])

    def test_malformed_subscription_route_writes_terminal_without_spawning(self):
        config = Path(self.cfg["config"])
        config.write_text(
            "llm:\n  execution: desktop\n  provider: unsupported\n  model: ''\n"
        )
        ready = self.root / "malformed-ready.json"
        ack = self.root / "malformed-ack"
        tombstone = self.root / "malformed-terminal.json"
        output = self.root / "malformed-output.log"
        ack.touch()
        args = self.N(
            command=["codex", "exec", "prompt"],
            ready=str(ready),
            ack=str(ack),
            tombstone=str(tombstone),
            output=str(output),
            invocation_id="subscription-malformed",
            ticket="T-1",
            sprint="1",
            stdin_file=None,
            subscription_route=True,
        )
        self.c.supervise_local(args, self.cfg)
        terminal = self.c.read_json(tombstone, label="terminal")
        self.assertFalse(terminal["spawned"])
        self.assertIn("provider must be one of", terminal["error"])

    def test_scope_required_with_decomposition_disabled(self):
        self.healthy()
        self.healthy("ticket-scoper")
        self.ticket["scope_assessment"] = {}
        self.c.save(self.path, self.state)
        plan = self.c.plan_value(self.state, self.cfg)
        self.assertEqual(plan["scope"], ["T-1"])
        self.assertEqual(plan["launch"], [])
        with self.assertRaisesRegex(self.c.SprintError, "scoping"):
            self.reserve()

    def test_tracking_parent_binds_existing_children_without_attempt(self):
        import json
        import contextlib
        import io

        self.ticket["subtasks"] = ["T-2"]
        self.state["tickets"]["T-2"] = {**self.ticket, "key": "T-2", "subtasks": []}
        self.c.save(self.path, self.state)
        p = self.root / "assessment.json"
        p.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    ticket="T-1",
                    verdict="tracking_parent",
                    complexity_score=1,
                    reasons=["existing chain"],
                    slices=[],
                    children=["T-2"],
                )
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.c.record_scope(
                self.N(sprint="1", ticket="T-1", assessment=str(p)), self.cfg
            )
        after = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(after["state"], "decomposed")
        self.assertEqual(after["attempts"], 0)
        self.assertEqual(after["decomposition_children"], ["T-2"])
        self.assertFalse(
            self.c.dependency_complete(self.c.load(self.path), "T-1", self.cfg)
        )

    def test_shared_auth_hold_is_one_provider_problem_no_launch(self):
        self.healthy()
        ProviderHealth(self.root).failure("openai", "authentication")
        result = self.c.plan_value(self.state, self.cfg)
        self.assertFalse(result["launch"])
        self.assertTrue(
            any(h["state"] == "authentication" for h in result["provider_holds"])
        )
        self.assertFalse(
            any(h["provider"] == "openai" for h in result["health_probes"])
        )

    def test_completed_sprint_does_not_schedule_health_work(self):
        self.ticket["state"] = "completed"
        result = self.c.plan_value(self.state, self.cfg)
        self.assertFalse(result["health_probes"])
        self.assertFalse(result["autonomous_work_remaining"])

    def test_batch_provider_mismatch_consumes_no_attempt(self):
        import json

        jobs = self.root / "jobs.json"
        jobs.write_text(
            json.dumps({"provider": "anthropic", "jobs": [{"ticket": "T-1"}]})
        )
        with self.assertRaisesRegex(self.c.SprintError, "batch provider"):
            self.c.prepare_batch(self.N(sprint="1", jobs=str(jobs)), self.cfg)
        self.assertEqual(self.c.load(self.path)["tickets"]["T-1"]["attempts"], 0)

    def test_missing_prerequisite_stops_before_implementation(self):
        import json
        import contextlib
        import io

        assessment = self.root / "assessment.json"
        assessment.write_text(
            json.dumps(
                dict(
                    schema_version=1,
                    ticket="T-1",
                    verdict="ready",
                    complexity_score=0,
                    reasons=["requires another ticket"],
                    prerequisites=["T-2"],
                    slices=[],
                )
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.c.record_scope(
                self.N(sprint="1", ticket="T-1", assessment=str(assessment)), self.cfg
            )
        ticket = self.c.load(self.path)["tickets"]["T-1"]
        self.assertEqual(ticket["state"], "operator_decision")
        self.assertEqual(ticket["attempts"], 0)
        self.assertIn("T-2", ticket["reason"])

    def test_authenticated_link_repair_only_clears_dependency_decision(self):
        import contextlib
        import io
        import copy

        inventory = self.root / "inventory.json"
        inventory.write_text("{}")
        for decision, expected in [
            ("product", "operator_decision"),
            ("dependency_reconciliation", "pending"),
        ]:
            self.ticket.update(
                state="operator_decision",
                scope_assessment={
                    "verdict": "operator_decision",
                    "decision_kind": decision,
                    "missing_dependencies": ["T-2"],
                },
            )
            self.c.save(self.path, self.state)
            fresh = copy.deepcopy(self.c.load(self.path)["tickets"]["T-1"])
            fresh.update(state="pending", dependencies=["T-2"], scope_assessment={})
            incoming = dict(
                project="T",
                sprint={"id": "1"},
                source_query="parents",
                subtask_source_query="children",
                subtask_keys=[],
                dependency_status={"T-2": "Done"},
                tickets={"T-1": fresh},
            )
            with (
                patch.object(self.c, "normalized_inventory", return_value=incoming),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.c.sync(
                    self.N(inventory=str(inventory), inventory_template=None), self.cfg
                )
            result = self.c.load(self.path)["tickets"]["T-1"]
            self.assertEqual(result["state"], expected)
            self.assertEqual(result["attempts"], 0)
            if expected == "pending":
                self.assertFalse(result["scope_assessment"])

    def test_batch_wrong_model_is_rejected_before_budget_reservation(self):
        import json

        config = self.cfg["config"]
        config.write_text(
            "llm:\n  execution: api\n  provider: openai\n  model: gpt-test\n"
        )
        self.healthy()
        jobs = self.root / "jobs.json"
        jobs.write_text(
            json.dumps(
                dict(
                    provider="openai",
                    jobs=[
                        dict(
                            ticket="T-1",
                            background=True,
                            interactive=False,
                            params=dict(
                                model="wrong-model",
                                max_output_tokens=10,
                                input=[dict(role="user", content="test")],
                            ),
                        )
                    ],
                )
            )
        )
        with self.assertRaisesRegex(self.c.SprintError, "batch model"):
            self.c.prepare_batch(self.N(sprint="1", jobs=str(jobs)), self.cfg)
        self.assertEqual(self.c.load(self.path)["tickets"]["T-1"]["attempts"], 0)

    def test_api_attempt_rejects_a_changed_route(self):
        from attempt_capability import validate, AttemptCapabilityError

        self.healthy()
        self.reserve()
        item = self.c.load(self.path)["tickets"]["T-1"]
        args = dict(
            state_dir=self.cfg["state_dir"],
            token=item["attempt_capability"]["token"],
            repository=str(self.root),
            sprint="1",
            ticket="T-1",
            role="implementer",
            run_id="first",
            worker="first",
        )
        validate(**args, route=item["reserved_route"])
        with self.assertRaisesRegex(AttemptCapabilityError, "route"):
            validate(**args, route={**item["reserved_route"], "model": "wrong-model"})


class DependencyTests(unittest.TestCase):
    def test_explicit_sections_only(self):
        from ticket_dependencies import declared_dependencies

        self.assertEqual(
            declared_dependencies(
                "Related: T-99\nPrerequisites:\n- T-1 must merge\n- T-2\n\nNotes: T-88"
            ),
            ["T-1", "T-2"],
        )
        self.assertEqual(
            declared_dependencies(
                "Depends on: T-3, T-4\nImplementation references T-55"
            ),
            ["T-3", "T-4"],
        )
        self.assertEqual(
            declared_dependencies("Does not depend on T-1\nRelated changes: T-2"), []
        )


class InstalledClientTests(unittest.TestCase):
    @unittest.skipUnless(__import__("shutil").which("codex"), "Codex is not installed")
    def test_forced_compaction_and_tool_use_are_metered(self):
        from runtime_smoke import check

        result = check("openai", "gpt-5.5")
        self.assertEqual(result["compaction"], "metered-responses")
        self.assertGreaterEqual(result["generations"], 3)


if __name__ == "__main__":
    unittest.main()
