"""Loopback admission gateway for cooperative native Claude workers.

Each message reserves its full token envelope before reaching the provider.
Ambiguous requests retain reservations. This is a spending boundary for clients
using the gateway, not an OS sandbox for arbitrary programs run by those clients.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from api_agent import (AgentError, BudgetError, HttpTransport, Pricing,
                       ProviderHTTPError, ProviderAdmissionError, UsageLedger, budgets_from_config,
                       normalize_usage, anthropic_context_beta)


def claude_child_environment(environment, token, endpoint):
    excluded = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}
    result = {key: value for key, value in environment.items()
              if not key.startswith("ANTHROPIC_") and key not in excluded}
    result.update(ANTHROPIC_BASE_URL=endpoint, ANTHROPIC_API_KEY=token, ANTHROPIC_AUTH_TOKEN=token)
    return result


def claude_launch_arguments(command, token, endpoint):
    """Preserve CLI settings while making the metered route the final env source.

    Claude loads settings after process env. A controller-owned --settings env
    therefore must also override user/project routing without disabling their
    unrelated configuration. Never include settings contents in errors.
    """
    arguments = [command[0]]
    settings = {}
    index = 1
    while index < len(command):
        argument = command[index]
        if argument == '--':
            break
        if argument == '--settings' or argument.startswith('--settings='):
            if argument == '--settings':
                index += 1
                if index == len(command):
                    raise AgentError('Claude --settings requires a JSON object or file')
                value = command[index]
            else:
                value = argument.split('=', 1)[1]
            try:
                settings = json.loads(value if value.lstrip().startswith('{') else Path(value).read_text())
                if not isinstance(settings, dict) or not isinstance(settings.get('env', {}), dict):
                    raise ValueError()
            except (OSError, ValueError):
                raise AgentError('Claude --settings requires a valid JSON object with an env object') from None
        else:
            arguments.append(argument)
        index += 1
    env = {**settings.get('env', {}), 'ANTHROPIC_BASE_URL': endpoint,
           'ANTHROPIC_API_KEY': token, 'ANTHROPIC_AUTH_TOKEN': token}
    # Explicit false/empty values override alternate providers in lower-priority
    # settings sources, which removal from the inherited environment cannot do.
    env.update(CLAUDE_CODE_USE_BEDROCK='0', CLAUDE_CODE_USE_VERTEX='0',
               CLAUDE_CODE_USE_FOUNDRY='0', CLAUDE_CODE_OAUTH_TOKEN='')
    arguments.extend(['--settings', json.dumps({**settings, 'env': env})])
    return arguments + command[index:]


def stream_events(response):
    """Render a settled, buffered Messages response using Anthropic SSE framing."""
    start = {**response, "content": [], "stop_reason": None, "stop_sequence": None}
    start["usage"] = {**response["usage"], "output_tokens": 0}
    yield {"type": "message_start", "message": start}
    for index, block in enumerate(response.get("content", [])):
        kind = block.get("type")
        initial = dict(block)
        delta = None
        if kind == "text":
            initial["text"] = ""
            delta = {"type": "text_delta", "text": block.get("text", "")}
        elif kind == "tool_use":
            initial["input"] = {}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}))}
        elif kind == "thinking":
            initial["thinking"] = ""
            initial.pop("signature", None)
            delta = {"type": "thinking_delta", "thinking": block.get("thinking", "")}
        yield {"type": "content_block_start", "index": index, "content_block": initial}
        if delta:
            yield {"type": "content_block_delta", "index": index, "delta": delta}
        if kind == "thinking" and block.get("signature"):
            yield {"type": "content_block_delta", "index": index,
                   "delta": {"type": "signature_delta", "signature": block["signature"]}}
        yield {"type": "content_block_stop", "index": index}
    yield {"type": "message_delta", "delta": {
        "stop_reason": response.get("stop_reason"),
        "stop_sequence": response.get("stop_sequence")}, "usage": response["usage"]}
    yield {"type": "message_stop"}


class NativeGateway:
    origin = "native-gateway"

    def __init__(self, root: Path, config: dict, ticket: str, sprint: str,
                 run_id: str, transport=None):
        from provider_health import ProviderHealth
        self.health = ProviderHealth(root)
        self.config = config
        self.ledger = UsageLedger(root)
        self.limits = budgets_from_config(config)
        self.context = dict(ticket=ticket, sprint=sprint, run_id=run_id,
                            provider="anthropic", role="implementer")
        self.transport = (
            transport
            if transport is not None
            else HttpTransport(
                timeout=self.limits["provider_read_timeout_seconds"]
            )
        )
        self.token = secrets.token_urlsafe(32)
        self.stopped = threading.Event()
        self.reason = ""
        self.stop_error = None
        self.stop_lock = threading.Lock()
        self.server = None
        self.provider_accepted = False
        self.provider_uncertain = False
        self.provider_rejected = False
        self.provider_inflight = 0
        self.provider_lock = threading.Lock()

    def stop(self, reason, error=None):
        # Preserve the first failure when concurrent/retried requests arrive.
        with self.stop_lock:
            if not self.stopped.is_set():
                self.reason = reason
                self.stop_error = error
                self.stopped.set()

    def raise_if_stopped(self):
        if self.stopped.is_set():
            raise self.stop_error if self.stop_error is not None else AgentError(self.reason)

    def model_request(self, *args, count_only=False, **kwargs):
        provider = args[0]
        status = self.health.status(provider)
        if status["state"] in {"rate_limited", "authentication", "incompatible", "transport"}:
            raise ProviderAdmissionError("provider admission held: " + status["state"])
        with self.provider_lock:
            self.provider_inflight += 1
        try:
            response = self.transport.request(*args, **kwargs)
            if not count_only:
                self.provider_accepted = True  # Includes malformed/unsettled responses.
            return response
        except ProviderHTTPError as exc:
            if exc.status in {401,403}:
                self.health.failure(provider, "authentication")
            elif exc.status in {429,529}:
                self.health.failure(provider, "rate_limited", getattr(exc, "retry_after_seconds", 30))
            if exc.status == 400 and "context_management" in exc.body:
                self.health.failure(provider, "incompatible")
            if exc.status in {429, 529}:
                self.provider_rejected = True
            else:
                self.provider_uncertain = True
            raise
        except Exception:
            self.provider_uncertain = True
            raise
        finally:
            with self.provider_lock:
                self.provider_inflight -= 1

    def startup_retryable(self):
        with self.provider_lock:
            return (self.provider_rejected and not self.provider_accepted
                    and not self.provider_uncertain and self.provider_inflight == 0)

    def request(self, path, payload):
        self.raise_if_stopped()
        anthropic_context_beta(payload)
        if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
            raise AgentError("unsupported native gateway endpoint")
        model = payload.get("model", "")
        pricing = Pricing.from_config(self.config, model)
        # Token prices cannot bound provider-hosted tools or premium service
        # tiers. Fail before forwarding such requests instead of undercharging.
        if payload.get("service_tier", "auto") != "auto" or any(
            tool.get("type", "custom") != "custom" for tool in payload.get("tools", [])
        ):
            raise AgentError("native gateway supports custom client tools and standard token pricing only")
        count_payload = {k: v for k, v in payload.items()
                         if k in {"model", "messages", "system", "tools", "tool_choice", "thinking"}}
        counted = self.model_request("anthropic", "/messages/count_tokens", count_payload, count_only=True)
        count = counted.get("input_tokens")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise AgentError("provider omitted valid input token count")
        if path.endswith("count_tokens"):
            return counted
        maximum = payload.get("max_tokens")
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
            raise AgentError("max_tokens must be a positive integer")
        maximum = min(maximum, self.limits["max_output_tokens_per_turn"])
        body = {**payload, "max_tokens": maximum, "stream": False}
        thinking = payload.get("thinking") or {}
        if thinking.get("type") == "enabled":
            budget = thinking.get("budget_tokens")
            if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1024 or maximum <= 1024:
                raise AgentError("manual thinking requires a budget of at least 1024 below the output cap")
            body["thinking"] = {**thinking, "budget_tokens": min(budget, maximum - 1)}
        reservation = self.ledger.reserve(
            projected=pricing.worst_case(count, maximum), limits=self.limits,
            model=model, origin=self.origin, **self.context)
        try:
            response = self.model_request("anthropic", "/messages",
                body,
                idempotency_key=reservation)
        except ProviderAdmissionError:
            self.ledger.release(reservation, self.context["run_id"], "shared provider admission refused before submission")
            raise
        except ProviderHTTPError as exc:
            if exc.status in {400, 401, 403, 404, 413, 422, 429, 529}:
                self.ledger.release(reservation, self.context["run_id"], "native request rejected")
            raise
        usage = response.get("usage")
        if not response.get("id") or not isinstance(usage, dict) or any(
            not isinstance(usage.get(key), int) or isinstance(usage.get(key), bool) or usage[key] < 0
            for key in ("input_tokens", "output_tokens", *(
                key for key in ("cache_read_input_tokens", "cache_creation_input_tokens") if key in usage))
        ):
            raise AgentError("native response missing settlement evidence; reservation retained")
        normalized = normalize_usage("anthropic", response)
        self.ledger.settle(reservation, model=model, response_id=response["id"],
                           usage=normalized, cost=pricing.actual_cost(normalized), **self.context)
        return response

    def is_streaming(self, path, payload):
        return payload.get("stream") is True and path == "/v1/messages"

    def events(self, response):
        return stream_events(response)

    def start(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)  # An abandoned client must not prevent shutdown.

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass  # Native clients may abandon discovery/error responses.

            def log_message(self, *_args):
                pass  # Never log credentials or prompts.

            def do_POST(self):
                supplied = self.headers.get("x-api-key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
                if not secrets.compare_digest(supplied, gateway.token):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32 * 1024 * 1024:
                        raise AgentError("invalid native request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise AgentError("native request must be an object")
                    response = gateway.request(self.path.split("?", 1)[0], payload)
                    streaming = gateway.is_streaming(self.path.split("?", 1)[0], payload)
                    if streaming:
                        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                                       for event in gateway.events(response)).encode()
                    else:
                        body = json.dumps(response).encode()
                    status = 200
                    content_type = "text/event-stream" if streaming else "application/json"
                except (AgentError, ValueError, TypeError, KeyError) as exc:
                    if isinstance(exc, AgentError) and str(exc).startswith(("native Codex gateway supports", "native Codex requires", "native gateway supports")):
                        gateway.health.failure(gateway.context["provider"], "incompatible")
                    rate_limited = isinstance(exc, ProviderHTTPError) and exc.status in {429, 529}
                    gateway.stop("provider_rate_limited" if rate_limited else "provider_authentication" if isinstance(exc, ProviderHTTPError) and exc.status in {401,403} else str(exc), error=exc)
                    # A local budget refusal is not an upstream rate limit.
                    status, content_type = (exc.status if isinstance(exc, ProviderHTTPError) else 402 if isinstance(exc, BudgetError) else 502), "application/json"
                    body = json.dumps({"type": "error", "error": {
                        "type": "budget_error" if isinstance(exc, BudgetError) else "api_error",
                        "message": "Native gateway stopped this lane; inspect its supervisor record."}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Join in-flight handlers before emitting a terminal startup receipt.
        self.server.daemon_threads = False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
