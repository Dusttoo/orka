"""Meter native Codex Responses requests through the shared admission ledger."""
from __future__ import annotations

import copy
import json
import os
import shlex
import sys
from pathlib import Path

from api_agent import AgentError, BudgetError, Pricing, ProviderHTTPError, ProviderAdmissionError, normalize_usage
from native_gateway import ClientIncompatibleError, NativeGateway, unmetered_endpoint

RESPONSES_PATH = "/v1/responses"
COMPACT_PATH = "/v1/responses/compact"


def response_events(response):
    """Convert a settled buffered response to Responses SSE, including tool calls."""
    sequence = 0
    def event(kind, **fields):
        nonlocal sequence
        value = dict(type=kind, sequence_number=sequence, **fields)
        sequence += 1
        return value
    start = {**response, "status": "in_progress", "output": [], "usage": None}
    yield event("response.created", response=start)
    yield event("response.in_progress", response=start)
    for index, item in enumerate(response.get("output", [])):
        initial = copy.deepcopy(item)
        initial["status"] = "in_progress"
        kind = item.get("type")
        if kind == "message":
            initial["content"] = []
        elif kind == "function_call":
            initial["arguments"] = ""
        elif kind == "custom_tool_call":
            initial["input"] = ""
        yield event("response.output_item.added", output_index=index, item=initial)
        if kind == "message":
            for part_index, part in enumerate(item.get("content", [])):
                initial_part = {**part}
                field = "text" if part.get("type") == "output_text" else "refusal"
                initial_part[field] = ""
                common = dict(item_id=item["id"], output_index=index, content_index=part_index)
                yield event("response.content_part.added", **common, part=initial_part)
                prefix = "response.output_text" if field == "text" else "response.refusal"
                yield event(prefix + ".delta", **common, delta=part.get(field, ""))
                yield event(prefix + ".done", **common, **{field: part.get(field, "")})
                yield event("response.content_part.done", **common, part=part)
        elif kind in {"function_call", "custom_tool_call"}:
            field = "arguments" if kind == "function_call" else "input"
            prefix = "response.function_call_arguments" if kind == "function_call" else "response.custom_tool_call_input"
            common = dict(item_id=item["id"], output_index=index)
            yield event(prefix + ".delta", **common, delta=item.get(field, ""))
            yield event(prefix + ".done", **common, **{field: item.get(field, "")})
        yield event("response.output_item.done", output_index=index, item=item)
    terminal = "response.completed" if response["status"] == "completed" else "response.incomplete"
    yield event(terminal, response=response)


def client_tools_only(tools):
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            return False
        if tool.get("type") == "namespace":
            if not client_tools_only(tool.get("tools")):
                return False
        elif tool.get("type") == "tool_search" and tool.get("execution") == "client":
            continue
        elif tool.get("type") not in {"function", "custom"}:
            return False
    return True


class CodexGateway(NativeGateway):
    origin = "codex-gateway"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context["provider"] = "openai"

    def is_streaming(self, path, payload):
        return path == "/v1/responses" and payload.get("stream") is True

    def events(self, response):
        return response_events(response)

    def request(self, path, payload):
        self.raise_if_stopped()
        compact = path == COMPACT_PATH
        if path != RESPONSES_PATH and not compact:
            raise ClientIncompatibleError(unmetered_endpoint(path, (RESPONSES_PATH, COMPACT_PATH)))
        if (payload.get("background") or payload.get("previous_response_id") or payload.get("conversation")
                or payload.get("context_management")
                or payload.get("service_tier", "default") not in {"auto", "default", None}
                or (compact and payload.get("stream") is not None and payload.get("stream") is not False)
                or not client_tools_only(payload.get("tools", []))):
            raise ClientIncompatibleError("native Codex requires stateless standard-tier requests with client tools only")
        for item in payload.get("input", []) if isinstance(payload.get("input"), list) else []:
            if isinstance(item, dict) and item.get("type") == "tool_search_output":
                if item.get("execution") != "client" or not client_tools_only(item.get("tools")):
                    raise AgentError("deferred tools must execute on the client")
        model = payload.get("model", "")
        pricing = Pricing.from_config(self.config, model)
        maximum = payload.get("max_output_tokens", self.limits["max_output_tokens_per_turn"])
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
            raise AgentError("max_output_tokens must be a positive integer")
        maximum = min(maximum, self.limits["max_output_tokens_per_turn"])
        if compact:
            # Remote compaction is a unary, stateless Responses-shaped request.
            # Forward only what the client sent: the endpoint's accepted
            # parameters are not controller-owned, so no fields are injected.
            body = dict(payload)
            if "max_output_tokens" in body:
                body["max_output_tokens"] = maximum
        else:
            body = {**payload, "stream": False, "store": False, "background": False,
                    "service_tier": "default", "max_output_tokens": maximum}
        body.pop("client_metadata", None)
        count_body = {key: value for key, value in body.items() if key in {
            "model", "input", "instructions", "tools", "tool_choice", "text", "reasoning",
            "parallel_tool_calls", "personality", "truncation"}}
        counted = self.model_request("openai", "responses/input_tokens", count_body, count_only=True)
        count = counted.get("input_tokens")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise AgentError("provider omitted a valid input token count")
        reservation = self.ledger.reserve(projected=pricing.worst_case(count, maximum),
            limits=self.limits, model=model, origin=self.origin, **self.context)
        try:
            response = self.model_request("openai", "responses/compact" if compact else "responses",
                                          body, idempotency_key=reservation)
        except ProviderAdmissionError:
            self.ledger.release(reservation, self.context["run_id"], "shared provider admission refused before submission")
            raise
        except ProviderHTTPError as exc:
            if exc.status in {400, 401, 403, 404, 413, 422, 429}:
                self.ledger.release(reservation, self.context["run_id"], "native Codex request rejected")
            raise
        usage = response.get("usage")
        # A compaction result has output items but no response lifecycle status.
        terminal = (isinstance(response.get("output"), list) if compact
                    else response.get("status") in {"completed", "incomplete"})
        if (not response.get("id") or not terminal
                or not isinstance(usage, dict) or any(
                    not isinstance(usage.get(key), int) or isinstance(usage.get(key), bool) or usage[key] < 0
                    for key in ("input_tokens", "output_tokens"))):
            raise AgentError("native Codex response lacks terminal usage evidence; reservation retained")
        for details_key, count_key, total_key in (
                ("input_tokens_details", "cached_tokens", "input_tokens"),
                ("output_tokens_details", "reasoning_tokens", "output_tokens")):
            details = usage.get(details_key) or {}
            if not isinstance(details, dict):
                raise AgentError("invalid native Codex usage details; reservation retained")
            value = details.get(count_key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= usage[total_key]:
                raise AgentError("invalid native Codex usage details; reservation retained")
        normalized = normalize_usage("openai", response)
        if any(value < 0 for value in normalized.values()) or normalized["cache_read_tokens"] > usage["input_tokens"]:
            raise AgentError("native Codex response has invalid token accounting; reservation retained")
        self.ledger.settle(reservation, model=model, response_id=response["id"],
            usage=normalized, cost=pricing.actual_cost(normalized), **self.context)
        return response


def launch_arguments(command, endpoint):
    """Controller overrides win without altering the user's persistent config."""
    if len(command) < 2 or command[1] != "exec":
        raise AgentError("metered native Codex requires a direct codex exec launch")
    settings = {
        "model_provider": "orka_metered",
        "model_providers.orka_metered.name": "Orka metered Responses",
        "model_providers.orka_metered.base_url": endpoint + "/v1",
        "model_providers.orka_metered.env_key": "ORKA_NATIVE_GATEWAY_TOKEN",
        "model_providers.orka_metered.wire_api": "responses",
        "model_providers.orka_metered.requires_openai_auth": False,
        "model_providers.orka_metered.supports_websockets": False,
        "web_search": "disabled",
        "service_tier": "default",
        "features.remote_compaction_v2": False,
    }
    overrides = [part for key, value in settings.items() for part in ("-c", key + "=" + json.dumps(value))]
    # Keep overrides on the exec parser; reject caller routing overrides
    # instead of relying on duplicate-option precedence across CLI versions.
    arguments = command[2:]
    for index, arg in enumerate(arguments):
        value = None
        if arg in {"-c", "--config"} and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif arg.startswith("--config="):
            value = arg[len("--config="):]
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:]
        key = value.split("=", 1)[0].strip() if value else ""
        if (key == "model_provider" or key.startswith("model_providers") or key in {"openai_base_url", "service_tier", "features.remote_compaction_v2"}
                or arg in {"--oss", "--local-provider"} or arg.startswith("--local-provider=")):
            raise AgentError("native Codex routing overrides conflict with metered execution")
    return [command[0], command[1], *overrides, *command[2:]]


def child_environment(environment, token, endpoint):
    result = {key: value for key, value in environment.items()
              if not key.startswith("OPENAI_") and key not in {"CODEX_API_KEY", "AZURE_OPENAI_API_KEY"}}
    result.update(ORKA_NATIVE_GATEWAY_TOKEN=token, OPENAI_API_KEY=token,
                  OPENAI_BASE_URL=endpoint + "/v1",
                  ORKA_NATIVE_GATEWAY_URL=endpoint)
    return result


def install_launcher(directory: Path, executable: str, environment: dict):
    """Keep ordinary nested `codex exec` calls on the same metered endpoint."""
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "codex"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                        shlex.quote(str(Path(__file__).resolve())) + ' "$@"\n')
    launcher.chmod(0o700)
    environment["ORKA_NATIVE_CODEX_EXECUTABLE"] = executable
    environment["PATH"] = str(directory) + os.pathsep + environment.get("PATH", "")


if __name__ == "__main__":
    try:
        executable = os.environ["ORKA_NATIVE_CODEX_EXECUTABLE"]
        arguments = launch_arguments([executable, *sys.argv[1:]], os.environ["ORKA_NATIVE_GATEWAY_URL"])
        os.execv(executable, arguments)
    except (AgentError, KeyError, OSError) as exc:
        print(f"metered Codex launcher: {exc}", file=sys.stderr)
        sys.exit(2)
