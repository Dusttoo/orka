"""Repository-wide provider admission, independent of ticket attempts and budgets.

Cooperative-worker runtime state. Only controller-owned probes clear incidents;
normal worker successes cannot clear a concurrent provider/authentication failure.
"""

from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import time
import uuid


class HealthError(RuntimeError):
    pass


DESKTOP_CLIENTS = {"openai": "codex", "anthropic": "claude"}
SUBSCRIPTION_ENVIRONMENT_KEYS = {
    "openai": {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    },
    "anthropic": {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CONFIG_DIR",
    },
}


def model_less_desktop_route(route):
    return (
        route.get("execution") == "desktop"
        and route.get("provider") == "openai"
        and not route.get("model")
    )


def desktop_subscription_status(route):
    """Validate a model-less subscription client without provider traffic."""
    if not model_less_desktop_route(route):
        raise HealthError("route is not a model-less desktop subscription route")
    client = DESKTOP_CLIENTS.get(route.get("provider"))
    executable = shutil.which(client) if client else None
    if not executable:
        return {
            "state": "incompatible",
            "reason": "configured desktop subscription client is unavailable",
            "client": client,
        }
    if client == "codex":
        environment = subscription_child_environment(os.environ, "openai")
        try:
            login = subprocess.run(
                [executable, "login", "status"],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "state": "incompatible",
                "reason": f"Codex subscription login could not be verified: {exc}",
                "client": client,
            }
        login_status = (login.stdout + "\n" + login.stderr).strip()
        if login.returncode != 0 or "logged in using chatgpt" not in login_status.lower():
            return {
                "state": "incompatible",
                "reason": "Codex is not authenticated with a ChatGPT subscription",
                "client": client,
            }
    return {
        "state": "healthy",
        "mode": "subscription",
        "client": client,
        "executable": str(Path(executable).resolve()),
        "route": route_identity(route),
    }


def subscription_child_environment(environment, provider):
    """Prevent inherited API credentials from overriding subscription login."""
    result = dict(environment)
    for key in SUBSCRIPTION_ENVIRONMENT_KEYS.get(provider, set()):
        result.pop(key, None)
    return result


def subscription_launch_command(command, route):
    """Bind a model-less client to subscription-safe configuration sources."""
    status = desktop_subscription_status(route)
    if status.get("state") != "healthy":
        raise HealthError(str(status.get("reason") or "subscription route is unavailable"))
    command = validate_native_command(command, route)
    provider = route.get("provider")
    if provider == "openai":
        # CLI configuration has highest precedence. Ignore the user's base
        # config and force the built-in OpenAI provider; authentication was
        # separately proven to be ChatGPT rather than an API key.
        command[2:2] = [
            "--ignore-user-config",
            "-c",
            'model_provider="openai"',
        ]
    else:
        raise HealthError("unsupported desktop subscription provider")
    return command


def bind_native_working_directory(command, route, working_directory):
    """Bind native Codex execution to the controller-authorized checkout.

    A caller-supplied ``--cd`` is routing input, not authority. Strip every
    spelling of it and inject the resolved controller-owned directory.
    """
    command = list(command)
    if route.get("execution") != "desktop" or route.get("provider") != "openai":
        return command
    bound = str(Path(working_directory).expanduser().resolve())
    cleaned = []
    index = 0
    while index < len(command):
        argument = command[index]
        if argument == "--":
            cleaned.extend(command[index:])
            break
        if argument in {"--cd", "-C"}:
            if index + 1 >= len(command) or command[index + 1] == "--":
                raise HealthError("native Codex --cd requires a value")
            index += 2
            continue
        if argument.startswith("--cd="):
            if not argument.split("=", 1)[1]:
                raise HealthError("native Codex --cd requires a value")
            index += 1
            continue
        if argument.startswith("-C") and argument != "-C":
            if not argument[2:].lstrip("="):
                raise HealthError("native Codex -C requires a value")
            index += 1
            continue
        if argument == "--worktree" or argument.startswith("--worktree="):
            raise HealthError("native Codex --worktree is not controller-authorized")
        cleaned.append(argument)
        index += 1
    if len(cleaned) < 2 or Path(cleaned[0]).name != "codex" or cleaned[1] != "exec":
        raise HealthError("working-directory binding requires direct codex exec")
    cleaned[2:2] = ["--cd", bound]
    return cleaned


HOLD_STATES = {"rate_limited", "authentication", "incompatible", "transport"}
SCOPED_INCIDENTS = "scoped_incidents"


def scoped_incident(client, detail):
    return dict(
        state="incompatible",
        client=str(client)[:64],
        reason=str(detail)[:300],
        failures=1,
        at=time.time(),
        incident=uuid.uuid4().hex,
    )


def clear_preserving_scoped(state):
    """Replace provider-level evidence without erasing route-scoped incidents."""
    incidents = state.get(SCOPED_INCIDENTS)
    state.clear()
    if incidents:
        state[SCOPED_INCIDENTS] = incidents


class ProviderHealth:
    def __init__(self, root):
        from runtime_state import shared_repository_root

        self.directory = (
            shared_repository_root(Path(root)) / ".orchestration/.provider-health"
        )

    @contextlib.contextmanager
    def locked(self, provider):
        if provider not in {
            "openai",
            "anthropic",
            "azure_adm",
            "bedrock",
            "bedrock_mantle",
        }:
            raise HealthError("unsupported provider")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (provider + ".json")
        with (self.directory / (provider + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(state, dict):
                    raise ValueError("expected object")
            except (ValueError, OSError) as exc:
                raise HealthError(
                    "provider health evidence is unreadable; repair is required"
                ) from exc
            yield state
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
            try:
                with temporary.open("x") as out:
                    json.dump(state, out, sort_keys=True)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def status(self, provider, route=None):
        """Return provider admission, narrowed to one route when it is named.

        Provider-level incidents, including legacy unscoped ``incompatible``
        records whose origin cannot be proven, hold every route. A route-scoped
        client incompatibility holds only callers naming that route identity;
        provider-wide callers (running gateways, API transports) ignore it.
        """
        with self.locked(provider) as state:
            result = dict(state)
        result.setdefault("state", "unverified")
        incidents = result.get(SCOPED_INCIDENTS) or {}
        scoped = incidents.get(route) if route is not None else None
        if result["state"] not in HOLD_STATES and isinstance(scoped, dict):
            return {**scoped, "state": "incompatible", "scope": "route", "route": route}
        if result["state"] == "healthy" and (
            result.get("valid_until", 0) < time.time()
            or route is not None
            and route not in result.get("routes", [])
        ):
            result["state"] = "unverified"
        result.pop("probe_token", None)
        return result

    def failure(self, provider, reason, retry_after=30, scope=None, client="", detail=""):
        if reason not in HOLD_STATES:
            raise HealthError("invalid provider incident")
        if scope is not None:
            # Only a client's request shape is route-scoped. Authentication,
            # rate-limit, and transport evidence always describes the provider.
            if reason != "incompatible" or not isinstance(scope, str) or not scope:
                raise HealthError("only client incompatibility can be route-scoped")
            with self.locked(provider) as state:
                incidents = state.setdefault(SCOPED_INCIDENTS, {})
                existing = incidents.get(scope)
                if isinstance(existing, dict):
                    existing["failures"] = int(existing.get("failures", 1)) + 1
                    existing["last_at"] = time.time()
                else:
                    incidents[scope] = scoped_incident(client, detail)
            return
        with self.locked(provider) as state:
            # A later transient failure must not erase an authentication hold.
            if state.get("state") in {"authentication", "incompatible"}:
                return
            count = state.get("probe_count", 0)
            failures = state.get("failures", 0) + 1
            delay = max(
                30,
                min(3600, float(retry_after or 30)),
                min(900, 30 * 2 ** min(failures - 1, 5)),
            )
            clear_preserving_scoped(state)
            state.update(
                state=reason,
                failures=failures,
                probe_count=count,
                at=time.time(),
                retry_at=time.time() + delay,
                incident=uuid.uuid4().hex,
            )

    def claim_probe(self, provider, repair=False):
        with self.locked(provider) as state:
            if state.get("probe_until", 0) > time.time() or (
                not repair and state.get("retry_at", 0) > time.time()
            ):
                return None
            if state.get("state") in {"authentication", "incompatible"} and not repair:
                return None
            if state.get("probe_count", 0) >= 3 and not repair:
                return None
            token = uuid.uuid4().hex
            state.update(
                probe_token=token,
                probe_until=time.time() + 60,
                probe_count=1 if repair else state.get("probe_count", 0) + 1,
                probe_repair=bool(repair),
            )
            return token

    def complete_probe(
        self, provider, token, outcome, route="", retry_after=30, scoped=False
    ):
        if not token or outcome not in {"healthy", *HOLD_STATES}:
            return False
        with self.locked(provider) as state:
            if state.get("probe_token") != token:
                return False
            if scoped:
                # A failed installed-client check says nothing about the
                # provider or other routes: keep provider state, hold the route.
                if outcome != "incompatible" or not route:
                    return False
                for key in ("probe_token", "probe_until", "probe_repair"):
                    state.pop(key, None)
                state.setdefault(SCOPED_INCIDENTS, {})[route] = scoped_incident(
                    "installed-client-check",
                    "installed client failed the bounded compatibility probe",
                )
                return True
            if outcome == "healthy":
                routes = (
                    set(state.get("routes", []))
                    if state.get("state") == "healthy"
                    else set()
                )
                routes.add(route)
                incidents = state.get(SCOPED_INCIDENTS) or {}
                if state.get("probe_repair") and route in incidents:
                    # Only an explicit after-repair probe of this exact route
                    # clears its client incompatibility.
                    del incidents[route]
                clear_preserving_scoped(state)
                state.update(
                    state="healthy",
                    routes=sorted(routes),
                    valid_until=time.time() + 300,
                )
            else:
                count = state.get("probe_count", 1)
                clear_preserving_scoped(state)
                state.update(
                    state=outcome,
                    at=time.time(),
                    retry_at=time.time() + max(30, min(3600, float(retry_after or 30))),
                    probe_count=count,
                    incident=uuid.uuid4().hex,
                )
            return True


def route_identity(route):
    # No credentials or credential digests are persisted.
    identity = {k: v for k, v in route.items() if k != "role"}
    if route.get("execution") == "desktop":
        client = DESKTOP_CLIENTS.get(route.get("provider"))
        executable = shutil.which(client) if client else None
        identity["executable"] = executable
        if executable:
            resolved = Path(executable).resolve()
            stat = resolved.stat()
            identity["client_revision"] = [
                str(resolved),
                stat.st_size,
                stat.st_mtime_ns,
            ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def validate_native_command(command, route):
    """Enforce the resolved model policy and prevent routing overrides."""
    command = list(command)
    expected = DESKTOP_CLIENTS.get(route.get("provider"))
    if route.get("execution") != "desktop" or not expected:
        raise HealthError(
            "native launch requires a supported desktop worker route"
        )
    if not command or Path(command[0]).name != expected:
        raise HealthError(
            "worker executable does not match resolved sprint-worker provider"
        )
    if expected == "codex" and (len(command) < 2 or command[1] != "exec"):
        raise HealthError("native Codex requires direct codex exec")
    resolved = shutil.which(command[0])
    expected_path = shutil.which(expected)
    if (
        not resolved
        or not expected_path
        or Path(resolved).resolve() != Path(expected_path).resolve()
    ):
        raise HealthError(
            "worker executable differs from the installed client checked on PATH"
        )
    command[0] = resolved
    values = []
    efforts = []
    for i, arg in enumerate(command[1:], 1):
        if arg == "--":
            break
        if (
            expected == "codex"
            and arg.startswith("-p")
            or arg.startswith("--local-provider=")
        ):
            raise HealthError(
                "worker provider/profile override is not controller-authorized"
            )
        if arg in {"--model", "-m"}:
            values.append(command[i + 1] if i + 1 < len(command) else "")
        elif arg.startswith("--model="):
            values.append(arg.split("=", 1)[1])
        elif arg.startswith("-m") and arg != "-m":
            values.append(arg[2:])
        if arg == "--effort":
            efforts.append(command[i + 1] if i + 1 < len(command) else "")
        elif arg.startswith("--effort="):
            efforts.append(arg.split("=", 1)[1])
        if arg in {
            "--profile",
            "-p" if expected == "codex" else "--settings",
            "--setting-sources",
            "--agent",
            "--agents",
            "--fallback-model",
            "--oss",
            "--local-provider",
            "--ignore-user-config",
            "--plugin-dir",
        } or any(
            arg.startswith(x + "=")
            for x in [
                "--profile",
                "--settings",
                "--setting-sources",
                "--agent",
                "--agents",
                "--fallback-model",
                "--plugin-dir",
            ]
        ):
            raise HealthError(
                "worker settings/profile override is not controller-authorized"
            )
        value = ""
        if arg in {"-c", "--config"}:
            value = command[i + 1] if i + 1 < len(command) else ""
        elif arg.startswith("--config="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:]
        if value:
            key = value.split("=", 1)[0].strip()
            if key not in {"sandbox_mode", "approval_policy"}:
                raise HealthError("worker config override is not controller-authorized")
    if route.get("model"):
        if values != [route["model"]]:
            raise HealthError("worker must specify the resolved model exactly once")
    elif values:
        raise HealthError("model-less desktop route must use the client's default model")
    effort = route.get("effort")
    if efforts and efforts != [effort]:
        raise HealthError("worker effort differs from resolved route")
    if effort and not efforts:
        if expected == "claude":
            command.extend(["--effort", effort])
        else:
            command[2:2] = ["-c", "model_reasoning_effort=" + json.dumps(effort)]
    return command


def probe(root, config, role="sprint-worker", repair=False, transport=None):
    """Verify one route without a ticket reservation or paid generation."""
    from context_pipeline import llm_route_from_config
    from api_agent import (
        AgentError,
        HttpTransport,
        ProviderHTTPError,
        load_orchestration_env,
    )

    route = llm_route_from_config(Path(config), role)
    provider = route["provider"]
    identity = route_identity(route)
    health = ProviderHealth(root)
    if model_less_desktop_route(route):
        return desktop_subscription_status(route)
    if not route.get("model"):
        raise HealthError("configured route has no explicit model")
    existing = health.status(provider, identity)
    if not repair and (
        existing["state"] == "healthy" or existing.get("scope") == "route"
    ):
        # A route-scoped client incompatibility needs actual repair and an
        # explicit after-repair probe, exactly like a provider-wide one.
        return existing
    token = health.claim_probe(provider, repair=repair)
    if token is None:
        return health.status(provider, identity)
    transport = transport or HttpTransport(timeout=15)
    retry_after = 30
    client_failed = False
    try:
        load_orchestration_env(Path(config))
        if route["execution"] == "desktop":
            from runtime_smoke import check

            try:
                check(provider, route["model"])
            except Exception as exc:
                client_failed = True
                raise HealthError("installed client is incompatible") from exc
        if provider == "anthropic":
            reply = transport.request(
                provider,
                "/messages/count_tokens",
                {
                    "model": route["model"],
                    "messages": [{"role": "user", "content": "health"}],
                },
            )
        elif provider == "openai":
            reply = transport.request(
                provider,
                "responses/input_tokens",
                {"model": route["model"], "input": "health"},
            )
        else:
            raise HealthError(
                "this provider requires a supported health-probe adapter before admission"
            )
        if (
            not isinstance(reply.get("input_tokens"), int)
            or isinstance(reply["input_tokens"], bool)
            or reply["input_tokens"] < 1
        ):
            raise HealthError("health probe returned invalid token-count evidence")
        outcome = "healthy"
    except ProviderHTTPError as exc:
        retry_after = exc.retry_after_seconds or 30
        outcome = (
            "authentication"
            if exc.status in {401, 403}
            else "rate_limited"
            if exc.status in {429, 529}
            else "incompatible"
        )
    except HealthError:
        outcome = "incompatible"
    except AgentError as exc:
        outcome = "authentication" if "API_KEY is required" in str(exc) else "transport"
    except Exception:
        outcome = "transport"
    health.complete_probe(
        provider,
        token,
        outcome,
        identity,
        retry_after,
        scoped=client_failed and outcome == "incompatible",
    )
    return health.status(provider, identity)


class ProviderTransport:
    """Apply shared incident admission to API roles as well as native workers."""

    def __init__(self, root, transport):
        self.health = ProviderHealth(root)
        self.transport = transport
        self.retry_owner = None
        self.retry_incident = None

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def request(self, provider, path, payload, **kwargs):
        from api_agent import ProviderAdmissionError, ProviderHTTPError

        state = self.health.status(provider)
        own_retry = (
            state["state"] == "rate_limited"
            and kwargs.get("idempotency_key") is not None
            and kwargs.get("idempotency_key") == self.retry_owner
            and state.get("incident") == self.retry_incident
        )
        # Provider-wide on purpose: a route-scoped client incompatibility from a
        # native gateway never holds API roles.
        if state["state"] in HOLD_STATES and not own_retry:
            raise ProviderAdmissionError("provider admission held: " + state["state"])
        try:
            return self.transport.request(provider, path, payload, **kwargs)
        except ProviderHTTPError as exc:
            if exc.status in {401, 403}:
                self.health.failure(provider, "authentication")
            elif exc.status in {429, 529}:
                self.health.failure(provider, "rate_limited", exc.retry_after_seconds)
                self.retry_owner = kwargs.get("idempotency_key")
                self.retry_incident = self.health.status(provider).get("incident")
            raise
