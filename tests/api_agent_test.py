#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "api_agent", ROOT / "scripts" / "api_agent.py"
)
assert SPEC and SPEC.loader
api_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = api_agent
SPEC.loader.exec_module(api_agent)

CLEAN_REVIEW = json.dumps(
    {
        "schema_version": 1,
        "gate": "code-review",
        "verdict": "PASS",
        "checks": [{"name": "diff", "status": "pass"}],
        "findings": [],
    },
    separators=(",", ":"),
)


BLOCKING_REVIEW = json.dumps(
    {
        "schema_version": 1,
        "gate": "code-review",
        "verdict": "FAIL",
        "checks": [{"name": "diff", "status": "pass"}],
        "findings": [
            {
                "component": "src/app.py:handler",
                "disposition": "blocking",
                "severity": "high",
                "title": "unchecked input",
                "explanation": "handler trusts the request body",
                "regression": False,
            }
        ],
    },
    separators=(",", ":"),
)


DEFAULT_TEST_BUDGETS = """    max_usd_per_run: 1.00
    max_usd_per_ticket: 2.00
    max_usd_per_sprint: 5.00
    max_output_tokens_per_turn: 100
    max_tool_rounds: 3
    max_tool_output_chars: 2000
    tool_timeout_seconds: 10
    provider_read_timeout_seconds: 777
    max_pre_ack_retries: 2
    retry_backoff_seconds: 0"""
DEFAULT_TEST_PRICING = """      input_per_mtok: 1
      cache_write_per_mtok: 2
      cache_read_per_mtok: 0.1
      output_per_mtok: 10"""
# Shaped like the gpt-6-astra review incident: a 32768-token output allowance
# reserved about $2.66 per request against a $5 review envelope.
INCIDENT_PRICING = """      input_per_mtok: 10
      cache_write_per_mtok: 10
      cache_read_per_mtok: 1
      output_per_mtok: 75"""


def incident_budgets(**overrides):
    values = {
        "max_usd_per_run": "10.00",
        "max_usd_per_ticket": "30.00",
        "max_usd_per_sprint": "300.00",
        "max_output_tokens_per_turn": "32768",
        "max_tool_rounds": "40",
        "max_tool_output_chars": "2000",
        "tool_timeout_seconds": "10",
        "max_pre_ack_retries": "0",
        "retry_backoff_seconds": "0",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    return "\n".join(f"    {key}: {value}" for key, value in values.items())


class FakeTransport:
    def __init__(self, responses, count=100):
        self.responses = list(responses)
        self.count = count
        self.calls = []

    def request(self, provider, path, payload, idempotency_key=None):
        self.calls.append((provider, path, payload, idempotency_key))
        if path.endswith("count_tokens") or path.endswith("input_tokens"):
            return {"input_tokens": self.count}
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeBedrockClient:
    def __init__(self, *, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.calls = []

    def count_tokens(self, **payload):
        self.calls.append(("count_tokens", payload))
        if self.error:
            raise self.error
        return self.response

    def converse(self, **payload):
        self.calls.append(("converse", payload))
        if self.error:
            raise self.error
        return self.response


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class ApiAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-qm",
                "initial",
            ],
            check=True,
        )
        (self.root / ".orchestration").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def config(
        self, provider="anthropic", model="test-model", extra="", budgets="", pricing=""
    ):
        path = self.root / ".orchestration" / "config.yaml"
        roles = (
            extra
            or "    code-reviewer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check]"
        )
        path.write_text(
            f"""schema_version: 1
require_review_authorization: false
llm:
  execution: api
  provider: {provider}
  model: {model}
  fallback: none
  budgets:
{budgets or DEFAULT_TEST_BUDGETS}
  pricing:
    {model}:
{pricing or DEFAULT_TEST_PRICING}
  roles:
{roles}
self_check:
  - name: smoke
    run: git status --short
""",
            encoding="utf-8",
        )
        return path

    def phase_permit(self, ticket="PROJ-1", role="code-reviewer", pr="1"):
        ledger_path = self.root / ".orchestration/.review-ledger" / f"pr-{pr}.json"
        if not ledger_path.exists():
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/review-ledger.py"), "open", pr],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
            )
        head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        permit = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/review-ledger.py"),
                "permit-review",
                pr,
                "--role",
                role,
                "--head",
                head,
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(permit.stdout)["review_phase_permit"]

    def agent(
        self,
        transport,
        provider="anthropic",
        role="code-reviewer",
        run_id="test-run",
        **config,
    ):
        review = {}
        if role in {"design-reviewer", "code-reviewer", "security-reviewer"}:
            review = {
                "review_authorization": self.phase_permit(role=role),
                "review_pr": "1",
            }
        worker = {}
        if role in {"implementer", "sprint-worker"}:
            worker = self.attempt_capability(run_id=run_id, role=role)
        return api_agent.ApiAgent(
            root=self.root,
            config_path=self.config(provider=provider, **config),
            role=role,
            ticket="PROJ-1",
            sprint="SPRINT-1",
            run_id=run_id,
            transport=transport,
            **review,
            **worker,
        )

    def attempt_capability(
        self,
        *,
        run_id,
        role="implementer",
        ticket="PROJ-1",
        sprint="SPRINT-1",
        worker_ref=None,
    ):
        token = "attemptcap_" + run_id.replace("-", "_")
        worker = worker_ref or run_id
        directory = self.root / ".orchestration/.sprint-state"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"test-{run_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "tickets": {
                        ticket: {
                            "state": "running",
                            "attempt_capability": {
                                "token": token,
                                "repository": str(self.root.resolve()),
                                "sprint": sprint,
                                "ticket": ticket,
                                "role": role,
                                "run_id": run_id,
                                "worker": worker,
                                "attempt": 1,
                            },
                            "attempts": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return {"attempt_capability": token, "worker_ref": worker}

    def test_repository_env_loads_provider_credentials_without_overriding_container(
        self,
    ):
        config = self.config()
        (config.parent / ".env").write_text(
            "# local orchestration secrets\n"
            "export ANTHROPIC_API_KEY='repo-key'\n"
            "ANTHROPIC_BASE_URL=https://proxy.example/v1 # optional proxy\n"
            "OPENAI_API_KEY=repo-openai\n"
            "AZURE_ADM_API_KEY=repo-azure\n"
            "AZURE_ADM_BASE_URL=https://resource.openai.azure.com/openai/v1\n"
            "PATH=/untrusted/path\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "container-key"}, clear=False
        ):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("AZURE_ADM_API_KEY", None)
            os.environ.pop("AZURE_ADM_BASE_URL", None)
            loaded = api_agent.load_orchestration_env(config)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "container-key")
            self.assertEqual(
                os.environ["ANTHROPIC_BASE_URL"], "https://proxy.example/v1"
            )
            self.assertEqual(os.environ["OPENAI_API_KEY"], "repo-openai")
            self.assertEqual(os.environ["AZURE_ADM_API_KEY"], "repo-azure")
            self.assertEqual(
                os.environ["AZURE_ADM_BASE_URL"],
                "https://resource.openai.azure.com/openai/v1",
            )
            self.assertNotEqual(os.environ.get("PATH"), "/untrusted/path")
            self.assertEqual(
                loaded,
                [
                    "ANTHROPIC_BASE_URL",
                    "OPENAI_API_KEY",
                    "AZURE_ADM_API_KEY",
                    "AZURE_ADM_BASE_URL",
                ],
            )

    @unittest.skipUnless(
        importlib.util.find_spec("botocore"), "optional Bedrock SDK absent"
    )
    def test_bedrock_transport_uses_request_metadata_and_aws_request_id(self):
        client = FakeBedrockClient(
            response={
                "ResponseMetadata": {"RequestId": "aws-request-123"},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "output": {
                    "message": {"role": "assistant", "content": [{"text": "ok"}]}
                },
            }
        )
        transport = api_agent.HttpTransport(bedrock_client=client)
        response = transport.request(
            "bedrock",
            "converse",
            {
                "modelId": "global.anthropic.claude-sonnet-5",
                "messages": [{"role": "user", "content": [{"text": "hello"}]}],
                "inferenceConfig": {"maxTokens": 100},
            },
            idempotency_key="resv_123",
        )
        self.assertEqual(response["id"], "aws-request-123")
        self.assertEqual(
            client.calls[0][1]["requestMetadata"]["orchestrationReservation"],
            "resv_123",
        )

    @unittest.skipUnless(
        importlib.util.find_spec("botocore"), "optional Bedrock SDK absent"
    )
    def test_bedrock_transport_retries_only_explicit_rejections(self):
        from botocore.exceptions import ClientError

        throttled = ClientError(
            {
                "Error": {"Code": "ThrottlingException", "Message": "slow down"},
                "ResponseMetadata": {"HTTPStatusCode": 429},
            },
            "Converse",
        )
        with self.assertRaisesRegex(api_agent.ProviderHTTPError, "HTTP 429"):
            api_agent.HttpTransport(
                bedrock_client=FakeBedrockClient(error=throttled)
            ).request("bedrock", "converse", {})
        uncertain = ClientError(
            {
                "Error": {"Code": "InternalServerException", "Message": "unknown"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "Converse",
        )
        with self.assertRaises(api_agent.ProviderAmbiguous):
            api_agent.HttpTransport(
                bedrock_client=FakeBedrockClient(error=uncertain)
            ).request("bedrock", "converse", {})

    @unittest.skipUnless(
        importlib.util.find_spec("boto3") and importlib.util.find_spec("botocore"),
        "optional Bedrock SDK absent",
    )
    def test_bedrock_mantle_transport_signs_with_aws_credentials(self):
        from botocore.credentials import Credentials

        session = mock.Mock()
        session.region_name = "us-east-1"
        session.get_credentials.return_value = Credentials(
            "access-key", "secret-key", "session-token"
        )
        response = FakeHTTPResponse(
            {
                "id": "chat-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )
        with (
            mock.patch("boto3.Session", return_value=session),
            mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            result = api_agent.HttpTransport().request(
                "bedrock_mantle",
                "chat/completions",
                {"model": "zai.glm-5", "messages": [], "max_tokens": 10},
                idempotency_key="resv_123",
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(result["id"], "chat-1")
        self.assertEqual(
            request.full_url,
            "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions",
        )
        self.assertTrue(request.headers["Authorization"].startswith("AWS4-HMAC-SHA256"))
        self.assertEqual(request.headers["X-amz-security-token"], "session-token")
        self.assertEqual(request.headers["Idempotency-key"], "resv_123")

    def test_repository_env_rejects_shell_syntax(self):
        config = self.config()
        (config.parent / ".env").write_text("source ../secrets\n", encoding="utf-8")
        with self.assertRaisesRegex(api_agent.AgentError, "expected KEY=value"):
            api_agent.load_orchestration_env(config)

    def test_anthropic_base_url_accepts_host_or_v1_form(self):
        response = FakeHTTPResponse({"id": "msg_1"})
        for base in ("https://api.anthropic.com", "https://api.anthropic.com/v1/"):
            with (
                self.subTest(base=base),
                mock.patch.dict(
                    os.environ,
                    {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": base},
                    clear=False,
                ),
                mock.patch("urllib.request.urlopen", return_value=response) as urlopen,
            ):
                api_agent.HttpTransport().request("anthropic", "messages", {})
                request = urlopen.call_args.args[0]
                self.assertEqual(
                    request.full_url, "https://api.anthropic.com/v1/messages"
                )

    def test_anthropic_tool_loop_and_usage(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_1",
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "git_status",
                            "input": {},
                        }
                    ],
                },
                {
                    "id": "msg_2",
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 20,
                        "cache_read_input_tokens": 80,
                        "output_tokens": 5,
                    },
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport)
        result = agent.run(
            {
                "model": "test-model",
                "max_tokens": 500,
                "system": [],
                "messages": [{"role": "user", "content": "review"}],
            }
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review"]["verdict"], "PASS")
        message_calls = [call for call in transport.calls if call[1] == "messages"]
        self.assertEqual(len(message_calls), 2)
        self.assertEqual(message_calls[0][2]["max_tokens"], 100)
        tool_names = {tool["name"] for tool in message_calls[0][2]["tools"]}
        self.assertNotIn("apply_patch", tool_names)
        tool_payload = json.dumps(message_calls[0][2]["tools"])
        self.assertNotIn('"strict"', tool_payload)
        self.assertIn('"maxItems"', tool_payload)
        self.assertIn('"minimum"', tool_payload)
        self.assertEqual(
            message_calls[1][2]["messages"][-1]["content"][0]["type"], "tool_result"
        )
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 120)
        self.assertEqual(summary["cache_read_tokens"], 80)
        self.assertEqual(summary["output_tokens"], 15)
        self.assertEqual(summary["open_reservations"], [])

    def test_rolling_cache_breakpoint_moves_to_the_conversation_end(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_1",
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "git_status",
                            "input": {},
                        }
                    ],
                },
                {
                    "id": "msg_2",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport)
        agent.run(
            {
                "model": "test-model",
                "max_tokens": 500,
                "system": [],
                "messages": [{"role": "user", "content": "review"}],
            }
        )
        resubmitted = [call for call in transport.calls if call[1] == "messages"][1][2][
            "messages"
        ]
        marked = [
            (index, block)
            for index, message in enumerate(resubmitted)
            for block in message["content"]
            if isinstance(block, dict) and "cache_control" in block
        ]
        # Exactly one breakpoint, on the newest block: system and tools already
        # spend two of Anthropic's four, and adding one per round would exceed it.
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0][0], len(resubmitted) - 1)
        self.assertEqual(
            resubmitted[-1]["content"][-1]["cache_control"], {"type": "ephemeral"}
        )

    def test_rolling_cache_breakpoint_does_not_mutate_provider_content(self):
        assistant_content = [{"type": "text", "text": "prior"}]
        messages = [
            {"role": "user", "content": "review"},
            {"role": "assistant", "content": assistant_content},
        ]
        rolled = api_agent.roll_conversation_cache_breakpoint(messages)
        self.assertEqual(
            rolled[-1]["content"][-1]["cache_control"], {"type": "ephemeral"}
        )
        self.assertNotIn("cache_control", assistant_content[-1])

    def _commit_initial(self):
        git = [
            "git",
            "-C",
            str(self.root),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
        ]
        (self.root / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(git + ["add", "README.md"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "-qm", "seed"], check=True, capture_output=True)
        return git

    @staticmethod
    def _completed_transport():
        return FakeTransport(
            [
                {
                    "id": "msg_done",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 40, "output_tokens": 5},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                }
            ]
        )

    def test_worktree_lanes_share_one_usage_ledger(self):
        git = self._commit_initial()
        worktree = self.root / ".claude" / "worktrees" / "agent-1"
        subprocess.run(
            git + ["worktree", "add", "-q", str(worktree), "-b", "lane-1"],
            check=True,
            capture_output=True,
        )
        config = self.config()
        body = {
            "model": "test-model",
            "max_tokens": 500,
            "system": [],
            "messages": [{"role": "user", "content": "review"}],
        }

        main_agent = self.agent(self._completed_transport(), run_id="main-run")
        main_agent.run(dict(body))

        lane = api_agent.ApiAgent(
            root=worktree,
            config_path=config,
            role="code-reviewer",
            ticket="PROJ-1",
            sprint="SPRINT-1",
            run_id="lane-run",
            transport=self._completed_transport(),
            review_authorization=self.phase_permit(pr="2"),
            review_pr="2",
        )
        lane.run(dict(body))

        shared = (self.root / ".orchestration" / ".llm-usage").resolve()
        self.assertEqual(lane.ledger.directory, shared)
        self.assertFalse((worktree / ".orchestration" / ".llm-usage").exists())
        # Both lanes counted against one ceiling instead of one ledger each.
        self.assertEqual(lane.ledger.summary()["input_tokens"], 80)
        self.assertEqual(lane.ledger.summary()["output_tokens"], 10)
        # Tool sandboxing still resolves to the lane's own checkout.
        self.assertEqual(lane.tool_executor.root, worktree.resolve())

    def test_usage_root_override_cannot_redirect_the_ledger(self):
        override = Path(self.temp.name) / "elsewhere"
        override.mkdir()
        with mock.patch.dict(os.environ, {"ORCHESTRATION_USAGE_ROOT": str(override)}):
            self.assertEqual(
                api_agent.shared_repository_root(self.root), self.root.resolve()
            )

    def test_conflicting_worktree_runtime_state_fails_closed(self):
        git = self._commit_initial()
        worktree = self.root / ".claude" / "worktrees" / "conflict"
        subprocess.run(
            git + ["worktree", "add", "-q", str(worktree), "-b", "conflict-lane"],
            check=True,
            capture_output=True,
        )
        self.config()
        shared = self.root / ".orchestration/.llm-usage/usage.jsonl"
        legacy = worktree / ".orchestration/.llm-usage/usage.jsonl"
        shared.parent.mkdir(parents=True, exist_ok=True)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text('{"kind":"usage","cost_usd":"1"}\n', encoding="utf-8")
        legacy.write_text('{"kind":"usage","cost_usd":"2"}\n', encoding="utf-8")
        with self.assertRaisesRegex(
            api_agent.AgentError, "conflicting legacy runtime state"
        ):
            api_agent.ApiAgent(
                root=worktree,
                config_path=self.root / ".orchestration/config.yaml",
                role="implementer",
                ticket="PROJ-1",
                sprint="S-1",
                run_id="conflict",
                transport=FakeTransport([]),
            )

    def test_shared_root_falls_back_outside_a_repository(self):
        plain = Path(self.temp.name) / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORCHESTRATION_USAGE_ROOT", None)
            with mock.patch.object(
                api_agent.subprocess, "run", side_effect=OSError("git missing")
            ):
                self.assertEqual(
                    api_agent.shared_repository_root(plain), plain.resolve()
                )

    def test_usage_events_record_role_and_request_latency(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_1",
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 30,
                        "cache_read_input_tokens": 70,
                        "output_tokens": 5,
                    },
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                }
            ]
        )
        agent = self.agent(transport)
        agent.run(
            {
                "model": "test-model",
                "max_tokens": 500,
                "system": [],
                "messages": [{"role": "user", "content": "review"}],
            }
        )
        events = agent.ledger._events()
        usage = [event for event in events if event["kind"] == "usage"]
        reservation = [event for event in events if event["kind"] == "reservation"]
        self.assertEqual(usage[0]["role"], "code-reviewer")
        self.assertEqual(reservation[0]["role"], "code-reviewer")
        self.assertIsInstance(usage[0]["latency_ms"], int)
        self.assertGreaterEqual(usage[0]["latency_ms"], 0)
        self.assertEqual(usage[0]["tool_round"], 0)

    def _report_args(self, **overrides):
        defaults = {
            "group_by": "role",
            "since": None,
            "until": None,
            "role": None,
            "model": None,
            "provider": None,
            "ticket": None,
            "sprint": None,
            "top": None,
            "format": "json",
        }
        defaults.update(overrides)
        return api_agent.argparse.Namespace(**defaults)

    def _write_ledger(self, events):
        directory = self.root / ".orchestration" / ".llm-usage"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "usage.jsonl").write_text(
            "".join(
                json.dumps(event, separators=(",", ":")) + "\n" for event in events
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _usage_event(
        role, cost, *, cache_read=900, fresh=100, latency=1000, age_hours=1, **extra
    ):
        moment = api_agent.dt.datetime.now(
            api_agent.dt.timezone.utc
        ) - api_agent.dt.timedelta(hours=age_hours)
        event = {
            "kind": "usage",
            "timestamp": moment.isoformat(),
            "reservation_id": f"resv_{role}_{age_hours}_{cost}",
            "run_id": f"run_{role}",
            "role": role,
            "ticket": "PROD-1",
            "sprint": "SPRINT-1",
            "provider": "anthropic",
            "model": "test-model",
            "response_id": f"msg_{role}_{age_hours}",
            "input_tokens": fresh,
            "cache_write_tokens": 0,
            "cache_read_tokens": cache_read,
            "output_tokens": 10,
            "reasoning_tokens": 0,
            "latency_ms": latency,
            "cost_usd": cost,
        }
        event.update(extra)
        return event

    def test_report_groups_by_role_with_cache_hit_rate(self):
        self._write_ledger(
            [
                self._usage_event("implementer", "0.500000", cache_read=900, fresh=100),
                self._usage_event(
                    "implementer", "0.250000", cache_read=900, fresh=100, age_hours=2
                ),
                self._usage_event(
                    "code-reviewer", "0.100000", cache_read=500, fresh=500, age_hours=3
                ),
            ]
        )
        report = api_agent.build_report(self.root, self._report_args())
        keys = [group["key"] for group in report["groups"]]
        self.assertEqual(keys, ["implementer", "code-reviewer"])
        implementer = report["groups"][0]
        self.assertEqual(implementer["requests"], 2)
        self.assertEqual(implementer["cache_hit_rate"], 0.9)
        self.assertEqual(report["groups"][1]["cache_hit_rate"], 0.5)
        self.assertEqual(report["totals"]["requests"], 3)
        self.assertEqual(report["totals"]["cost_usd"], "0.850000")

    def test_report_window_and_role_filter_narrow_the_ledger(self):
        self._write_ledger(
            [
                self._usage_event("implementer", "1.000000", age_hours=1),
                self._usage_event("implementer", "2.000000", age_hours=200),
                self._usage_event("code-reviewer", "4.000000", age_hours=1),
            ]
        )
        recent = api_agent.build_report(self.root, self._report_args(since="24h"))
        self.assertEqual(recent["totals"]["requests"], 2)
        self.assertEqual(recent["totals"]["cost_usd"], "5.000000")
        scoped = api_agent.build_report(
            self.root, self._report_args(role="code-reviewer")
        )
        self.assertEqual(scoped["totals"]["cost_usd"], "4.000000")
        self.assertEqual(scoped["filters"], {"role": "code-reviewer"})

    def test_report_tolerates_ledger_entries_without_performance_fields(self):
        legacy = self._usage_event("implementer", "0.100000")
        del legacy["latency_ms"]
        del legacy["role"]
        self._write_ledger([legacy])
        report = api_agent.build_report(self.root, self._report_args())
        self.assertEqual(report["groups"][0]["key"], "unknown")
        self.assertIsNone(report["totals"]["latency_p50_ms"])
        self.assertEqual(report["totals"]["latency_samples"], 0)
        self.assertIn(
            "latency recorded for 0 of 1 requests", api_agent.format_report(report)
        )

    def test_report_top_reports_what_it_hid(self):
        self._write_ledger(
            [
                self._usage_event(
                    f"role-{index}", f"{index}.000000", age_hours=index + 1
                )
                for index in range(1, 5)
            ]
        )
        report = api_agent.build_report(self.root, self._report_args(top=2))
        self.assertEqual(len(report["groups"]), 2)
        self.assertEqual(report["groups_hidden_by_top"], 2)
        self.assertEqual(report["totals"]["requests"], 4)
        self.assertIn("2 further groups hidden", api_agent.format_report(report))

    def test_parse_window_accepts_relative_and_absolute_forms(self):
        now = api_agent.dt.datetime.now(api_agent.dt.timezone.utc)
        self.assertLess(
            abs((now - api_agent.parse_window("30m")).total_seconds() - 1800), 5
        )
        self.assertLess(
            abs((now - api_agent.parse_window("2w")).total_seconds() - 1209600), 5
        )
        absolute = api_agent.parse_window("2026-08-01T00:00:00+00:00")
        self.assertEqual(absolute.year, 2026)
        # A naive timestamp is read as UTC rather than silently taking local time.
        self.assertEqual(
            api_agent.parse_window("2026-08-01").tzinfo, api_agent.dt.timezone.utc
        )
        with self.assertRaises(api_agent.AgentError):
            api_agent.parse_window("last tuesday")

    def test_openai_tool_loop_uses_previous_response_id(self):
        transport = FakeTransport(
            [
                {
                    "id": "resp_1",
                    "status": "completed",
                    "usage": {"input_tokens": 100, "output_tokens": 8},
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "git_status",
                            "arguments": "{}",
                        }
                    ],
                },
                {
                    "id": "resp_2",
                    "status": "completed",
                    "usage": {
                        "input_tokens": 90,
                        "input_tokens_details": {
                            "cached_tokens": 60,
                            "cache_write_tokens": 10,
                        },
                        "output_tokens": 4,
                    },
                    "output_text": "done",
                    "output": [],
                },
            ]
        )
        config = self.config(
            provider="openai",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-2",
            sprint="SPRINT-1",
            run_id="openai-run",
            transport=transport,
            review_authorization=self.phase_permit(ticket="PROJ-5"),
            review_pr="1",
            **self.attempt_capability(run_id="openai-run", ticket="PROJ-2"),
        )
        result = agent.run(
            {
                "model": "test-model",
                "max_output_tokens": 100,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "work",
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                ],
                "text": {"verbosity": "low"},
                "prompt_cache_key": "stale-key",
                "prompt_cache_options": {"mode": "explicit"},
                "prompt_cache_retention": "24h",
            }
        )
        response_calls = [call for call in transport.calls if call[1] == "responses"]
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(response_calls[1][2]["previous_response_id"], "resp_1")
        self.assertEqual(
            response_calls[1][2]["input"][0]["type"], "function_call_output"
        )
        self.assertEqual(response_calls[1][2]["text"], {"verbosity": "low"})
        self.assertIn(
            "apply_patch", {tool["name"] for tool in response_calls[0][2]["tools"]}
        )
        serialized_calls = json.dumps(response_calls)
        for field in api_agent.OPENAI_CACHE_REQUEST_FIELDS:
            self.assertNotIn(field, serialized_calls)

    def test_azure_adm_chat_completion_tool_loop(self):
        transport = FakeTransport(
            [
                {
                    "id": "chat_1",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "git_status",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 8},
                },
                {
                    "id": "chat_2",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "done"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 70,
                        "prompt_tokens_details": {"cached_tokens": 40},
                        "completion_tokens": 4,
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
            ]
        )
        config = self.config(
            provider="azure_adm",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-3",
            sprint="SPRINT-1",
            run_id="azure-adm-run",
            transport=transport,
            **self.attempt_capability(run_id="azure-adm-run", ticket="PROJ-3"),
        )
        result = agent.run(
            {
                "model": "test-model",
                "max_completion_tokens": 100,
                "messages": [{"role": "user", "content": "work"}],
            }
        )
        chat_calls = [call for call in transport.calls if call[1] == "chat/completions"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(len(chat_calls), 2)
        self.assertFalse(
            any(call[1].endswith("input_tokens") for call in transport.calls)
        )
        self.assertIn(
            "apply_patch",
            {tool["function"]["name"] for tool in chat_calls[0][2]["tools"]},
        )
        self.assertEqual(chat_calls[1][2]["messages"][-1]["role"], "tool")
        self.assertEqual(chat_calls[1][2]["messages"][-1]["tool_call_id"], "call_1")
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 110)
        self.assertEqual(summary["cache_read_tokens"], 40)
        self.assertEqual(summary["output_tokens"], 12)

    def test_bedrock_mantle_chat_completion_tool_loop(self):
        transport = FakeTransport(
            [
                {
                    "id": "chat_1",
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "git_status",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 8},
                },
                {
                    "id": "chat_2",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "done"},
                        }
                    ],
                    "usage": {"prompt_tokens": 70, "completion_tokens": 4},
                },
            ]
        )
        config = self.config(
            provider="bedrock_mantle",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-4",
            sprint="SPRINT-1",
            run_id="bedrock-mantle-run",
            transport=transport,
            **self.attempt_capability(run_id="bedrock-mantle-run", ticket="PROJ-4"),
        )
        result = agent.run(
            {
                "model": "test-model",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "work"}],
            }
        )
        chat_calls = [call for call in transport.calls if call[1] == "chat/completions"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(len(chat_calls), 2)
        self.assertEqual(chat_calls[0][2]["max_tokens"], 100)
        self.assertIn(
            "apply_patch",
            {tool["function"]["name"] for tool in chat_calls[0][2]["tools"]},
        )
        self.assertEqual(chat_calls[1][2]["messages"][-1]["role"], "tool")
        self.assertEqual(chat_calls[1][2]["messages"][-1]["tool_call_id"], "call_1")
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 150)
        self.assertEqual(summary["output_tokens"], 12)

    def test_bedrock_mantle_review_accepts_only_complete_think_wrapper(self):
        wrapped = "<think>private reasoning trace</think>\n" + CLEAN_REVIEW
        agent = self.agent(
            FakeTransport(
                [
                    {
                        "id": "chat_1",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": wrapped},
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                    }
                ]
            ),
            provider="bedrock_mantle",
            run_id="mantle-review-wrapper",
        )
        result = agent.run(
            {
                "model": "test-model",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "review"}],
            }
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], CLEAN_REVIEW)
        self.assertEqual(result["review"]["verdict"], "PASS")
        contaminated = "commentary before " + CLEAN_REVIEW
        self.assertEqual(
            api_agent.review_text("bedrock_mantle", contaminated),
            contaminated,
        )

    def test_bedrock_converse_tool_loop_and_usage(self):
        transport = FakeTransport(
            [
                {
                    "id": "aws-request-1",
                    "stopReason": "tool_use",
                    "usage": {
                        "inputTokens": 40,
                        "cacheReadInputTokens": 60,
                        "cacheWriteInputTokens": 10,
                        "outputTokens": 8,
                    },
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "toolUse": {
                                        "toolUseId": "tool-1",
                                        "name": "git_status",
                                        "input": {},
                                    }
                                }
                            ],
                        }
                    },
                },
                {
                    "id": "aws-request-2",
                    "stopReason": "end_turn",
                    "usage": {
                        "inputTokens": 30,
                        "cacheReadInputTokens": 70,
                        "outputTokens": 4,
                    },
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": "done"}],
                        }
                    },
                },
            ]
        )
        config = self.config(
            provider="bedrock",
            extra="    implementer:\n      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]",
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=config,
            role="implementer",
            ticket="PROJ-4",
            sprint="SPRINT-1",
            run_id="bedrock-run",
            transport=transport,
            **self.attempt_capability(run_id="bedrock-run", ticket="PROJ-4"),
        )
        result = agent.run(
            {
                "modelId": "test-model",
                "inferenceConfig": {"maxTokens": 100},
                "system": [{"text": "system"}],
                "messages": [{"role": "user", "content": [{"text": "work"}]}],
            }
        )
        converse_calls = [call for call in transport.calls if call[1] == "converse"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "done")
        self.assertEqual(len(converse_calls), 2)
        self.assertFalse(any(call[1] == "count_tokens" for call in transport.calls))
        self.assertIn(
            "apply_patch",
            {
                tool["toolSpec"]["name"]
                for tool in converse_calls[0][2]["toolConfig"]["tools"]
                if "toolSpec" in tool
            },
        )
        tool_result = converse_calls[1][2]["messages"][-1]["content"][0]["toolResult"]
        self.assertEqual(tool_result["toolUseId"], "tool-1")
        self.assertEqual(tool_result["status"], "success")
        summary = agent.ledger.summary()
        self.assertEqual(summary["input_tokens"], 70)
        self.assertEqual(summary["cache_read_tokens"], 130)
        self.assertEqual(summary["cache_write_tokens"], 10)
        self.assertEqual(summary["output_tokens"], 12)

    def test_bedrock_cache_breakpoint_rolls_without_mutating_response(self):
        original = [
            {"role": "assistant", "content": [{"text": "before"}]},
            {"role": "user", "content": [{"text": "after"}]},
        ]
        rolled = api_agent.roll_bedrock_cache_breakpoint(original)
        self.assertEqual(original[-1]["content"], [{"text": "after"}])
        self.assertEqual(
            rolled[-1]["content"][-1],
            {"cachePoint": {"type": "default", "ttl": "1h"}},
        )

    def test_bedrock_openai_uses_local_count_and_no_claude_cache_points(self):
        model = "global.openai.gpt-5.6-sol"
        transport = FakeTransport(
            [
                {
                    "id": "aws-openai-1",
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 20, "outputTokens": 4},
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [{"text": CLEAN_REVIEW}],
                        }
                    },
                }
            ]
        )
        agent = api_agent.ApiAgent(
            root=self.root,
            config_path=self.config(provider="bedrock", model=model),
            role="code-reviewer",
            ticket="PROJ-5",
            sprint=None,
            run_id="bedrock-openai-run",
            transport=transport,
            review_authorization=self.phase_permit(ticket="PROJ-5"),
            review_pr="1",
        )
        result = agent.run(
            {
                "modelId": model,
                "inferenceConfig": {"maxTokens": 100},
                "system": [{"text": "system"}],
                "messages": [{"role": "user", "content": [{"text": "review"}]}],
                "additionalModelRequestFields": {"reasoning_effort": "xhigh"},
            }
        )
        converse = next(call for call in transport.calls if call[1] == "converse")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(any(call[1] == "count_tokens" for call in transport.calls))
        self.assertTrue(
            all("cachePoint" not in block for block in converse[2]["system"])
        )
        self.assertTrue(
            all("cachePoint" not in tool for tool in converse[2]["toolConfig"]["tools"])
        )

    def test_budget_blocks_before_provider_submission(self):
        transport = FakeTransport([], count=2_000_000)
        agent = self.agent(transport, run_id="budget-run")
        with self.assertRaises(api_agent.BudgetError):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertFalse(any(call[1] == "messages" for call in transport.calls))
        self.assertEqual(agent.state["status"], "budget_blocked")

    def test_unique_model_and_reviewer_run_breakers_count_runs_not_tool_rounds(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(api_agent.DEFAULT_BUDGETS)
        limits["max_model_runs_per_ticket"] = 2
        limits["max_reviewer_runs_per_ticket"] = 1
        ledger.reserve(
            projected=api_agent.Decimal("0.01"),
            limits=limits,
            run_id="review-1",
            ticket="PROJ-1",
            sprint="S-1",
            provider="openai",
            model="m",
            role="code-reviewer",
        )
        # A second provider/tool round inside one run does not consume another run slot.
        ledger.reserve(
            projected=api_agent.Decimal("0.01"),
            limits=limits,
            run_id="review-1",
            ticket="PROJ-1",
            sprint="S-1",
            provider="openai",
            model="m",
            role="code-reviewer",
        )
        with self.assertRaisesRegex(
            api_agent.BudgetError, "max_reviewer_runs_per_ticket"
        ):
            ledger.reserve(
                projected=api_agent.Decimal("0.01"),
                limits=limits,
                run_id="review-2",
                ticket="PROJ-1",
                sprint="S-1",
                provider="openai",
                model="m",
                role="security-reviewer",
            )
        worker_limits = dict(limits)
        worker_limits["max_model_runs_per_ticket"] = 1
        ledger.reserve(
            projected=api_agent.Decimal("0.01"),
            limits=worker_limits,
            run_id="implement-1",
            ticket="PROJ-2",
            sprint="S-1",
            provider="anthropic",
            model="m",
            role="implementer",
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "max_model_runs_per_ticket"):
            ledger.reserve(
                projected=api_agent.Decimal("0.01"),
                limits=worker_limits,
                run_id="implement-2",
                ticket="PROJ-2",
                sprint="S-1",
                provider="anthropic",
                model="m",
                role="implementer",
            )

    def test_design_rounds_do_not_exhaust_post_implementation_review_capacity(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(api_agent.DEFAULT_BUDGETS)
        limits["max_reviewer_runs_per_ticket"] = 1
        for index in range(6):
            ledger.reserve(
                projected=api_agent.Decimal("0.01"),
                limits=limits,
                run_id=f"design-{index}",
                ticket="PROJ-3",
                sprint="S-1",
                provider="openai",
                model="m",
                role="design-reviewer",
            )
        # Simulate the stale pause emitted by the old shared reviewer counter.
        ledger._append_locked(
            {
                "kind": "ticket_budget_pause",
                "timestamp": api_agent.utc_now(),
                "ticket": "PROJ-3",
                "run_id": "old-code-review",
                "reason": "max_reviewer_runs_per_ticket",
            }
        )
        ledger.reserve(
            projected=api_agent.Decimal("0.01"),
            limits=limits,
            run_id="code-1",
            ticket="PROJ-3",
            sprint="S-1",
            provider="openai",
            model="m",
            role="code-reviewer",
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "post-implementation"):
            ledger.reserve(
                projected=api_agent.Decimal("0.01"),
                limits=limits,
                run_id="security-2",
                ticket="PROJ-3",
                sprint="S-1",
                provider="openai",
                model="m",
                role="security-reviewer",
            )

    def test_ticket_pause_is_durable_and_has_no_self_approval_bypass(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(api_agent.DEFAULT_BUDGETS)
        limits["warn_usd_per_ticket"] = api_agent.Decimal("0.05")
        limits["pause_usd_per_ticket"] = api_agent.Decimal("0.10")
        with self.assertRaisesRegex(api_agent.BudgetError, "operator policy change"):
            ledger.reserve(
                projected=api_agent.Decimal("0.11"),
                limits=limits,
                run_id="costly",
                ticket="PROJ-9",
                sprint="S-1",
                provider="openai",
                model="m",
                role="implementer",
            )
        events = ledger._events()
        self.assertTrue(
            any(event.get("kind") == "ticket_budget_pause" for event in events)
        )
        ledger._append_locked(
            {
                "kind": "ticket_budget_pause",
                "timestamp": api_agent.utc_now(),
                "ticket": "PROJ-9",
                "run_id": "later-counter-stop",
                "reason": "max_reviewer_runs_per_ticket",
            }
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "ticket_budget_pause"):
            ledger.reserve(
                projected=api_agent.Decimal("0.01"),
                limits=limits,
                run_id="still-paused",
                ticket="PROJ-9",
                sprint="S-1",
                provider="openai",
                model="m",
                role="implementer",
            )
        self.assertFalse(hasattr(ledger, "approve_ticket_budget"))

    def test_external_budget_authority_extends_only_the_ticket_cost_ceiling(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(api_agent.DEFAULT_BUDGETS)
        limits["max_usd_per_ticket"] = api_agent.Decimal("0.15")
        limits["pause_usd_per_ticket"] = api_agent.Decimal("0.10")
        with self.assertRaises(api_agent.BudgetError):
            ledger.reserve(
                projected=api_agent.Decimal("0.11"),
                limits=limits,
                run_id="before-grant",
                ticket="PROJ-9",
                sprint="S-1",
                provider="openai",
                model="m",
                role="implementer",
            )
        with mock.patch.object(
            api_agent,
            "authorized_budget_ceiling",
            return_value=api_agent.Decimal("0.20"),
        ):
            ledger.reserve(
                projected=api_agent.Decimal("0.05"),
                limits=limits,
                run_id="after-grant",
                ticket="PROJ-9",
                sprint="S-1",
                provider="openai",
                model="m",
                role="implementer",
            )
            with self.assertRaisesRegex(api_agent.BudgetError, "max_usd_per_ticket"):
                ledger.reserve(
                    projected=api_agent.Decimal("0.16"),
                    limits=limits,
                    run_id="over-grant",
                    ticket="PROJ-9",
                    sprint="S-1",
                    provider="openai",
                    model="m",
                    role="implementer",
                )

    def test_incident_breakers_are_active_and_config_can_only_tighten(self):
        legacy = api_agent.budgets_from_config({"llm": {"budgets": {}}})
        self.assertEqual(legacy["max_model_runs_per_ticket"], 12)
        self.assertEqual(legacy["max_reviewer_runs_per_ticket"], 6)
        self.assertEqual(legacy["pause_usd_per_ticket"], api_agent.Decimal("20"))
        self.assertEqual(legacy["provider_read_timeout_seconds"], 900)
        raised = api_agent.budgets_from_config(
            {
                "llm": {
                    "budgets": {
                        "max_usd_per_run": 999,
                        "max_usd_per_ticket": 999,
                        "max_usd_per_sprint": 9999,
                        "pause_usd_per_ticket": 998,
                        "warn_usd_per_ticket": 10,
                        "max_model_runs_per_ticket": 999,
                        "max_reviewer_runs_per_ticket": 999,
                    }
                }
            }
        )
        self.assertEqual(raised["max_usd_per_ticket"], api_agent.Decimal("30"))
        self.assertEqual(raised["pause_usd_per_ticket"], api_agent.Decimal("20"))
        self.assertEqual(raised["max_model_runs_per_ticket"], 12)
        self.assertEqual(raised["max_reviewer_runs_per_ticket"], 6)

    def test_direct_api_agent_uses_configured_provider_read_timeout(self):
        permit = self.phase_permit()
        with mock.patch.object(api_agent, "HttpTransport") as transport:
            api_agent.ApiAgent(
                root=self.root,
                config_path=self.config(),
                role="code-reviewer",
                ticket="PROJ-1",
                sprint="SPRINT-1",
                run_id="configured-provider-timeout",
                transport=None,
                review_authorization=permit,
                review_pr="1",
            )
        transport.assert_called_once_with(timeout=777)

    def test_free_form_accounting_scopes_fail_closed(self):
        with self.assertRaisesRegex(api_agent.AgentError, "canonical Jira key"):
            api_agent.normalize_ticket_scope("PROJ-1/../2")
        with self.assertRaisesRegex(
            api_agent.AgentError, "sprint must be a canonical id"
        ):
            api_agent.normalize_sprint_scope("Sprint 1")

    def test_alternate_config_path_is_rejected(self):
        self.config()
        alternate = self.root / "alternate.yaml"
        alternate.write_text("llm: {}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            api_agent.AgentError, "alternate orchestration config"
        ):
            api_agent.ApiAgent(
                root=self.root,
                config_path=alternate,
                role="implementer",
                ticket="PROJ-1",
                sprint="S-1",
                run_id="alternate",
                transport=FakeTransport([]),
            )

    def test_implementer_requires_exact_controller_attempt_capability(self):
        config = self.config(extra="    implementer:\n      allowed_tools: [read_file]")
        with self.assertRaisesRegex(
            api_agent.AgentError, "controller-issued attempt capability"
        ):
            api_agent.ApiAgent(
                root=self.root,
                config_path=config,
                role="implementer",
                ticket="PROJ-1",
                sprint="SPRINT-1",
                run_id="uncap",
                transport=FakeTransport([]),
            )
        binding = self.attempt_capability(run_id="bound", ticket="PROJ-1")
        with self.assertRaisesRegex(api_agent.AgentError, "does not match"):
            api_agent.ApiAgent(
                root=self.root,
                config_path=config,
                role="implementer",
                ticket="PROJ-1",
                sprint="SPRINT-1",
                run_id="different",
                transport=FakeTransport([]),
                **binding,
            )

    def test_implementer_revalidates_attempt_before_each_provider_request(self):
        transport = FakeTransport([])
        agent = self.agent(
            transport, role="implementer", run_id="superseded-worker"
        )
        checkpoint = next(
            (self.root / ".orchestration/.sprint-state").glob("*.json")
        )
        state = json.loads(checkpoint.read_text())
        state["tickets"]["PROJ-1"]["state"] = "pending"
        checkpoint.write_text(json.dumps(state))
        with self.assertRaisesRegex(api_agent.AgentError, "stale|active lane"):
            agent._submit({"messages": [], "max_tokens": 10})
        self.assertEqual(transport.calls, [])

    def test_review_phase_permit_is_single_use_and_bound_to_head(self):
        token = self.phase_permit()
        head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with self.assertRaisesRegex(api_agent.ReviewPermitError, "does not match"):
            api_agent.consume_review_permit(
                shared_root=self.root,
                ledger_dir=".orchestration/.review-ledger",
                pr="1",
                token=token,
                role="security-reviewer",
                head=head,
                timestamp="now",
            )
        api_agent.consume_review_permit(
            shared_root=self.root,
            ledger_dir=".orchestration/.review-ledger",
            pr="1",
            token=token,
            role="code-reviewer",
            head=head,
            timestamp="now",
        )
        with self.assertRaisesRegex(api_agent.ReviewPermitError, "already started"):
            api_agent.consume_review_permit(
                shared_root=self.root,
                ledger_dir=".orchestration/.review-ledger",
                pr="1",
                token=token,
                role="code-reviewer",
                head=head,
                timestamp="later",
            )

    def test_explicit_rate_limit_retries_with_same_reservation(self):
        transport = FakeTransport(
            [
                api_agent.ProviderHTTPError(429, "rate limited"),
                {
                    "id": "msg_after_retry",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 2},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport, run_id="retry-run")
        with mock.patch.object(api_agent.time, "sleep") as sleep:
            result = agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        message_calls = [call for call in transport.calls if call[1] == "messages"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(message_calls), 2)
        self.assertEqual(message_calls[0][3], message_calls[1][3])
        self.assertEqual(agent.state["retry_count"], 1)
        sleep.assert_called_once_with(0)

    def test_rate_limit_honors_provider_retry_after(self):
        transport = FakeTransport(
            [
                api_agent.ProviderHTTPError(
                    429, "rate limited", retry_after_seconds=17.5
                ),
                {
                    "id": "msg_after_retry_after",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 2},
                    "content": [{"type": "text", "text": CLEAN_REVIEW}],
                },
            ]
        )
        agent = self.agent(transport, run_id="retry-after-run")
        with mock.patch.object(api_agent.time, "sleep") as sleep:
            result = agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(result["status"], "completed")
        sleep.assert_called_once_with(17.5)
        self.assertEqual(agent.state["total_rate_limit_wait_seconds"], 17.5)

    def test_reviewer_output_fails_closed_when_not_structured(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_invalid",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 2},
                    "content": [{"type": "text", "text": "VERDICT: PASS"}],
                }
            ]
        )
        agent = self.agent(transport, run_id="invalid-review")
        with self.assertRaisesRegex(api_agent.AgentError, "invalid structured output"):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(agent.state["status"], "invalid_output")
        failed_cost = api_agent.Decimal(agent.state["cost_usd"])
        self.assertGreater(failed_cost, 0)
        retry = self.agent(self._completed_transport(), run_id="valid-review-retry")
        result = retry.run(
            {
                "model": "test-model",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "review"}],
            }
        )
        self.assertEqual(result["status"], "completed")
        self.assertGreater(api_agent.Decimal(result["usage"]["cost_usd"]), failed_cost)

    def test_incomplete_review_releases_permit_but_not_spend(self):
        transport = self._completed_transport()
        transport.responses[0]["stop_reason"] = "max_tokens"
        agent = self.agent(transport, run_id="truncated-review")
        self.assertEqual(
            agent.run({"model": "test-model", "max_tokens": 100})["status"],
            "incomplete",
        )
        self.assertTrue(self.phase_permit())
        self.assertGreater(api_agent.Decimal(agent.ledger.summary()["cost_usd"]), 0)

    def test_tool_exhaustion_releases_review_permit(self):
        tool_use = {
            "id": "msg_tools",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "git_status",
                    "input": {},
                }
            ],
        }
        # The forced final verdict turn still asked for tools: no second final
        # turn and no verdict, so the run stays blocked and the permit returns.
        transport = FakeTransport([tool_use, dict(tool_use, id="msg_final_tools")])
        agent = self.agent(transport)
        agent.state["tool_rounds"] = agent.budgets["max_tool_rounds"]
        with self.assertRaisesRegex(api_agent.BudgetError, "final verdict turn"):
            agent.run({"model": "test-model", "max_tokens": 100})
        self.assertEqual(
            len([call for call in transport.calls if call[1] == "messages"]), 2
        )
        self.assertEqual(agent.state["status"], "budget_blocked")
        self.assertEqual(agent.state["final_turn"], "max_tool_rounds")
        self.assertTrue(self.phase_permit())

    def test_final_verdict_turn_counter_failure_releases_review_permit(self):
        transport = FakeTransport([self._anthropic_tool_turn(0, 10, 2)])
        agent = self.agent(transport, run_id="final-counter-failure")
        agent.state["tool_rounds"] = agent.budgets["max_tool_rounds"]
        with mock.patch.object(
            agent,
            "_count",
            side_effect=[10, api_agent.AgentError("counter unavailable")],
        ):
            with self.assertRaisesRegex(api_agent.AgentError, "counter unavailable"):
                agent.run({"model": "test-model", "max_tokens": 100})
        self.assertTrue(self.phase_permit())

    @staticmethod
    def _anthropic_tool_turn(index, input_tokens=20000, output_tokens=400):
        return {
            "id": f"msg_tool_{index}",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tool_{index}",
                    "name": "git_status",
                    "input": {},
                }
            ],
        }

    def _review_permit_record(self):
        ledgers = list((self.root / ".orchestration/.review-ledger").glob("*.json"))
        self.assertEqual(len(ledgers), 1)
        permits = json.loads(ledgers[0].read_text(encoding="utf-8"))["review_permits"]
        return permits[-1]

    def test_review_budget_incident_forces_bounded_final_verdict(self):
        # Nineteen $0.23 tool rounds fit a $5 review once each turn reserves a
        # realistic output bound; the refused twentieth becomes the verdict turn.
        # A forced FAIL keeps its blocking findings authoritative.
        responses = [self._anthropic_tool_turn(index) for index in range(19)]
        responses.append(
            {
                "id": "msg_verdict",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20000, "output_tokens": 300},
                "content": [{"type": "text", "text": BLOCKING_REVIEW}],
            }
        )
        transport = FakeTransport(responses, count=20000)
        agent = self.agent(
            transport,
            run_id="incident-review",
            budgets=incident_budgets(),
            pricing=INCIDENT_PRICING,
        )
        result = agent.run(
            {
                "model": "test-model",
                "max_tokens": 32768,
                "system": [],
                "messages": [{"role": "user", "content": "review"}],
            }
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_turn"], "budget")
        self.assertEqual(result["review"]["verdict"], "FAIL")
        self.assertEqual(agent.state["final_turn"], "budget")
        self.assertIn(
            "max_usd_per_code_review_phase", agent.state["final_turn_reason"]
        )
        message_calls = [call[2] for call in transport.calls if call[1] == "messages"]
        self.assertEqual(len(message_calls), 20)
        # Every ordinary review turn is bounded by the review output cap.
        self.assertEqual({call["max_tokens"] for call in message_calls[:-1]}, {8192})
        final = message_calls[-1]
        self.assertEqual(final["max_tokens"], 4096)
        self.assertEqual(final["tool_choice"], {"type": "none"})
        final_blocks = final["messages"][-1]["content"]
        self.assertEqual(final_blocks[0]["type"], "tool_result")
        self.assertIn("final structured review JSON", final_blocks[-1]["text"])
        # Reservations are the true worst case of what was submitted.
        reservations = [
            api_agent.Decimal(event["projected_cost_usd"])
            for event in agent.ledger._events()
            if event["kind"] == "reservation"
        ]
        self.assertEqual(reservations[0], agent.pricing.worst_case(20000, 8192))
        self.assertEqual(reservations[-1], agent.pricing.worst_case(20000, 4096))
        phase = agent.ledger.phase_totals(agent.ledger._events(), "PROJ-1")
        self.assertLessEqual(
            phase["code_review"]["spent_usd"], api_agent.Decimal("5")
        )
        self.assertEqual(phase["code_review"]["reserved_usd"], 0)
        # The first request tells the reviewer what it can afford.
        brief = message_calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn("code_review phase ceiling $5.00", brief)
        self.assertIn("already spent or reserved $0.00", brief)
        self.assertIn("remaining $5.00", brief)
        self.assertIn("$0.6144", brief)
        self.assertIn("final verdict turn", brief)
        permit = self._review_permit_record()
        self.assertTrue(permit.get("completion_receipt"))
        self.assertFalse(permit.get("cancelled_at"))

    def test_forced_final_turn_pass_is_not_authoritative(self):
        # A PASS written only because budget ran out may rest on partial
        # evidence, so it must not clear a gate: no receipt, permit released.
        responses = [self._anthropic_tool_turn(index) for index in range(19)]
        responses.append(
            {
                "id": "msg_verdict",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 20000, "output_tokens": 300},
                "content": [{"type": "text", "text": CLEAN_REVIEW}],
            }
        )
        transport = FakeTransport(responses, count=20000)
        agent = self.agent(
            transport,
            run_id="incident-forced-pass",
            budgets=incident_budgets(),
            pricing=INCIDENT_PRICING,
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "not authoritative"):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 32768,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(agent.state["status"], "budget_blocked")
        self.assertEqual(agent.state["final_turn"], "budget")
        self.assertEqual(agent.state["review"]["verdict"], "PASS")
        permit = self._review_permit_record()
        self.assertFalse(permit.get("completion_receipt"))
        self.assertTrue(permit.get("cancelled_at"))

    def test_full_output_allowance_refuses_review_at_half_budget(self):
        # Reserving the whole 32768-token allowance reproduces the incident:
        # the ordinary turn is refused after about $2.50 of real work. The
        # forced verdict turn still fits, but here it asks for tools again.
        responses = [self._anthropic_tool_turn(index) for index in range(40)]
        transport = FakeTransport(responses, count=20000)
        agent = self.agent(
            transport,
            run_id="incident-refusal",
            budgets=incident_budgets(max_output_tokens_per_review_turn=32768),
            pricing=INCIDENT_PRICING,
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "final verdict turn"):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 32768,
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        message_calls = [call[2] for call in transport.calls if call[1] == "messages"]
        self.assertEqual(len(message_calls), 12)
        self.assertEqual(message_calls[0]["max_tokens"], 32768)
        self.assertEqual(agent.state["final_turn"], "budget")
        self.assertIn(
            "max_usd_per_code_review_phase", agent.state["final_turn_reason"]
        )
        self.assertEqual(message_calls[-1]["max_tokens"], 4096)
        self.assertEqual(message_calls[-1]["tool_choice"], {"type": "none"})
        self.assertLess(api_agent.Decimal(agent.state["cost_usd"]), api_agent.Decimal("3"))
        self.assertTrue(self.phase_permit())

    def test_final_verdict_turn_is_refused_when_it_cannot_fit(self):
        transport = FakeTransport(
            [self._anthropic_tool_turn(0, output_tokens=5334)], count=20000
        )
        agent = self.agent(
            transport,
            run_id="final-refused",
            budgets=incident_budgets(max_usd_per_code_review_phase="1.00"),
            pricing=INCIDENT_PRICING,
        )
        with self.assertRaisesRegex(
            api_agent.BudgetError, "max_usd_per_code_review_phase"
        ):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 32768,
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(
            len([call for call in transport.calls if call[1] == "messages"]), 1
        )
        self.assertEqual(agent.state["status"], "budget_blocked")
        self.assertEqual(agent.state["final_turn"], "budget")
        self.assertEqual(agent.ledger.summary()["open_reservations"], [])
        self.assertTrue(self.phase_permit())

    def test_reviewer_output_cap_bounds_submitted_request_and_reservation(self):
        transport = self._completed_transport()
        agent = self.agent(
            transport,
            run_id="review-cap",
            budgets=incident_budgets(max_output_tokens_per_review_turn=6000),
            pricing=INCIDENT_PRICING,
        )
        agent.run({"model": "test-model", "max_tokens": 32768})
        submitted = [call[2] for call in transport.calls if call[1] == "messages"][0]
        self.assertEqual(submitted["max_tokens"], 6000)
        reservation = next(
            event
            for event in agent.ledger._events()
            if event["kind"] == "reservation"
        )
        self.assertEqual(
            api_agent.Decimal(reservation["projected_cost_usd"]),
            agent.pricing.worst_case(100, 6000),
        )
        defaults = api_agent.budgets_from_config({})
        self.assertEqual(defaults["max_output_tokens_per_review_turn"], 4096)
        self.assertEqual(defaults["final_verdict_output_tokens"], 4096)
        raised = api_agent.budgets_from_config(
            {"llm": {"budgets": {"max_output_tokens_per_turn": 32768}}}
        )
        self.assertEqual(raised["max_output_tokens_per_review_turn"], 8192)
        self.assertEqual(raised["final_verdict_output_tokens"], 4096)
        bounded = api_agent.budgets_from_config(
            {
                "llm": {
                    "budgets": {
                        "max_output_tokens_per_turn": 2000,
                        "max_output_tokens_per_review_turn": 50000,
                        "final_verdict_output_tokens": 9000,
                    }
                }
            }
        )
        self.assertEqual(bounded["max_output_tokens_per_review_turn"], 2000)
        self.assertEqual(bounded["final_verdict_output_tokens"], 2000)
        for key in (
            "max_output_tokens_per_review_turn",
            "final_verdict_output_tokens",
        ):
            with self.assertRaises(api_agent.AgentError):
                api_agent.budgets_from_config({"llm": {"budgets": {key: 0}}})

    def test_final_verdict_turn_disables_tools_for_each_provider(self):
        def chat_tool_turn(index):
            return {
                "id": f"chat_{index}",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call_{index}",
                                    "type": "function",
                                    "function": {
                                        "name": "git_status",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }

        tool_turns = {
            "anthropic": lambda index: self._anthropic_tool_turn(index, 10, 2),
            "openai": lambda index: {
                "id": f"resp_{index}",
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{index}",
                        "call_id": f"call_{index}",
                        "name": "git_status",
                        "arguments": "{}",
                    }
                ],
            },
            "azure_adm": chat_tool_turn,
            "bedrock_mantle": chat_tool_turn,
            "bedrock": lambda index: {
                "id": f"aws-{index}",
                "stopReason": "tool_use",
                "usage": {"inputTokens": 10, "outputTokens": 2},
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": f"tool-{index}",
                                    "name": "git_status",
                                    "input": {},
                                }
                            }
                        ],
                    }
                },
            },
        }
        chat_verdict = {
            "id": "chat_verdict",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": BLOCKING_REVIEW},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }
        verdicts = {
            "anthropic": {
                "id": "msg_verdict",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "content": [{"type": "text", "text": BLOCKING_REVIEW}],
            },
            "openai": {
                "id": "resp_verdict",
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "output_text": BLOCKING_REVIEW,
                "output": [],
            },
            "azure_adm": chat_verdict,
            "bedrock_mantle": chat_verdict,
            "bedrock": {
                "id": "aws-verdict",
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 2},
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": BLOCKING_REVIEW}],
                    }
                },
            },
        }
        requests = {
            "anthropic": {
                "model": "test-model",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "review"}],
            },
            "openai": {
                "model": "test-model",
                "max_output_tokens": 100,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "review"}],
                    }
                ],
            },
            "azure_adm": {
                "model": "test-model",
                "max_completion_tokens": 100,
                "messages": [{"role": "user", "content": "review"}],
            },
            "bedrock_mantle": {
                "model": "test-model",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "review"}],
            },
            "bedrock": {
                "modelId": "test-model",
                "inferenceConfig": {"maxTokens": 100},
                "messages": [{"role": "user", "content": [{"text": "review"}]}],
            },
        }
        endpoints = {
            "anthropic": "messages",
            "openai": "responses",
            "azure_adm": "chat/completions",
            "bedrock_mantle": "chat/completions",
            "bedrock": "converse",
        }
        budgets = DEFAULT_TEST_BUDGETS.replace(
            "max_tool_rounds: 3", "max_tool_rounds: 1"
        ).replace(
            "max_output_tokens_per_turn: 100",
            "max_output_tokens_per_turn: 100\n    final_verdict_output_tokens: 60",
        )
        for provider, endpoint in endpoints.items():
            # Each provider gets a fresh review ledger and usage ledger.
            for directory in (".orchestration/.review-ledger", ".orchestration/.llm-usage"):
                if (self.root / directory).is_dir():
                    shutil.rmtree(self.root / directory)
            with self.subTest(provider=provider):
                transport = FakeTransport(
                    [
                        tool_turns[provider](1),
                        tool_turns[provider](2),
                        verdicts[provider],
                    ]
                )
                agent = self.agent(
                    transport,
                    provider=provider,
                    run_id=f"final-{provider}",
                    budgets=budgets,
                )
                result = agent.run(json.loads(json.dumps(requests[provider])))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["final_turn"], "max_tool_rounds")
                self.assertEqual(agent.state["tool_rounds"], 1)
                calls = [call[2] for call in transport.calls if call[1] == endpoint]
                self.assertEqual(len(calls), 3)
                final = json.dumps(calls[-1])
                self.assertIn("final structured review JSON", final)
                self.assertIn("not executed", final)
                if provider == "bedrock":
                    self.assertEqual(calls[-1]["inferenceConfig"]["maxTokens"], 60)
                    # Converse cannot disable tools while the transcript holds
                    # toolUse blocks; a returned tool call fails closed instead.
                    self.assertIn("toolConfig", calls[-1])
                else:
                    cap_key = {
                        "anthropic": "max_tokens",
                        "openai": "max_output_tokens",
                        "azure_adm": "max_completion_tokens",
                        "bedrock_mantle": "max_tokens",
                    }[provider]
                    self.assertEqual(calls[-1][cap_key], 60)
                    self.assertEqual(
                        calls[-1]["tool_choice"],
                        {"type": "none"} if provider == "anthropic" else "none",
                    )
                self.assertIn("ORKA BUDGET NOTE", json.dumps(calls[0]))
                self.assertTrue(self._review_permit_record().get("completion_receipt"))

    def test_report_open_reservations_follow_report_filters(self):
        moment = api_agent.dt.datetime.now(api_agent.dt.timezone.utc).isoformat()
        reservations = [
            {
                "kind": "reservation",
                "timestamp": moment,
                "reservation_id": f"resv_other_{index}",
                "run_id": f"other-{index}",
                "role": "implementer",
                "ticket": "OTHER-1",
                "sprint": "SPRINT-1",
                "provider": "anthropic",
                "model": "test-model",
                "projected_cost_usd": "1",
            }
            for index in range(9)
        ]
        self._write_ledger(
            [self._usage_event("implementer", "0.100000"), *reservations]
        )
        scoped = api_agent.build_report(self.root, self._report_args(ticket="PROD-1"))
        self.assertEqual(scoped["open_reservations"], [])
        self.assertEqual(scoped["open_reservations_ledger_wide"], 9)
        text = api_agent.format_report(scoped)
        self.assertNotIn("open reservations: 9", text)
        self.assertIn("0 match these filters (9 ledger-wide)", text)
        other = api_agent.build_report(self.root, self._report_args(ticket="OTHER-1"))
        self.assertEqual(len(other["open_reservations"]), 9)
        unfiltered = api_agent.build_report(self.root, self._report_args())
        self.assertEqual(len(unfiltered["open_reservations"]), 9)
        self.assertIn("open reservations: 9 ", api_agent.format_report(unfiltered))

    def test_token_count_failure_can_retry_without_outstanding_permit(self):
        transport = FakeTransport([], count=0)
        agent = self.agent(transport)
        with self.assertRaisesRegex(api_agent.AgentError, "no input token count"):
            agent.run({"model": "test-model", "max_tokens": 100})
        self.assertTrue(self.phase_permit())

    def test_nonterminal_openai_response_keeps_review_fenced(self):
        transport = FakeTransport(
            [
                {
                    "id": "resp_live",
                    "status": "in_progress",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "output": [],
                }
            ]
        )
        agent = self.agent(transport, provider="openai")
        self.assertEqual(
            agent.run({"model": "test-model", "max_output_tokens": 100})["status"],
            "needs_reconcile",
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self.phase_permit()

    def test_token_counter_failure_after_tool_turn_allows_review_retry(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_tools",
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "git_status",
                            "input": {},
                        }
                    ],
                }
            ]
        )
        agent = self.agent(transport)
        with mock.patch.object(
            agent,
            "_count",
            side_effect=[10, api_agent.AgentError("counter unavailable")],
        ):
            with self.assertRaisesRegex(api_agent.AgentError, "counter unavailable"):
                agent.run({"model": "test-model", "max_tokens": 100})
        self.assertTrue(self.phase_permit())
        self.assertGreater(api_agent.Decimal(agent.ledger.summary()["cost_usd"]), 0)

    def test_released_requests_free_reviewer_slots_but_attempts_stay_bounded(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(api_agent.DEFAULT_BUDGETS)
        limits["max_reviewer_runs_per_ticket"] = 1
        limits["max_model_runs_per_ticket"] = 3
        common = dict(
            projected=api_agent.Decimal(".01"),
            limits=limits,
            ticket="PROJ-1",
            sprint="1",
            provider="anthropic",
            model="m",
            role="code-reviewer",
        )
        for run in ("rejected-1", "rejected-2"):
            reservation = ledger.reserve(run_id=run, **common)
            ledger.release(reservation, run, "known rejection")
        ledger.reserve(run_id="accepted", **common)
        with self.assertRaisesRegex(api_agent.BudgetError, "max_model_runs_per_ticket"):
            ledger.reserve(run_id="unbounded-retry", **common)

    def test_rejected_final_request_does_not_erase_accepted_review_work(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(api_agent.DEFAULT_BUDGETS, max_reviewer_runs_per_ticket=1)
        common = dict(
            projected=api_agent.Decimal(".01"),
            limits=limits,
            ticket="PROJ-1",
            sprint="1",
            provider="anthropic",
            model="m",
            role="code-reviewer",
        )
        reservation = ledger.reserve(run_id="partial", **common)
        ledger.settle(
            reservation,
            run_id="partial",
            ticket="PROJ-1",
            sprint="1",
            provider="anthropic",
            model="m",
            response_id="msg_partial",
            usage={},
            cost=api_agent.Decimal(".005"),
            role="code-reviewer",
        )
        rejected = ledger.reserve(run_id="partial", **common)
        ledger.release(rejected, "partial", "known rejection")
        with self.assertRaisesRegex(
            api_agent.BudgetError, "max_reviewer_runs_per_ticket"
        ):
            ledger.reserve(run_id="replacement", **common)

    def test_sprint_reservation_pressure_is_not_a_ticket_pause(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(
            api_agent.DEFAULT_BUDGETS, max_usd_per_sprint=api_agent.Decimal("1")
        )
        common = dict(limits=limits, sprint="1", provider="anthropic", model="m")
        reservation = ledger.reserve(
            projected=api_agent.Decimal(".8"), run_id="a", ticket="PROJ-1", **common
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "max_usd_per_sprint"):
            ledger.reserve(
                projected=api_agent.Decimal(".3"), run_id="b", ticket="PROJ-2", **common
            )
        ledger.release(reservation, "a", "known rejection")
        # Historical v1.0.1 pressure events must also stop latching ticket pauses.
        ledger.append(
            {
                "kind": "ticket_budget_pause",
                "ticket": "PROJ-2",
                "reason": "max_usd_per_sprint",
            }
        )
        ledger.reserve(
            projected=api_agent.Decimal(".3"),
            run_id="b-retry",
            ticket="PROJ-2",
            **common,
        )

    def test_settled_sprint_exhaustion_still_blocks_other_tickets(self):
        ledger = api_agent.UsageLedger(self.root)
        limits = dict(
            api_agent.DEFAULT_BUDGETS, max_usd_per_sprint=api_agent.Decimal("1")
        )
        common = dict(limits=limits, sprint="1", provider="anthropic", model="m")
        reservation = ledger.reserve(
            projected=api_agent.Decimal("1"), run_id="a", ticket="PROJ-1", **common
        )
        ledger.settle(
            reservation,
            run_id="a",
            ticket="PROJ-1",
            sprint="1",
            provider="anthropic",
            model="m",
            response_id="msg_a",
            usage={},
            cost=api_agent.Decimal("1"),
        )
        with self.assertRaisesRegex(api_agent.BudgetError, "max_usd_per_sprint"):
            ledger.reserve(
                projected=api_agent.Decimal(".01"),
                run_id="b",
                ticket="PROJ-2",
                **common,
            )

    def test_ambiguous_submission_keeps_reservation_for_reconciliation(self):
        transport = FakeTransport([api_agent.ProviderAmbiguous("timeout")])
        agent = self.agent(transport, run_id="ambiguous-run")
        with self.assertRaises(api_agent.ProviderAmbiguous):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(agent.state["status"], "needs_reconcile")
        self.assertEqual(len(agent.ledger.summary()["open_reservations"]), 1)
        with self.assertRaises(subprocess.CalledProcessError):
            self.phase_permit()

        args = type(
            "Args",
            (),
            {
                "run_id": "ambiguous-run",
                "outcome": "not-found",
                "evidence": "provider dashboard search at 2026-08-26T12:00Z",
                "response_id": None,
                "input_tokens": 0,
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "config": str(self.root / ".orchestration" / "config.yaml"),
            },
        )()
        reconciled = api_agent.reconcile_run(args, self.root)
        self.assertEqual(reconciled["status"], "reconciled_not_found")
        self.assertEqual(reconciled["usage"]["open_reservations"], [])

    def _needs_reconcile_review(self, run_id):
        agent = self.agent(
            FakeTransport([api_agent.ProviderAmbiguous("503 after submission")]),
            run_id=run_id,
        )
        token = agent.review_authorization
        with self.assertRaises(api_agent.ProviderAmbiguous):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(agent.state["status"], "needs_reconcile")
        return agent, token

    def _reconcile_args(self, run_id, outcome="not-found", **usage):
        return type(
            "Args",
            (),
            {
                "run_id": run_id,
                "outcome": outcome,
                "evidence": "provider dashboard search at 2026-09-17T12:00Z",
                "response_id": usage.pop("response_id", None),
                "input_tokens": usage.get("input_tokens", 0),
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": usage.get("output_tokens", 0),
                "config": str(self.root / ".orchestration" / "config.yaml"),
            },
        )()

    def _review_permit(self, token, pr="1"):
        from review_permit import ledger_path

        ledger = json.loads(
            ledger_path(self.root, ".orchestration/.review-ledger", pr).read_text(
                encoding="utf-8"
            )
        )
        return next(p for p in ledger["review_permits"] if p["token"] == token)

    def _ledger_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/review-ledger.py"), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def test_started_review_refusal_names_permit_run_and_recovery(self):
        _, token = self._needs_reconcile_review("refusal-run")
        head = self._review_permit(token)["head"]
        refused = self._ledger_cli(
            "permit-review", "1", "--role", "code-reviewer", "--head", head
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(token[:14], refused.stderr)
        self.assertNotIn(token, refused.stderr)
        self.assertIn("run refusal-run", refused.stderr)
        self.assertIn("api_agent.py reconcile --run-id refusal-run", refused.stderr)
        self.assertIn("cancel-permit", refused.stderr)

    def test_reconcile_not_found_cancels_started_review_permit(self):
        agent, token = self._needs_reconcile_review("not-found-review")
        binding = json.loads(agent.state_path.read_text(encoding="utf-8"))[
            "review_permit"
        ]
        self.assertEqual(binding["pr"], "1")
        self.assertEqual(binding["role"], "code-reviewer")
        self.assertNotIn(token, json.dumps(binding))
        reconciled = api_agent.reconcile_run(
            self._reconcile_args("not-found-review"), self.root
        )
        self.assertEqual(reconciled["status"], "reconciled_not_found")
        self.assertEqual(reconciled["usage"]["open_reservations"], [])
        self.assertEqual(reconciled["review_permit"]["status"], "cancelled")
        permit = self._review_permit(token)
        self.assertTrue(permit["cancelled_at"])
        self.assertIn("not-found", permit["cancellation_reason"])
        self.assertFalse(permit["completion_receipt"])
        self.assertNotEqual(self.phase_permit(), token)

    def test_reconcile_completed_cancels_permit_without_a_receipt(self):
        _, token = self._needs_reconcile_review("completed-review")
        reconciled = api_agent.reconcile_run(
            self._reconcile_args(
                "completed-review",
                outcome="completed",
                response_id="msg_found",
                input_tokens=40,
                output_tokens=5,
            ),
            self.root,
        )
        self.assertEqual(reconciled["status"], "reconciled_completed")
        self.assertGreater(api_agent.Decimal(reconciled["cost_usd"]), 0)
        self.assertEqual(reconciled["review_permit"]["status"], "cancelled")
        self.assertIn("re-run", reconciled["review_permit"]["message"])
        permit = self._review_permit(token)
        self.assertTrue(permit["cancelled_at"])
        self.assertFalse(permit["completion_receipt"])
        self.assertNotEqual(self.phase_permit(), token)

    def test_reconcile_reports_an_already_cancelled_permit(self):
        agent, token = self._needs_reconcile_review("already-cancelled")
        api_agent.cancel_unresolved_review_permit(
            shared_root=self.root,
            ledger_dir=".orchestration/.review-ledger",
            pr="1",
            token=token,
            reason="operator cleanup",
            timestamp="earlier",
        )
        reconciled = api_agent.reconcile_run(
            self._reconcile_args("already-cancelled"), self.root
        )
        self.assertEqual(reconciled["status"], "reconciled_not_found")
        self.assertEqual(reconciled["usage"]["open_reservations"], [])
        self.assertEqual(reconciled["review_permit"]["status"], "already_cancelled")
        self.assertEqual(self._review_permit(token)["cancelled_at"], "earlier")

    def test_failed_money_reconciliation_never_cancels_the_permit(self):
        _, token = self._needs_reconcile_review("money-first")
        with self.assertRaisesRegex(api_agent.AgentError, "response-id"):
            api_agent.reconcile_run(
                self._reconcile_args("money-first", outcome="completed"), self.root
            )
        permit = self._review_permit(token)
        self.assertTrue(permit["started_at"])
        self.assertFalse(permit.get("cancelled_at"))

    def test_cancel_permit_fails_closed_until_provider_work_is_reconciled(self):
        agent, token = self._needs_reconcile_review("legacy-review")
        # A run state written before permit binding cannot cancel on reconcile.
        state = json.loads(agent.state_path.read_text(encoding="utf-8"))
        state.pop("review_permit")
        agent.state_path.write_text(json.dumps(state), encoding="utf-8")
        refused = self._ledger_cli(
            "cancel-permit", "1", "--phase-permit", token, "--reason", "stuck"
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("open usage reservation", refused.stderr)
        self.assertIn("legacy-review", refused.stderr)
        self.assertFalse(self._review_permit(token).get("cancelled_at"))

        reconciled = api_agent.reconcile_run(
            self._reconcile_args("legacy-review"), self.root
        )
        self.assertEqual(reconciled["review_permit"]["status"], "unbound")
        self.assertIn("cancel-permit", reconciled["review_permit"]["message"])
        self.assertTrue(self._review_permit(token)["started_at"])

        cancelled = self._ledger_cli(
            "cancel-permit",
            "1",
            "--phase-permit",
            token,
            "--role",
            "code-reviewer",
            "--reason",
            "provider confirmed no request",
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(
            json.loads(cancelled.stdout)["permit_cancelled"]["status"], "cancelled"
        )
        permit = self._review_permit(token)
        self.assertEqual(permit["cancellation_reason"], "provider confirmed no request")
        self.assertTrue(permit["cancelled_at"])
        self.assertFalse(permit["completion_receipt"])
        self.assertNotEqual(self.phase_permit(), token)

    def test_bulk_reservation_reconciliation_requires_and_preserves_evidence(self):
        ledger = api_agent.UsageLedger(self.root)
        run_id = "historical-timeout"
        reservation = ledger.reserve(
            projected=api_agent.Decimal(".10"),
            limits=dict(api_agent.DEFAULT_BUDGETS),
            run_id=run_id,
            ticket="PROJ-1",
            sprint="1",
            provider="anthropic",
            model="test-model",
            role="implementer",
        )
        state_path = self.root / ".orchestration" / ".llm-runs" / f"{run_id}.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "needs_reconcile",
                    "pending_reservation": reservation,
                    "pending_request": {"request": "redacted"},
                }
            ),
            encoding="utf-8",
        )

        plan = api_agent.reservation_migration_plan(self.root)
        self.assertEqual(plan["entries"][0]["reservation_id"], reservation)
        self.assertEqual(plan["entries"][0]["evidence"], "")

        manifest_path = self.root / ".orchestration" / "reservation-migration.json"
        manifest_path.write_text(json.dumps(plan), encoding="utf-8")
        args = type("Args", (), {"manifest": str(manifest_path), "apply": False})()
        with self.assertRaisesRegex(api_agent.AgentError, "requires a valid run"):
            api_agent.reconcile_reservation_manifest(args, self.root)

        plan["entries"][0]["evidence"] = (
            "Anthropic dashboard search on 2026-09-14 found no request or usage."
        )
        manifest_path.write_text(json.dumps(plan), encoding="utf-8")
        args.apply = True
        result = api_agent.reconcile_reservation_manifest(args, self.root)
        self.assertEqual(result["applied_runs"], [run_id])
        self.assertEqual(result["usage"]["open_reservations"], [])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "reconciled_not_found")
        self.assertIn("Anthropic dashboard", state["reconciliation_evidence"])

        # Reapplying the exact audited manifest is deliberately idempotent.
        repeated = api_agent.reconcile_reservation_manifest(args, self.root)
        self.assertEqual(repeated["applied_runs"], [run_id])
        self.assertEqual(repeated["usage"]["open_reservations"], [])

    def test_reviewer_cannot_add_write_tool(self):
        config = self.config(
            extra="    code-reviewer:\n      allowed_tools: [read_file, apply_patch]"
        )
        with self.assertRaisesRegex(api_agent.AgentError, "may not receive"):
            api_agent.ApiAgent(
                root=self.root,
                config_path=config,
                role="code-reviewer",
                ticket=None,
                sprint=None,
                run_id="forbidden-tool",
                transport=FakeTransport([]),
            )

    def test_ticket_scoper_is_read_only_and_cannot_run_checks(self):
        tools = api_agent.tools_for_role("ticket-scoper", None, "openai")
        self.assertEqual(
            {tool["type"] + ":" + tool["name"] for tool in tools},
            {"function:read_file", "function:search", "function:git_status"},
        )
        with self.assertRaisesRegex(api_agent.AgentError, "may not receive"):
            api_agent.tools_for_role(
                "ticket-scoper", ["read_file", "run_check"], "openai"
            )

    def test_tool_paths_cannot_escape_repository(self):
        executor = api_agent.ToolExecutor(self.root, {}, 1000, 5)
        with self.assertRaisesRegex(api_agent.AgentError, "escapes repository"):
            executor.execute("read_file", {"path": "../secret"})

    def test_missing_price_fails_closed(self):
        config = self.config()
        text = config.read_text(encoding="utf-8").replace(
            "    test-model:\n", "    another-model:\n"
        )
        config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(api_agent.AgentError, "pricing.test-model"):
            api_agent.ApiAgent(
                root=self.root,
                config_path=config,
                role="code-reviewer",
                ticket=None,
                sprint=None,
                run_id="missing-price",
                transport=FakeTransport([]),
            )

    def test_response_without_usage_remains_reserved(self):
        transport = FakeTransport(
            [
                {
                    "id": "msg_no_usage",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "done"}],
                }
            ]
        )
        agent = self.agent(transport, run_id="missing-usage")
        with self.assertRaises(api_agent.ProviderAmbiguous):
            agent.run(
                {
                    "model": "test-model",
                    "max_tokens": 100,
                    "system": [],
                    "messages": [{"role": "user", "content": "review"}],
                }
            )
        self.assertEqual(agent.state["status"], "needs_reconcile")
        self.assertEqual(len(agent.ledger.summary()["open_reservations"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
