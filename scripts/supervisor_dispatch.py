#!/usr/bin/env python3
"""Deterministic worker dispatch and terminal-result handling for Orka 2.

The supervisor is the only caller of this module.  Models may produce a result
envelope, but they never choose admission, identity, or lifecycle authority.
Those bindings come from the controller reservation and launch evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from context_pipeline import llm_route_from_config
from controller_runtime import execute_request, supervisor_request
from breaker_runtime import BreakerRuntime, BreakerRuntimeError
from github_progress import (
    ProgressError,
    number_from_evidence,
    repository as github_repository,
)
from provider_health import DESKTOP_CLIENTS, ProviderHealth, route_identity
from phase_execution import PhaseExecutionError, PhaseExecutionRuntime
from execution_backend import (
    BackendContractError,
    BackendCoordinator,
    TransactionalBackendState,
    _digest as backend_digest,
    _launch_receipt_digest,
)
from execution_backend_contract import load_contract as load_backend_contract
from phase_worker_adapter import (
    AdapterProtocolError,
    capability_offer,
    classify_route_failure,
    negotiate,
)
from phase_worker_contract import ContractError as PhaseContractError
from supervisor_contract import (
    ContractError,
    load_json as load_contract,
    validate as validate_contract,
)
from supervisor_planning import canonical_digest, parse_json_output
from supervisor_admission import (
    AdmissionError,
    CLAIM_SCHEMA,
    automatic_claims,
    claim_set_digest,
    conflicts,
    migrate_legacy_active_jobs,
    normalize_claims,
    release_claims,
)
from runtime_state import canonical_config_path


class DispatchError(RuntimeError):
    pass


class StaleResultError(DispatchError):
    """A result is well-formed enough to prove it belongs to another execution."""


CONTROLLER = Path(__file__).with_name("sprint-controller.py")
API_AGENT = Path(__file__).with_name("api_agent.py")
CONTEXT_PIPELINE = Path(__file__).with_name("context_pipeline.py")
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PHASE_CONTRACT = PLUGIN_ROOT / "contracts/phase-worker-protocol-v1.json"
EXECUTION_BACKEND_CONTRACT = PLUGIN_ROOT / "contracts/execution-backend-v1.json"
RESULT_SCHEMA = "orka.worker-terminal-result/v1"
TERMINAL_OUTCOMES = {
    "completed",
    "needs_repair",
    "recoverable",
    "needs_decomposition",
    "external_blocked",
    "operator_decision",
    "blocked",
    "timeout_with_progress",
    "timeout_without_progress",
    "cancelled_attempt",
}
CONTROLLER_OUTCOME = {
    "completed": "completed",
    "needs_repair": "needs_repair",
    "recoverable": "recoverable",
    "needs_decomposition": "needs_decomposition",
    "external_blocked": "external_blocked",
    "operator_decision": "operator_decision",
    "blocked": "blocked",
    "timeout_with_progress": "recoverable",
    "timeout_without_progress": "recoverable",
    "cancelled_attempt": "recoverable",
    "malformed_result": "recoverable",
}
KEY = re.compile(r"[A-Z][A-Z0-9_]*-[0-9]+\Z")


def _private_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _regular_private_file(
    path: Path, label: str, *, maximum_bytes: int = 1024 * 1024
) -> bytes:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise DispatchError(f"{label} is not a regular file")
        if metadata.st_size > maximum_bytes:
            raise DispatchError(f"{label} exceeds {maximum_bytes} bytes")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise DispatchError(f"{label} is missing") from exc
    except OSError as exc:
        raise DispatchError(f"cannot read {label}: {exc}") from exc


def _json_object(raw: str) -> dict[str, Any]:
    candidates = [raw.strip()]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise DispatchError("worker terminal result is not one JSON object")


def extract_terminal_result(path: Path) -> tuple[dict[str, Any], str]:
    """Extract one model result while preserving a digest of exact evidence."""
    raw = _regular_private_file(path, "worker result")
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="strict")
    try:
        value = _json_object(text)
    except DispatchError:
        value = {}
    for field in ("output_text", "result"):
        if isinstance(value.get(field), str):
            return _json_object(value[field]), digest
    if value.get("schema_version") == RESULT_SCHEMA:
        return value, digest
    # Native JSONL clients retain the final assistant item.  Only extract a
    # single explicit JSON object; never infer an outcome from prose.
    messages: list[str] = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        nested = item.get("item") if isinstance(item.get("item"), dict) else item
        if nested.get("type") in {"agent_message", "assistant_message"}:
            candidate = nested.get("text") or nested.get("content")
            if isinstance(candidate, str):
                messages.append(candidate)
    if len(messages) == 1:
        return _json_object(messages[0]), digest
    raise DispatchError("worker output contains no unique terminal result")


def validate_terminal_result(
    value: dict[str, Any],
    *,
    ticket: str,
    sprint: str,
    attempt_token: str,
    invocation_id: str,
) -> dict[str, Any]:
    expected = {
        "schema_version": RESULT_SCHEMA,
        "ticket": ticket,
        "sprint": sprint,
        "attempt_token": attempt_token,
        "invocation_id": invocation_id,
    }
    mismatches = [
        name
        for name, expected_value in expected.items()
        if value.get(name) != expected_value
    ]
    if mismatches:
        raise StaleResultError(
            "worker result binding mismatch: " + ", ".join(sorted(mismatches))
        )
    outcome = value.get("outcome")
    if outcome not in TERMINAL_OUTCOMES:
        raise DispatchError("worker result has an unsupported outcome")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
        raise DispatchError(
            "worker result summary must contain 1 through 4000 characters"
        )
    for field in ("branch", "worktree", "pr"):
        if not isinstance(value.get(field, ""), str):
            raise DispatchError(f"worker result {field} must be a string")
    if outcome == "completed" and (not value.get("branch") or not value.get("pr")):
        raise DispatchError("completed worker result requires branch and PR identity")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise DispatchError("worker result evidence must be an object")
    return {
        "schema_version": RESULT_SCHEMA,
        "ticket": ticket,
        "sprint": sprint,
        "attempt_token": attempt_token,
        "invocation_id": invocation_id,
        "outcome": outcome,
        "summary": summary.strip(),
        "branch": value.get("branch", "").strip(),
        "worktree": value.get("worktree", "").strip(),
        "pr": value.get("pr", "").strip(),
        "evidence": evidence,
    }


def result_prompt(
    ticket: str,
    sprint: str,
    attempt_token: str,
    result_path: Path,
    *,
    phase_envelope: dict[str, Any] | None = None,
) -> str:
    example = {
        "schema_version": RESULT_SCHEMA,
        "ticket": ticket,
        "sprint": sprint,
        "attempt_token": attempt_token,
        "invocation_id": "COPY_ORCHESTRATOR_INVOCATION_ID",
        "outcome": "completed|needs_repair|recoverable|needs_decomposition|external_blocked|operator_decision|blocked|timeout_with_progress|timeout_without_progress|cancelled_attempt",
        "summary": "bounded factual summary",
        "branch": "branch or empty string",
        "worktree": "absolute managed worktree or empty string",
        "pr": "PR number/URL or empty string",
        "evidence": {},
    }
    prompt = (
        f"Run Jira ticket {ticket} from sprint {sprint} through the installed Orka "
        "orchestrate-ticket workflow. Re-fetch authoritative Jira and GitHub state; "
        "preserve every review, security, CI, budget, and merge gate. Do not run a "
        "second sprint captain. At terminal state, write exactly one JSON object to "
        f"{result_path}. The supervisor will replace the invocation placeholder in "
        "this pre-launch template with its authenticated execution identity before "
        "accepting the result. Also return the same JSON object as the final response. "
        "Do not infer success: completed requires an authenticated merged-PR receipt. "
        "Evidence must contain every lifecycle-contract field for the selected outcome "
        "(for example merge_receipt for completed, or pr_identity and "
        "review_ledger_digest for needs_repair). An external_blocked result must "
        "supply external_dependency_receipt as an object with the exact authenticated "
        "Jira dependency keys in dependencies and a bounded receipt string. An "
        "operator_decision result must supply one contract decision_class and a "
        "bounded decision_question. "
        f"Schema template: {json.dumps(example, sort_keys=True)}"
    )
    if phase_envelope is not None:
        prompt += (
            " This invocation is one fresh disposable phase execution. Its immutable "
            "supervisor-owned job envelope is: "
            + json.dumps(phase_envelope, sort_keys=True, separators=(",", ":"))
        )
    return prompt


class ControllerDispatchAdapter:
    """Mutation adapter; every operation remains fenced by sprint-controller."""

    def __init__(
        self,
        repository: Path,
        runtime_directory: Path,
        *,
        supervisor_fence: str = "",
        writer_identity: str = "",
        event_store: Any | None = None,
    ):
        self.repository = repository.resolve()
        self.runtime_directory = runtime_directory.resolve()
        self.config = canonical_config_path(self.repository)
        self.supervisor_fence = supervisor_fence
        self.writer_identity = writer_identity
        self.event_store = event_store
        self.private_root = runtime_directory / "controller-materializations"

    def phase_capability_offer(self, contract: dict[str, Any]) -> dict[str, Any]:
        """Advertise the installed route before reservation or provider work."""

        route = llm_route_from_config(self.config, "sprint-worker")
        if route["execution"] == "desktop":
            client = DESKTOP_CLIENTS.get(route["provider"])
            profile = f"{client}-desktop" if client else ""
        else:
            profile = "api"
        return capability_offer(
            profile,
            adapter_version="orka-phase-adapter/1",
            # This adapter implementation consumes protocol v1. Never echo the
            # supervisor's requested version: mixed-version rollout must fail
            # closed until the installed adapter itself is upgraded.
            protocol_version=1,
        )

    def _run(self, *arguments: str) -> dict[str, Any]:
        if self.event_store is not None:
            try:
                request = supervisor_request(
                    self.repository,
                    arguments,
                    self.supervisor_fence,
                    store=self.event_store,
                )
                response = execute_request(
                    self.repository,
                    request,
                    supervisor_fence=self.supervisor_fence,
                    writer_identity=self.writer_identity,
                    private_root=self.private_root,
                    controller_path=CONTROLLER,
                    store=self.event_store,
                )
                result = subprocess.CompletedProcess(
                    args=list(arguments),
                    returncode=int(response["returncode"]),
                    stdout=str(response.get("stdout") or ""),
                    stderr=str(response.get("stderr") or ""),
                )
                return parse_json_output(result, arguments[0])
            except Exception as exc:
                raise DispatchError(
                    f"cannot run transactional controller {arguments[0]}: {exc}"
                ) from exc
        try:
            result = subprocess.run(
                [sys.executable, str(CONTROLLER), *arguments],
                cwd=self.repository,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DispatchError(f"cannot run controller {arguments[0]}: {exc}") from exc
        try:
            return parse_json_output(result, arguments[0])
        except Exception as exc:
            raise DispatchError(str(exc)) from exc

    def reserve(self, sprint: str, ticket: str, run_ref: str) -> dict[str, Any]:
        return self._run(
            "reserve",
            "--sprint",
            sprint,
            "--ticket",
            ticket,
            "--run-ref",
            run_ref,
            "--role",
            "sprint-worker",
        )

    def attach(self, sprint: str, ticket: str, launch_evidence: str) -> dict[str, Any]:
        return self._run(
            "attach",
            "--sprint",
            sprint,
            "--ticket",
            ticket,
            "--launch-evidence",
            launch_evidence,
        )

    def finish(
        self,
        sprint: str,
        ticket: str,
        result: dict[str, Any],
        *,
        outcome: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "finish",
            "--sprint",
            sprint,
            "--ticket",
            ticket,
            "--outcome",
            outcome or CONTROLLER_OUTCOME[result["outcome"]],
            "--summary",
            summary or result["summary"],
            "--branch",
            result.get("branch", ""),
            "--pr",
            result.get("pr", ""),
            "--attempt-token",
            result["attempt_token"],
        ]
        if result["outcome"] == "operator_decision":
            arguments.extend(
                [
                    "--decision-class",
                    str(result["evidence"]["decision_class"]),
                    "--decision-question",
                    str(result["evidence"]["decision_question"]),
                ]
            )
        elif result["outcome"] == "external_blocked":
            receipt = result["evidence"]["external_dependency_receipt"]
            for dependency in receipt["dependencies"]:
                arguments.extend(["--external-dependency", dependency])
            arguments.extend(
                [
                    "--external-dependency-receipt",
                    str(receipt["receipt"]),
                ]
            )
        return self._run(*arguments)

    def requeue(self, sprint: str, job: dict[str, Any], reason: str) -> dict[str, Any]:
        return self._run(
            "requeue",
            "--sprint",
            sprint,
            "--ticket",
            job["ticket"],
            "--reason",
            reason,
            "--attempt-token",
            job["attempt_token"],
        )

    def verify_completion(self, result: dict[str, Any]) -> dict[str, Any]:
        try:
            host, name, _repository_id = github_repository(self.repository)
            pr_number = str(number_from_evidence(result["pr"], host, name))
        except ProgressError as exc:
            raise DispatchError("completed result has an invalid PR identity") from exc
        try:
            observed = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_number,
                    "--json",
                    "state,mergedAt,mergeCommit,headRefName,url,title,body",
                ],
                cwd=self.repository,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            value = json.loads(observed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise DispatchError(f"cannot verify completed PR: {exc}") from exc
        if (
            not isinstance(value, dict)
            or value.get("state") != "MERGED"
            or not value.get("mergedAt")
            or value.get("headRefName") != result["branch"]
            or not isinstance(value.get("mergeCommit"), dict)
            or not value["mergeCommit"].get("oid")
            or result["ticket"]
            not in "\n".join(
                str(value.get(field) or "")
                for field in ("headRefName", "title", "body")
            ).upper()
        ):
            raise DispatchError(
                "completed result is not bound to an authenticated merged PR"
            )
        return {
            "url": value.get("url"),
            "merged_at": value["mergedAt"],
            "merge_commit": value["mergeCommit"]["oid"],
            "head_ref": value["headRefName"],
            "receipt_digest": canonical_digest(value),
        }

    def verify_work_identity(self, result: dict[str, Any]) -> dict[str, Any] | None:
        raw_worktree = result.get("worktree") or ""
        if not raw_worktree:
            return None
        if not result.get("branch"):
            raise DispatchError("reported worktree has no branch binding")
        try:
            observed = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repository,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise DispatchError(f"cannot verify reported worktree: {exc}") from exc
        expected_path = Path(raw_worktree).resolve()
        expected_branch = f"refs/heads/{result['branch']}"
        match: dict[str, str] | None = None
        for record in observed.strip().split("\n\n") if observed.strip() else []:
            fields: dict[str, str] = {}
            for line in record.splitlines():
                name, _, value = line.partition(" ")
                fields[name] = value
            if Path(fields.get("worktree") or "").resolve() == expected_path:
                match = fields
                break
        if match is None or match.get("branch") != expected_branch:
            raise DispatchError("reported worktree is not bound to the reported branch")
        receipt = {
            "worktree": str(expected_path),
            "branch": result["branch"],
            "head": match.get("HEAD"),
        }
        receipt["receipt_digest"] = canonical_digest(receipt)
        return receipt

    def launch(
        self,
        sprint: str,
        ticket: str,
        reservation: dict[str, Any],
        prompt_path: Path,
        output_path: Path,
        result_path: Path,
    ) -> dict[str, Any]:
        route = llm_route_from_config(self.config, "sprint-worker")
        input_path = prompt_path
        if route["execution"] == "desktop":
            client = DESKTOP_CLIENTS.get(route["provider"])
            if client == "codex":
                command = [
                    client,
                    "exec",
                    "--ephemeral",
                    "--json",
                    "--sandbox",
                    "danger-full-access",
                    "--output-last-message",
                    str(result_path),
                    "-",
                ]
                if route.get("model"):
                    command[2:2] = ["--model", route["model"]]
            elif client == "claude":
                command = [
                    client,
                    "--print",
                    "--output-format",
                    "json",
                    "--permission-mode",
                    "bypassPermissions",
                ]
                if route.get("model"):
                    command.extend(["--model", route["model"]])
            else:
                raise DispatchError("unsupported desktop sprint-worker route")
        else:
            request_path = prompt_path.with_suffix(".request.json")
            repo_map = prompt_path.with_suffix(".repo-map.txt")
            try:
                tracked = subprocess.run(
                    ["git", "ls-files"],
                    cwd=self.repository,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout
                _private_write(repo_map, tracked)
                rules = [PLUGIN_ROOT / "skills/orchestrate-ticket/SKILL.md"]
                repository_rules = self.repository / "AGENTS.md"
                if repository_rules.is_file():
                    rules.append(repository_rules)
                payload_command = [
                    sys.executable,
                    str(CONTEXT_PIPELINE),
                    "payload",
                    "--config",
                    str(self.config),
                    "--role",
                    "sprint-worker",
                    "--role-file",
                    str(PLUGIN_ROOT / "agents/orchestration-implementer.md"),
                    "--repo-map",
                    str(repo_map),
                    "--ticket",
                    str(prompt_path),
                    "--mode",
                    "implement",
                    "--execution",
                    "on-demand",
                ]
                for rules_file in rules:
                    payload_command.extend(["--rules-file", str(rules_file)])
                payload = subprocess.run(
                    payload_command,
                    cwd=self.repository,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                ).stdout
                _private_write(request_path, payload)
            except (OSError, subprocess.SubprocessError) as exc:
                raise DispatchError(
                    f"cannot assemble API sprint-worker request: {exc}"
                ) from exc
            input_path = request_path
            command = [
                sys.executable,
                str(API_AGENT),
                "run",
                "--request",
                str(request_path),
                "--config",
                str(self.config),
                "--role",
                "sprint-worker",
                "--repo",
                str(self.repository),
                "--ticket",
                ticket,
                "--sprint",
                sprint,
                "--run-id",
                reservation["run_ref"],
                "--result",
                str(result_path),
                "--attempt-capability",
                reservation["attempt_capability"],
                "--worker-ref",
                reservation["run_ref"],
            ]
        return self._run(
            "launch-local",
            "--sprint",
            sprint,
            "--ticket",
            ticket,
            "--attach-capability",
            reservation["attach_capability"],
            "--output",
            str(output_path.relative_to(self.repository)),
            "--stdin-file",
            str(input_path.relative_to(self.repository)),
            "--bind-invocation-placeholder",
            "--",
            *command,
        )


class CurrentRouteExecutionBackend:
    """Production adapter for today's Codex, Claude, and API host paths.

    Provider command construction remains in ``ControllerDispatchAdapter``;
    the supervisor sees only the versioned execution-backend lifecycle.
    """

    def __init__(
        self, adapter: ControllerDispatchAdapter, phase_contract: dict[str, Any]
    ) -> None:
        self.adapter = adapter
        self.phase_contract = phase_contract
        self.profile = ""
        self.material: dict[str, Any] = {}

    def bind_launch(
        self,
        *,
        sprint: str,
        ticket: str,
        reservation: dict[str, Any],
        prompt_path: Path,
        output_path: Path,
        result_path: Path,
    ) -> None:
        self.material = {
            "sprint": sprint,
            "ticket": ticket,
            "reservation": reservation,
            "prompt_path": prompt_path,
            "output_path": output_path,
            "result_path": result_path,
        }

    def _profile(self) -> str:
        offer = self.adapter.phase_capability_offer(self.phase_contract)
        profile = str(offer.get("adapter") or "")
        if profile not in {"codex-desktop", "claude-desktop", "api"}:
            raise BackendContractError(
                "current route has no supported execution profile"
            )
        self.profile = profile
        return profile

    def discover(self) -> dict[str, Any]:
        profile = self._profile()
        desktop = profile != "api"
        return {
            "backend_id": f"orka-current-route/{profile}",
            "backend_version": "1",
            "protocol_version": 1,
            "test_only": False,
            "lifecycle": [
                "launch",
                "attach",
                "heartbeat",
                "progress",
                "cancel",
                "inspect",
                "terminal",
            ],
            "containment": {
                "process": "shared-host" if desktop else "isolated-process",
                "filesystem": "worktree",
                "network": "unrestricted",
                "credentials": "ambient",
                "process_control": "inspect-cancel",
            },
            "provider_capabilities": [
                "desktop-subscription" if desktop else "provider-receipts"
            ],
        }

    def launch(
        self, binding: dict[str, str], envelope: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.material:
            raise BackendContractError("execution backend launch material is missing")
        launch = self.adapter.launch(
            self.material["sprint"],
            self.material["ticket"],
            self.material["reservation"],
            self.material["prompt_path"],
            self.material["output_path"],
            self.material["result_path"],
        )
        launch_evidence = str(launch.get("launch_evidence") or "")
        if not launch_evidence:
            raise BackendContractError("current route omitted launch evidence")
        attached = self.adapter.attach(
            self.material["sprint"], self.material["ticket"], launch_evidence
        )
        identity = attached.get("worker_identity")
        if not isinstance(identity, dict) or not identity.get("invocation_id"):
            raise BackendContractError(
                "current route omitted an execution-unit identity"
            )
        profile = self.profile or self._profile()
        backend_instance_id = f"orka-current-route/{profile}/1"
        # The launch token is a one-use attach bearer and is consumed above.
        # Persist only an opaque digest as the backend handle.
        backend_handle = "launch:" + backend_digest(launch_evidence)
        process_identity = backend_digest(identity)
        envelope_digest = backend_digest(envelope)
        return {
            "status": "launched",
            "binding": dict(binding),
            "backend_instance_id": backend_instance_id,
            "backend_handle": backend_handle,
            "process_identity": process_identity,
            "envelope_digest": envelope_digest,
            "launch_receipt": _launch_receipt_digest(
                binding,
                backend_instance_id=backend_instance_id,
                backend_handle=backend_handle,
                process_identity=process_identity,
                envelope_digest=envelope_digest,
            ),
            "backend_metadata": {
                "worker_identity": identity,
            },
        }

    @staticmethod
    def _event(
        binding: dict[str, str], launch: dict[str, Any], status: str, **extra: Any
    ) -> dict[str, Any]:
        value = {
            "status": status,
            "binding": dict(binding),
            "backend_handle": launch["backend_handle"],
            **extra,
        }
        value["evidence_receipt"] = backend_digest(value)
        return value

    @staticmethod
    def _identity(launch: dict[str, Any]) -> dict[str, Any]:
        metadata = launch.get("backend_metadata") or {}
        identity = metadata.get("worker_identity")
        if not isinstance(identity, dict) or not identity.get("invocation_id"):
            raise BackendContractError("launch receipt has no worker identity")
        return identity

    def attach(self, binding: dict[str, str], launch: dict[str, Any]) -> dict[str, Any]:
        return self._event(
            binding,
            launch,
            "attached",
            worker_identity=self._identity(launch),
        )

    def heartbeat(
        self, binding: dict[str, str], launch: dict[str, Any]
    ) -> dict[str, Any]:
        observation = self.inspect(binding, launch)
        return self._event(
            binding,
            launch,
            "heartbeat",
            observed_at=time.time(),
            observed_status=observation["status"],
            observation_receipt=observation["evidence_receipt"],
        )

    def progress(
        self, binding: dict[str, str], launch: dict[str, Any]
    ) -> dict[str, Any]:
        observation = self.inspect(binding, launch)
        return self._event(
            binding,
            launch,
            "progress",
            observed_at=time.time(),
            observed_status=observation["status"],
            observation_receipt=observation["evidence_receipt"],
        )

    def _terminal_result(
        self, binding: dict[str, str], launch: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.material or not Path(self.material["result_path"]).is_file():
            raise BackendContractError("execution has no structured terminal result")
        result, _digest_value = extract_terminal_result(self.material["result_path"])
        identity = self._identity(launch)
        validated = validate_terminal_result(
            result,
            ticket=self.material["ticket"],
            sprint=self.material["sprint"],
            attempt_token=binding["attempt_token"],
            invocation_id=str(identity["invocation_id"]),
        )
        ticket_id = str(self.material["ticket"])
        terminal = {
            **binding,
            "ticket_id": ticket_id,
            "kind": "terminal",
            "protocol_version": 1,
            "outcome": validated["outcome"],
            "summary": validated["summary"],
            "artifacts": {
                key: validated[key]
                for key in ("branch", "pr")
                if isinstance(validated.get(key), str) and validated[key]
            },
            "evidence": dict(validated.get("evidence") or {}),
        }
        return validated, terminal

    def terminal(
        self, binding: dict[str, str], launch: dict[str, Any]
    ) -> dict[str, Any]:
        validated, terminal = self._terminal_result(binding, launch)
        return self._event(
            binding,
            launch,
            "terminal",
            terminal_envelope=terminal,
            terminal_result=validated,
        )

    def inspect(
        self, binding: dict[str, str], launch: dict[str, Any]
    ) -> dict[str, Any]:
        identity = self._identity(launch)
        tombstone_path = Path(str(identity.get("tombstone_path") or ""))
        if not tombstone_path.is_file():
            # A missing terminal record does not mechanically prove that the
            # original process is still live. Preserve uncertainty instead of
            # inferring liveness from the absence of absence evidence.
            return self._event(binding, launch, "unknown")
        try:
            tombstone = json.loads(
                _regular_private_file(tombstone_path, "execution tombstone")
            )
        except (DispatchError, json.JSONDecodeError):
            return self._event(binding, launch, "unknown")
        if (
            not isinstance(tombstone, dict)
            or tombstone.get("phase") != "terminal"
            or tombstone.get("invocation_id") != identity.get("invocation_id")
        ):
            return self._event(binding, launch, "unknown")
        if self.material and Path(self.material["result_path"]).is_file():
            try:
                return self.terminal(binding, launch)
            except (BackendContractError, DispatchError, StaleResultError):
                # The output is invalid, but the exact host tombstone still
                # proves that the original execution is absent. The
                # supervisor will classify the malformed result separately.
                pass
        return self._event(
            binding,
            launch,
            "absent",
            absence_receipt={
                "backend_instance_id": launch["backend_instance_id"],
                "backend_handle": launch["backend_handle"],
                "original_process_identity": launch["process_identity"],
                "inspection_receipt": backend_digest(tombstone),
            },
        )

    def cancel(
        self,
        binding: dict[str, str],
        launch: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        # The current host primitive has no exact acknowledgement handshake.
        # Report a request only; the supervisor must retain or fence the lane.
        return self._event(
            binding,
            launch,
            "requested",
            cancellation_id=request["cancellation_id"],
        )

    def verify_cancellation_ack(self, *_args: Any) -> bool:
        return False


class SupervisorDispatcher:
    def __init__(
        self,
        repository: Path,
        runtime_directory: Path,
        contract_path: Path,
        adapter: ControllerDispatchAdapter | None = None,
        *,
        retry_delay_seconds: float = 30.0,
        breaker_contract_path: Path | None = None,
        supervisor_fence: str | None = None,
        event_store: Any | None = None,
        writer_identity: str = "",
        repository_id: str | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.runtime_directory = runtime_directory.resolve()
        self.adapter = adapter or ControllerDispatchAdapter(
            repository,
            runtime_directory,
            supervisor_fence=supervisor_fence or "",
            writer_identity=writer_identity,
            event_store=event_store,
        )
        if retry_delay_seconds < 5 or retry_delay_seconds > 3600:
            raise DispatchError("retry delay must be from 5 through 3600 seconds")
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.last_errors: list[dict[str, str]] = []
        self.last_skips: list[dict[str, Any]] = []
        try:
            self.contract = load_contract(contract_path)
            validate_contract(self.contract, CONTROLLER)
        except ContractError as exc:
            raise DispatchError(str(exc)) from exc
        try:
            self.breakers = BreakerRuntime(
                breaker_contract_path
                or PLUGIN_ROOT / "contracts/breaker-classification-v1.json",
                CONTROLLER,
            )
        except BreakerRuntimeError as exc:
            raise DispatchError(str(exc)) from exc
        repository_id = (
            repository_id
            or "repository:"
            + hashlib.sha256(str(self.repository).encode("utf-8")).hexdigest()
        )
        try:
            self.phase_runtime = PhaseExecutionRuntime(
                PHASE_CONTRACT,
                repository_id=repository_id,
                supervisor_fence=supervisor_fence
                or "legacy-supervisor:"
                + hashlib.sha256(str(self.runtime_directory).encode()).hexdigest(),
            )
        except (PhaseExecutionError, PhaseContractError) as exc:
            raise DispatchError(str(exc)) from exc
        try:
            self.execution_backend_contract = load_backend_contract(
                EXECUTION_BACKEND_CONTRACT
            )
        except Exception as exc:
            raise DispatchError(
                f"cannot load execution-backend contract: {exc}"
            ) from exc
        self.backend_state = (
            TransactionalBackendState(event_store, writer_identity=writer_identity)
            if event_store is not None and writer_identity
            else None
        )

    def _execution_backend(
        self,
        *,
        sprint: str,
        ticket: str,
        reservation: dict[str, Any] | None,
        paths: dict[str, Path],
    ) -> tuple[CurrentRouteExecutionBackend, BackendCoordinator]:
        backend = CurrentRouteExecutionBackend(
            self.adapter, self.phase_runtime.contract
        )
        if reservation is not None:
            backend.bind_launch(
                sprint=sprint,
                ticket=ticket,
                reservation=reservation,
                prompt_path=paths["prompt"],
                output_path=paths["output"],
                result_path=paths["result"],
            )
        coordinator = BackendCoordinator(
            self.execution_backend_contract,
            backend,
            state_store=self.backend_state,
        )
        try:
            coordinator.negotiate(
                required={
                    "lifecycle": [
                        "launch",
                        "attach",
                        "heartbeat",
                        "progress",
                        "cancel",
                        "inspect",
                        "terminal",
                    ],
                    "containment": {"filesystem": "worktree"},
                }
            )
        except BackendContractError as exc:
            raise DispatchError(f"execution backend is incompatible: {exc}") from exc
        return backend, coordinator

    def _restore_execution_backend(
        self, job: dict[str, Any]
    ) -> tuple[CurrentRouteExecutionBackend, BackendCoordinator, dict[str, str]]:
        state = job.get("phase_execution")
        backend_state = job.get("execution_backend") or {}
        if not isinstance(state, dict) or not isinstance(state.get("executions"), list):
            raise DispatchError("job has no disposable phase execution")
        executions = state["executions"]
        if not executions:
            raise DispatchError("job has no execution-backend binding")
        record = executions[-1]
        identity = record.get("identity")
        envelope = record.get("job")
        launch_receipt = backend_state.get("launch_receipt")
        if not isinstance(identity, dict) or not isinstance(envelope, dict):
            raise DispatchError("job execution-backend identity is malformed")
        paths = {key: Path(value) for key, value in (job.get("paths") or {}).items()}
        backend, coordinator = self._execution_backend(
            sprint=str(job["sprint"]),
            ticket=str(job["ticket"]),
            reservation={},
            paths=paths,
        )
        try:
            coordinator.restore(identity, envelope, launch_receipt)
        except BackendContractError as exc:
            raise DispatchError(f"execution backend restore failed: {exc}") from exc
        return backend, coordinator, identity

    def _legacy_phase_execution(self, job: dict[str, Any]) -> bool:
        """Recognize only the adjacent pre-backend in-flight job shape.

        New jobs always contain ``execution_backend`` before reservation is
        returned to the supervisor. Its absence, together with an attached v1
        phase execution, is the bounded 1.8.33 compatibility boundary. No
        backend receipt is synthesized for this path.
        """

        if "execution_backend" in job:
            return False
        state = job.get("phase_execution")
        execution_identity = job.get("execution_identity")
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != "orka.phase-execution-state/v1"
            or state.get("status") != "running"
            or not isinstance(state.get("active"), dict)
            or not isinstance(execution_identity, dict)
            or not execution_identity.get("invocation_id")
        ):
            return False
        active_identity = state["active"].get("identity")
        attachment = state["active"].get("attachment")
        if not isinstance(active_identity, dict) or not isinstance(attachment, dict):
            return False
        attached_phase_identity = attachment.get("identity")
        attached_identity = attachment.get("adapter_execution_identity")
        if (
            attached_phase_identity != active_identity
            or not isinstance(attached_identity, dict)
            or attached_identity != execution_identity
        ):
            return False
        if self.backend_state is not None:
            binding = {
                field: active_identity.get(field)
                for field in self.execution_backend_contract["identity_fields"]
            }
            if all(isinstance(value, str) and value for value in binding.values()):
                if self.backend_state.load(binding) is not None:
                    return False
        return True

    def _worker_transition(self, outcome: str) -> tuple[str, str]:
        event = (self.contract.get("worker_terminal_results") or {}).get(outcome)
        if not isinstance(event, str) or not event:
            raise DispatchError(f"lifecycle contract has no event for {outcome}")
        matches = [
            transition
            for transition in self.contract.get("transitions", [])
            if transition.get("entity") == "job"
            and transition.get("from") == "running"
            and transition.get("event") == event
        ]
        if len(matches) != 1:
            raise DispatchError(f"lifecycle event {event} is not deterministic")
        return event, str(matches[0]["to"])

    def _terminal_breaker(
        self,
        outcome: str,
        target: str,
        ticket: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            return self.breakers.terminal_record(
                outcome,
                target_state=target,
                ticket=ticket,
                evidence=evidence,
            )
        except BreakerRuntimeError as exc:
            raise DispatchError(str(exc)) from exc

    def _contract_evidence(
        self, event: str, result: dict[str, Any], result_digest: str
    ) -> dict[str, Any]:
        supplied = dict(result.get("evidence") or {})
        supplied.setdefault("attempt_token", result.get("attempt_token"))
        supplied.setdefault("pr_identity", result.get("pr"))
        supplied.setdefault("result_digest", result_digest)
        if event == "worker_external_blocked":
            receipt = supplied.get("external_dependency_receipt")
            if not isinstance(receipt, dict) or set(receipt) != {
                "dependencies",
                "receipt",
            }:
                raise DispatchError(
                    "external dependency receipt must contain dependencies and receipt"
                )
            dependencies = receipt.get("dependencies")
            if (
                not isinstance(dependencies, list)
                or not dependencies
                or any(
                    not isinstance(item, str) or not KEY.fullmatch(item)
                    for item in dependencies
                )
                or len(set(dependencies)) != len(dependencies)
                or not isinstance(receipt.get("receipt"), str)
                or not receipt["receipt"].strip()
                or len(receipt["receipt"]) > 4000
            ):
                raise DispatchError("external dependency receipt is invalid")
        if event == "worker_operator_decision":
            allowed = {
                item.get("class")
                for item in self.contract.get("operator_only_decisions", [])
                if isinstance(item, dict)
            }
            if supplied.get("decision_class") not in allowed:
                raise DispatchError("operator decision class is not allowed")
            question = supplied.get("decision_question")
            if (
                not isinstance(question, str)
                or not question.strip()
                or len(question) > 2000
            ):
                raise DispatchError("operator decision question is invalid")
        definition = (self.contract.get("events") or {}).get(event) or {}
        required = list(definition.get("required_evidence") or [])
        missing = [
            name
            for name in required
            if supplied.get(name) is None or supplied.get(name) == ""
        ]
        if missing:
            raise DispatchError(
                "worker result lacks lifecycle evidence: " + ", ".join(missing)
            )
        return {name: supplied[name] for name in required}

    def _job_paths(self, run_ref: str) -> dict[str, Path]:
        root = self.runtime_directory / "jobs" / run_ref
        return {
            "prompt": root.with_suffix(".prompt.txt"),
            "output": root.with_suffix(".output.jsonl"),
            "result": root.with_suffix(".result.json"),
            "request": root.with_suffix(".request.json"),
        }

    def _route_identity(self) -> str:
        config = getattr(self.adapter, "config", None)
        if not isinstance(config, Path) or not config.is_file():
            return "desktop-default"
        try:
            route = llm_route_from_config(config, "sprint-worker")
        except Exception as exc:
            raise DispatchError(
                f"cannot resolve sprint-worker resource route: {exc}"
            ) from exc
        identity = {
            key: route.get(key)
            for key in ("execution", "provider", "model", "desktop_client")
            if route.get(key)
        }
        return canonical_digest(identity)[:32]

    def _phase_capability_offer(self) -> tuple[dict[str, Any], dict[str, Any]]:
        advertise = getattr(self.adapter, "phase_capability_offer", None)
        if not callable(advertise):
            error = AdapterProtocolError(
                "missing_advertisement",
                "execution adapter does not advertise the phase-worker protocol",
            )
            self._record_protocol_incompatibility(error)
            raise DispatchError(f"phase-worker protocol incompatibility: {error}")
        try:
            offer = advertise(self.phase_runtime.contract)
            receipt = negotiate(self.phase_runtime.contract, offer)
        except (AdapterProtocolError, PhaseExecutionError) as exc:
            protocol_error = (
                exc
                if isinstance(exc, AdapterProtocolError)
                else AdapterProtocolError("malformed_offer", str(exc))
            )
            self._record_protocol_incompatibility(protocol_error)
            raise DispatchError(
                f"phase-worker protocol incompatibility: {protocol_error}"
            ) from exc
        return offer, receipt

    def _record_protocol_incompatibility(self, error: AdapterProtocolError) -> None:
        config = getattr(self.adapter, "config", None)
        if not isinstance(config, Path) or not config.is_file():
            return
        try:
            route = llm_route_from_config(config, "sprint-worker")
            classification = classify_route_failure(error)
            ProviderHealth(self.repository).failure(
                route["provider"],
                "incompatible",
                scope=route_identity(route),
                client="phase-worker-protocol",
                detail=f"{classification['code']}: {classification['detail']}",
                incident_class=classification["class"],
            )
        except Exception:
            # Compatibility rejection itself remains authoritative. Health is
            # diagnostic and must never turn a pre-reservation refusal into an
            # attempted launch.
            return

    def _phase_terminal_envelope(
        self, job: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        state = job.get("phase_execution")
        if not isinstance(state, dict) or not isinstance(state.get("active"), dict):
            raise DispatchError("job has no active disposable phase execution")
        identity = state["active"]["identity"]
        artifacts = {
            key: value
            for key, value in {
                "branch": result.get("branch"),
                "pr": result.get("pr"),
            }.items()
            if isinstance(value, str) and value
        }
        return {
            "kind": "terminal",
            "protocol_version": self.phase_runtime.contract["protocol_version"],
            **identity,
            "outcome": result["outcome"],
            "summary": result["summary"],
            "artifacts": artifacts,
            "evidence": dict(result.get("evidence") or {}),
        }

    def _phase_runtime_for_state(self, state: dict[str, Any]) -> PhaseExecutionRuntime:
        """Use the dispatch's persisted fence after an authenticated takeover."""

        fence = str(state.get("supervisor_fence") or "")
        repository_id = str(state.get("repository_id") or "")
        if (
            fence == self.phase_runtime.supervisor_fence
            and repository_id == self.phase_runtime.repository_id
        ):
            return self.phase_runtime
        try:
            return PhaseExecutionRuntime(
                PHASE_CONTRACT,
                repository_id=repository_id,
                supervisor_fence=fence,
            )
        except PhaseExecutionError as exc:
            raise DispatchError(str(exc)) from exc

    def resource_claims(
        self,
        ticket: str,
        *,
        capacity: int,
        heavy_capacity: int,
        additional: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return automatic_claims(
                self.repository,
                ticket,
                concurrency=capacity,
                heavy_capacity=heavy_capacity,
                route_identity=self._route_identity(),
                additional=additional,
            )
        except AdmissionError as exc:
            raise DispatchError(str(exc)) from exc

    def launch(
        self,
        sprint: str,
        ticket: str,
        resource_claims: list[dict[str, Any]] | None = None,
        *,
        phase: str = "ticket-workflow",
        sanitized_input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not KEY.fullmatch(ticket):
            raise DispatchError("controller plan returned an invalid ticket key")
        if resource_claims is None:
            resource_claims = self.resource_claims(ticket, capacity=1, heavy_capacity=1)
        try:
            resource_claims = normalize_claims(resource_claims)
            resource_claim_digest = claim_set_digest(resource_claims)
        except AdmissionError as exc:
            raise DispatchError(str(exc)) from exc
        # Both protocol and execution-backend negotiation deliberately happen
        # before reservation or provider work.
        capability_offer, negotiation_receipt = self._phase_capability_offer()
        run_ref = f"supervisor-{ticket}-{uuid.uuid4().hex}"
        paths = self._job_paths(run_ref)
        _backend, backend_coordinator = self._execution_backend(
            sprint=sprint,
            ticket=ticket,
            reservation=None,
            paths=paths,
        )
        reservation = self.adapter.reserve(sprint, ticket, run_ref)
        attempt_token = str(reservation.get("attempt_token") or "")
        if not attempt_token or not reservation.get("attach_capability"):
            raise DispatchError("controller reservation omitted launch capabilities")
        try:
            phase_execution = self.phase_runtime.create_job(
                ticket_id=ticket,
                phase=phase,
                attempt_token=attempt_token,
                worktree_id=str(self.repository),
                sanitized_input=sanitized_input
                or {"sprint": sprint, "ticket": ticket, "phase": phase},
            )
            phase_record = self.phase_runtime.dispatch(
                phase_execution, capability_offer
            )
        except PhaseExecutionError as exc:
            raise DispatchError(str(exc)) from exc
        _private_write(
            paths["prompt"],
            result_prompt(
                ticket,
                sprint,
                attempt_token,
                paths["result"],
                phase_envelope=phase_record["job"],
            )
            + "\n",
        )
        job = {
            "ticket": ticket,
            "sprint": sprint,
            "run_ref": run_ref,
            "attempt_token": attempt_token,
            "execution_identity": {},
            "paths": {key: str(value) for key, value in paths.items()},
            "state": "reserved",
            "launched_at": time.time(),
            "terminal": {},
            "phase_execution": phase_execution,
            "phase_negotiation": negotiation_receipt,
            "execution_backend": {},
            "resource_claims": resource_claims,
            "resource_claim_schema": CLAIM_SCHEMA,
            "resource_claim_digest": resource_claim_digest,
            "resource_release": {},
            "history": [
                {
                    "event": "resource_claims_acquired",
                    "claim_digest": resource_claim_digest,
                }
            ],
        }
        try:
            backend = backend_coordinator.backend
            backend.bind_launch(
                sprint=sprint,
                ticket=ticket,
                reservation=reservation,
                prompt_path=paths["prompt"],
                output_path=paths["output"],
                result_path=paths["result"],
            )
            binding = phase_record["identity"]
            launch_receipt = backend_coordinator.launch(binding, phase_record["job"])
            attached = backend_coordinator.attach(binding)
            job["execution_backend"] = {
                "binding": binding,
                "offer": backend_coordinator.offer,
                "launch_receipt": launch_receipt,
                "attachment_receipt": attached,
            }
            job["launch_evidence"] = str(launch_receipt["backend_handle"])
        except (DispatchError, BackendContractError) as exc:
            job["state"] = "launch_uncertain"
            job["launch_error"] = str(exc)
            return job
        identity = attached.get("worker_identity")
        if not isinstance(identity, dict) or not identity.get("invocation_id"):
            job["state"] = "launch_uncertain"
            job["launch_error"] = "controller attach omitted execution-unit identity"
            return job
        job["execution_identity"] = identity
        try:
            self.phase_runtime.bind_attachment(phase_execution, identity)
        except PhaseExecutionError as exc:
            job["state"] = "launch_uncertain"
            job["launch_error"] = f"phase attachment rejected: {exc}"
            return job
        job["state"] = "running"
        return job

    def apply_terminal(self, job: dict[str, Any]) -> dict[str, Any]:
        """Apply exactly one result or return an evidenced replay no-op."""
        prior = job.get("terminal") or {}
        result_path = Path(job["paths"]["result"])
        if prior:
            if not result_path.is_file():
                return {"applied": False, "duplicate": True, "terminal": prior}
            raw_digest = hashlib.sha256(
                _regular_private_file(result_path, "worker result")
            ).hexdigest()
            if prior.get("result_digest") == raw_digest:
                return {"applied": False, "duplicate": True, "terminal": prior}
            raise DispatchError("terminal result changed after it was applied")
        raw_digest = hashlib.sha256(
            _regular_private_file(result_path, "worker result")
        ).hexdigest()
        identity = job.get("execution_identity") or {}
        try:
            value, digest = extract_terminal_result(result_path)
            result = validate_terminal_result(
                value,
                ticket=job["ticket"],
                sprint=job["sprint"],
                attempt_token=job["attempt_token"],
                invocation_id=str(identity.get("invocation_id") or ""),
            )
            work_identity = self.adapter.verify_work_identity(result)
            if work_identity is not None:
                result["evidence"]["preserved_work_identity"] = work_identity
            if result["outcome"] == "completed":
                result["evidence"]["merge_receipt"] = self.adapter.verify_completion(
                    result
                )
            event, target = self._worker_transition(result["outcome"])
            contract_evidence = self._contract_evidence(event, result, digest)
            phase_state = job.get("phase_execution")
            if isinstance(phase_state, dict):
                try:
                    if self._legacy_phase_execution(job):
                        self._phase_runtime_for_state(phase_state).ingest(
                            phase_state,
                            self._phase_terminal_envelope(job, result),
                        )
                    else:
                        _backend, backend_coordinator, binding = (
                            self._restore_execution_backend(job)
                        )
                        backend_terminal = backend_coordinator.terminal(binding)
                        backend_result = backend_terminal.get("terminal_result")
                        if not isinstance(backend_result, dict) or any(
                            backend_result.get(field) != result.get(field)
                            for field in (
                                "schema_version",
                                "ticket",
                                "sprint",
                                "attempt_token",
                                "invocation_id",
                                "outcome",
                                "summary",
                                "branch",
                                "worktree",
                                "pr",
                            )
                        ):
                            raise DispatchError(
                                "execution backend terminal result differs from supervisor evidence"
                            )
                        self._phase_runtime_for_state(phase_state).ingest(
                            phase_state,
                            backend_terminal["terminal_envelope"],
                        )
                except (PhaseExecutionError, BackendContractError) as exc:
                    raise DispatchError(f"phase terminal rejected: {exc}") from exc
            controller = self.adapter.finish(job["sprint"], job["ticket"], result)
            validation_error = ""
        except StaleResultError:
            raise
        except DispatchError as exc:
            digest = raw_digest
            result = {
                "outcome": "malformed_result",
                "summary": f"worker result rejected: {exc}",
                "branch": "",
                "worktree": "",
                "pr": "",
                "attempt_token": job["attempt_token"],
            }
            event, target = self._worker_transition("malformed_result")
            contract_evidence = {
                "attempt_token": job["attempt_token"],
                "validation_error": str(exc),
                "result_digest": digest,
            }
            phase_state = job.get("phase_execution")
            if isinstance(phase_state, dict) and phase_state.get("active") is not None:
                try:
                    self._phase_runtime_for_state(phase_state).reject_output(
                        phase_state, str(exc)
                    )
                except PhaseExecutionError as phase_exc:
                    raise DispatchError(str(phase_exc)) from phase_exc
            controller = self.adapter.finish(
                job["sprint"],
                job["ticket"],
                result,
                outcome="recoverable",
                summary=result["summary"],
            )
            validation_error = str(exc)
        terminal = {
            "event": event,
            "target_state": target,
            "contract_evidence": contract_evidence,
            "result_digest": digest,
            "outcome": result["outcome"],
            "controller_state": controller.get("state"),
            "branch": result.get("branch", ""),
            "worktree": result.get("worktree", ""),
            "pr": result.get("pr", ""),
            "validation_error": validation_error,
            "applied_at": time.time(),
        }
        breaker = self._terminal_breaker(
            result["outcome"], target, job["ticket"], contract_evidence
        )
        if breaker is not None:
            terminal["breaker"] = breaker
        if target == "retry_wait":
            terminal["retry_at"] = terminal["applied_at"] + self.retry_delay_seconds
            terminal["timer_id"] = f"retry:{job['run_ref']}"
        job["terminal"] = terminal
        job["state"] = target
        try:
            terminal["resource_release"] = release_claims(
                job, observed_at=terminal["applied_at"]
            )
        except AdmissionError as exc:
            raise DispatchError(str(exc)) from exc
        return {"applied": True, "duplicate": False, "terminal": terminal}

    def observe_execution(self, job: dict[str, Any]) -> dict[str, Any]:
        """Inspect one exact backend execution without inferring absence."""

        _backend, coordinator, binding = self._restore_execution_backend(job)
        try:
            return coordinator.inspect(binding)
        except BackendContractError as exc:
            raise DispatchError(f"execution inspection failed: {exc}") from exc

    def heartbeat_execution(self, job: dict[str, Any]) -> dict[str, Any]:
        _backend, coordinator, binding = self._restore_execution_backend(job)
        try:
            return coordinator.heartbeat(binding)
        except BackendContractError as exc:
            raise DispatchError(f"execution heartbeat failed: {exc}") from exc

    def progress_execution(self, job: dict[str, Any]) -> dict[str, Any]:
        _backend, coordinator, binding = self._restore_execution_backend(job)
        try:
            return coordinator.progress(binding)
        except BackendContractError as exc:
            raise DispatchError(f"execution progress failed: {exc}") from exc

    def cancel_execution(self, job: dict[str, Any], *, reason: str) -> dict[str, Any]:
        """Request cancellation, then apply the supervisor-owned fence.

        Today's host launcher cannot authenticate an in-band acknowledgement,
        so a request alone is never replacement-safe. The supervisor records
        its separate fence and binds the phase-runtime cancellation to it.
        """

        _backend, coordinator, binding = self._restore_execution_backend(job)
        phase_state = job.get("phase_execution")
        if not isinstance(phase_state, dict):
            raise DispatchError("job has no disposable phase execution")
        runtime = self._phase_runtime_for_state(phase_state)
        try:
            cancellation = runtime.request_cancel(
                phase_state,
                reason=reason,
                deadline="supervisor-fence-immediate",
            )
            requested = coordinator.cancel(binding, reason=reason)
            fenced = requested.get("supervisor_fence")
            if not isinstance(fenced, dict):
                fenced = coordinator.fence(binding, reason=reason)
            runtime.fence_cancellation(
                phase_state,
                cancellation["cancellation_id"],
                fenced["fence_receipt"],
            )
        except (BackendContractError, PhaseExecutionError) as exc:
            raise DispatchError(f"execution cancellation failed: {exc}") from exc
        return {"request": requested, "fence": fenced, "replacement_safe": True}

    def _fence_exited_execution(self, job: dict[str, Any], *, reason: str) -> None:
        try:
            _backend, coordinator, binding = self._restore_execution_backend(job)
            coordinator.fence(binding, reason=reason)
        except BackendContractError as exc:
            raise DispatchError(f"execution exit fence failed: {exc}") from exc

    def wake_due_retries(
        self, jobs: dict[str, Any], *, current_time: float | None = None
    ) -> list[dict[str, Any]]:
        """Requeue due ticket-local cooldowns after stopped-worker proof."""

        current_time = time.time() if current_time is None else current_time
        awakened: list[dict[str, Any]] = []
        for job in jobs.values():
            if job.get("state") != "retry_wait":
                continue
            terminal = job.get("terminal") or {}
            breaker = terminal.get("breaker") or {}
            if (
                breaker.get("class_id") != "ticket_retry_wait"
                or breaker.get("scope") != "ticket"
                or breaker.get("durable_state") != "retry_wait"
                or breaker.get("subject") != job.get("ticket")
            ):
                self.last_errors.append(
                    {
                        "ticket": str(job.get("ticket") or ""),
                        "error": "retry wait lacks its exact ticket breaker binding",
                    }
                )
                continue
            retry_at = terminal.get("retry_at")
            if not isinstance(retry_at, (int, float)) or retry_at > current_time:
                continue
            try:
                self.adapter.requeue(job["sprint"], job, "supervisor cooldown elapsed")
            except DispatchError as exc:
                self.last_errors.append({"ticket": job["ticket"], "error": str(exc)})
                # Avoid a tight supervisor loop when the stopped-worker proof
                # or controller transition is temporarily unavailable.
                terminal["retry_at"] = current_time + self.retry_delay_seconds
                continue
            job["state"] = "queued"
            job.setdefault("history", []).append(
                {
                    "event": "cooldown_elapsed",
                    "timer_id": terminal.get("timer_id"),
                    "observed_at": current_time,
                }
            )
            awakened.append(job)
        return awakened

    def prepare_continuations(
        self,
        sprint: str,
        plan: dict[str, list[str]],
        jobs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return controller-authorized repair/recovery work to the launch queue."""

        prepared: list[dict[str, Any]] = []
        expected_states = {"repair": "repair_ready", "recovery": "recovery_ready"}
        for action, expected_state in expected_states.items():
            for ticket in plan.get(action) or []:
                candidates = sorted(
                    (
                        job
                        for job in jobs.values()
                        if job.get("ticket") == ticket
                        and job.get("state") == expected_state
                    ),
                    key=lambda item: float(item.get("launched_at") or 0),
                    reverse=True,
                )
                if not candidates:
                    self.last_errors.append(
                        {
                            "ticket": ticket,
                            "error": (
                                f"{action} requires the supervisor-owned prior execution; "
                                "restart recovery belongs to the idempotency slice"
                            ),
                        }
                    )
                    continue
                job = candidates[0]
                try:
                    self.adapter.requeue(
                        sprint, job, f"supervisor admitted {action} continuation"
                    )
                except DispatchError as exc:
                    self.last_errors.append({"ticket": ticket, "error": str(exc)})
                    continue
                job["state"] = "queued"
                job.setdefault("history", []).append(
                    {
                        "event": f"{action}_continuation_queued",
                        "observed_at": time.time(),
                    }
                )
                prepared.append(job)
        return prepared

    def execution_terminal(self, job: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        if self._legacy_phase_execution(job):
            identity = job.get("execution_identity") or {}
            path = Path(str(identity.get("tombstone_path") or ""))
            if not path.is_file():
                return False, {}
            value = json.loads(_regular_private_file(path, "execution tombstone"))
            if (
                not isinstance(value, dict)
                or value.get("phase") != "terminal"
                or value.get("invocation_id") != identity.get("invocation_id")
            ):
                raise DispatchError(
                    "legacy execution tombstone does not match the attached worker"
                )
            return True, value
        observation = self.observe_execution(job)
        return observation.get("status") in {"absent", "terminal"}, observation

    def apply_process_exit(self, job: dict[str, Any]) -> dict[str, Any] | None:
        terminal, tombstone = self.execution_terminal(job)
        if not terminal:
            return None
        if Path(job["paths"]["result"]).is_file():
            if tombstone.get("status") == "absent" and not self._legacy_phase_execution(
                job
            ):
                self._fence_exited_execution(
                    job,
                    reason="execution exited with an invalid structured terminal result",
                )
            return self.apply_terminal(job)
        if job.get("terminal"):
            return {"applied": False, "duplicate": True, "terminal": job["terminal"]}
        if not self._legacy_phase_execution(job):
            self._fence_exited_execution(
                job, reason="execution exited without a structured terminal result"
            )
        raw = json.dumps(tombstone, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        result = {
            "outcome": "malformed_result",
            "summary": "worker execution exited without a structured terminal result",
            "branch": "",
            "worktree": "",
            "pr": "",
            "attempt_token": job["attempt_token"],
        }
        phase_state = job.get("phase_execution")
        if isinstance(phase_state, dict) and phase_state.get("active") is not None:
            try:
                self._phase_runtime_for_state(phase_state).reject_output(
                    phase_state, "missing structured terminal result"
                )
            except PhaseExecutionError as exc:
                raise DispatchError(str(exc)) from exc
        controller = self.adapter.finish(
            job["sprint"],
            job["ticket"],
            result,
            outcome="recoverable",
            summary=result["summary"],
        )
        event, target = self._worker_transition("malformed_result")
        applied = {
            "event": event,
            "target_state": target,
            "contract_evidence": {
                "attempt_token": job["attempt_token"],
                "validation_error": "missing structured terminal result",
                "result_digest": digest,
            },
            "result_digest": digest,
            "outcome": "malformed_result",
            "controller_state": controller.get("state"),
            "branch": "",
            "worktree": "",
            "pr": "",
            "validation_error": "missing structured terminal result",
            "applied_at": time.time(),
        }
        breaker = self._terminal_breaker(
            "malformed_result",
            target,
            job["ticket"],
            applied["contract_evidence"],
        )
        if breaker is not None:
            applied["breaker"] = breaker
        job["terminal"] = applied
        job["state"] = target
        try:
            applied["resource_release"] = release_claims(
                job, observed_at=applied["applied_at"]
            )
        except AdmissionError as exc:
            raise DispatchError(str(exc)) from exc
        return {"applied": True, "duplicate": False, "terminal": applied}

    def fill(
        self,
        sprint: str,
        tickets: list[str],
        jobs: dict[str, Any],
        capacity: int,
        *,
        heavy_capacity: int | None = None,
        claims_by_ticket: dict[str, list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        self.last_errors = []
        self.last_skips = []
        heavy_capacity = capacity if heavy_capacity is None else heavy_capacity
        claims_by_ticket = claims_by_ticket or {}
        try:
            migrate_legacy_active_jobs(
                jobs,
                lambda ticket: self.resource_claims(
                    ticket,
                    capacity=capacity,
                    heavy_capacity=heavy_capacity,
                    additional=claims_by_ticket.get(ticket),
                ),
            )
        except (AdmissionError, DispatchError) as exc:
            raise DispatchError(f"durable resource state is invalid: {exc}") from exc
        active = sum(
            1
            for job in jobs.values()
            if job.get("state") in {"running", "reserved", "launch_uncertain"}
        )
        launched = []
        for ticket in tickets:
            if active >= capacity:
                break
            if any(
                job.get("ticket") == ticket
                and job.get("state") in {"running", "reserved", "launch_uncertain"}
                for job in jobs.values()
            ):
                continue
            try:
                claims = self.resource_claims(
                    ticket,
                    capacity=capacity,
                    heavy_capacity=heavy_capacity,
                    additional=claims_by_ticket.get(ticket),
                )
                blocked = conflicts(claims, jobs)
                if blocked:
                    self.last_skips.append(
                        {
                            "ticket": ticket,
                            "claim_digest": claim_set_digest(claims),
                            "conflicts": blocked,
                        }
                    )
                    continue
                job = self.launch(sprint, ticket, claims)
            except (AdmissionError, DispatchError) as exc:
                self.last_errors.append({"ticket": ticket, "error": str(exc)})
                continue
            jobs[job["run_ref"]] = job
            launched.append(job)
            if job["state"] in {"running", "launch_uncertain", "reserved"}:
                active += 1
        return launched
