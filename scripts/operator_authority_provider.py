#!/usr/bin/env python3
"""Validate Orka's versioned operator-authority provider contract.

This is deliberately a conformance coordinator, not an issuer.  It accepts
only a provider discovery document and opaque provider receipts; issuing,
storing, and authenticating a capability stay on the separately trusted host.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
import time
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    pass


_CLASSES = {
    "budget-extension": {"raise-ticket-budget"},
    "relaunch-extension": {"raise-ticket-attempt-limit"},
    "terminal-recovery": {"recover-exact-terminal-attempt"},
    "review-repair": {"extend-pr-repair-cycles"},
    "restart": {"restart-ticket-with-absolute-ceilings"},
}
_FORBIDDEN = {"review", "security", "merge", "destructive-action"}
_TICKET = re.compile(r"[A-Z][A-Z0-9_]*-[0-9]+")
_PR = re.compile(r"[1-9][0-9]{0,19}")
_INSTALLED_HELPER = Path("/usr/local/libexec/orchestration-recovery-authority")


def _positive_decimal(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"{label} must be a decimal string") from exc
    if not amount.is_finite() or amount <= 0:
        raise ContractError(f"{label} must be positive")
    return str(amount)


def validate_scope(authority_class: str, scope: Any) -> dict[str, Any]:
    """Validate the closed, class-specific provider scope.

    The local helper predates this contract and keys its private records by a
    smaller legacy scope.  The adapter derives that key below, but the
    provider-facing receipt still binds the absolute ceiling/allowances so a
    caller cannot widen them between verification and consumption.
    """
    if not isinstance(scope, dict):
        raise ContractError("capability scope must be an object")
    if authority_class in {"budget-extension", "relaunch-extension", "restart"}:
        required = {"ticket"}
    elif authority_class == "terminal-recovery":
        required = {"ticket", "attempt"}
    elif authority_class == "review-repair":
        required = {"pr"}
    else:
        raise ContractError("capability has an unknown authority class")
    if authority_class == "budget-extension":
        required.add("ticket_usd")
    elif authority_class == "relaunch-extension":
        required.add("attempts")
    elif authority_class == "review-repair":
        required.add("repair_cycles")
    elif authority_class == "restart":
        required.add("allowances")
    if set(scope) != required:
        raise ContractError("capability scope has missing, widened, or unknown fields")
    if "ticket" in required and (
        type(scope["ticket"]) is not str or not _TICKET.fullmatch(scope["ticket"])
    ):
        raise ContractError("capability scope has an invalid ticket")
    if "pr" in required and (
        type(scope["pr"]) is not str or not _PR.fullmatch(scope["pr"])
    ):
        raise ContractError("capability scope has an invalid PR")
    if "attempt" in required and (
        type(scope["attempt"]) is not int
        or isinstance(scope["attempt"], bool)
        or scope["attempt"] < 0
    ):
        raise ContractError("terminal recovery requires an exact non-negative attempt")
    if "ticket_usd" in required:
        _positive_decimal(scope["ticket_usd"], "ticket_usd")
    for key in ("attempts", "repair_cycles"):
        if key in required and (
            type(scope[key]) is not int
            or isinstance(scope[key], bool)
            or not 0 < scope[key] <= 10000
        ):
            raise ContractError(f"{key} must be a bounded positive integer")
    if "allowances" in required:
        allowances = scope["allowances"]
        if not isinstance(allowances, dict) or not allowances:
            raise ContractError("restart allowances must be a closed nonempty object")
        # This mirrors the root helper's exact documented field set without
        # accepting a future provider's extra grant dimensions implicitly.
        counts = {
            "attempts",
            "model_runs",
            "review_runs",
            "design_rounds",
            "code_rounds",
            "security_rounds",
            "repair_cycles",
        }
        dollars = {
            "ticket_usd",
            "design_usd",
            "implementation_usd",
            "code_review_usd",
            "security_review_usd",
            "progress_baseline_usd",
        }
        if set(allowances) != counts | dollars:
            raise ContractError(
                "restart allowances have missing, widened, or unknown fields"
            )
        for key in counts:
            if (
                type(allowances[key]) is not int
                or isinstance(allowances[key], bool)
                or not 0 < allowances[key] <= 10000
            ):
                raise ContractError("restart allowances have an invalid count")
        for key in dollars:
            try:
                value = Decimal(str(allowances[key]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ContractError(
                    "restart allowances have an invalid dollar ceiling"
                ) from exc
            if (
                not value.is_finite()
                or value < 0
                or (value == 0 and key != "progress_baseline_usd")
            ):
                raise ContractError("restart allowances have an invalid dollar ceiling")
        if Decimal(str(allowances["progress_baseline_usd"])) >= Decimal(
            str(allowances["ticket_usd"])
        ):
            raise ContractError("restart baseline must be below ticket ceiling")
    return dict(scope)


def load_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(
            f"cannot read operator-authority provider contract: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ContractError("operator-authority provider contract must be an object")
    return value


def _strings(value: Any, label: str) -> set[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(item) is not str or not item for item in value)
    ):
        raise ContractError(f"{label} must be a nonempty string array")
    if len(value) != len(set(value)):
        raise ContractError(f"{label} must not contain duplicates")
    return set(value)


def _versions(value: Any, label: str) -> set[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(item) is not int or item <= 0 for item in value)
        or len(value) != len(set(value))
    ):
        raise ContractError(f"{label} must be a nonempty unique positive integer array")
    return set(value)


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if (
        contract.get("schema_version") != 1
        or contract.get("contract_id") != "orka.operator-authority-provider"
        or contract.get("protocol_version") != 1
    ):
        raise ContractError("contract identity must be operator-authority provider v1")
    if _strings(contract.get("operations"), "operations") != {
        "discover",
        "issue",
        "verify",
        "consume",
        "revoke",
    }:
        raise ContractError("operations must provide the complete authority lifecycle")
    classes = contract.get("authority_classes")
    if not isinstance(classes, dict) or set(classes) != set(_CLASSES):
        raise ContractError("authority classes must be complete and closed")
    for authority_class, action in _CLASSES.items():
        if (
            _strings(classes[authority_class], f"authority_classes.{authority_class}")
            != action
        ):
            raise ContractError(f"{authority_class} has an unsafe action mapping")
    capability = contract.get("capability")
    fields = {
        "provider_id",
        "capability_id",
        "authority_class",
        "repository_id",
        "scope",
        "issuer_id",
        "issued_at",
        "expires_at",
        "one_use",
        "authorized_actions",
        "receipt_id",
    }
    if (
        not isinstance(capability, dict)
        or _strings(capability.get("required_fields"), "capability.required_fields")
        != fields
    ):
        raise ContractError("capability identity is incomplete")
    if (
        capability.get("one_use") is not True
        or capability.get("repository_bound") is not True
        or capability.get("absolute_ceilings_only") is not True
        or capability.get("tokens_are_opaque") is not True
        or capability.get("class_specific_scope_schemas") is not True
        or capability.get("scope_binds_absolute_ceilings") is not True
    ):
        raise ContractError(
            "capabilities must remain one-use, repository-bound, opaque, absolute grants"
        )
    if (
        _strings(
            capability.get("target_scope_fields"), "capability.target_scope_fields"
        )
        != {"ticket", "pr"}
        or capability.get("terminal_recovery_requires_attempt") is not True
    ):
        raise ContractError("capabilities must bind an exact ticket or PR target")
    verify = contract.get("verification")
    if not isinstance(verify, dict) or _strings(
        verify.get("required_matches"), "verification.required_matches"
    ) != {"provider_id", "authority_class", "repository_id", "scope"}:
        raise ContractError(
            "verification must bind provider, class, repository, and scope"
        )
    expected_verify = {
        "expired": "deny",
        "revoked": "deny",
        "replayed": "deny",
        "incompatible_provider": "deny",
        "unavailable_provider": "unknown-fail-closed",
    }
    if {key: verify.get(key) for key in expected_verify} != expected_verify:
        raise ContractError("verification failures must fail closed")
    consume = contract.get("consumption")
    expected_receipt = {"provider_id", "capability_id", "receipt_id", "consumed_at"}
    if (
        not isinstance(consume, dict)
        or consume.get("durable_before_success") is not True
        or consume.get("replay_safe") is not True
        or consume.get("failure_preserves_state") is not True
        or _strings(
            consume.get("success_receipt_fields"), "consumption.success_receipt_fields"
        )
        != expected_receipt
    ):
        raise ContractError("consumption must be durable and replay-safe")
    revocation = contract.get("revocation")
    if revocation != {
        "durable": True,
        "takes_effect_before_future_verification": True,
        "does_not_restore_consumed_capabilities": True,
    }:
        raise ContractError("revocation must be durable and irreversible")
    discovery = contract.get("discovery")
    if (
        not isinstance(discovery, dict)
        or discovery.get("provider_id_required") is not True
        or discovery.get("authority_classes_required") is not True
        or discovery.get("repository_configuration_may_select_provider") is not False
        or discovery.get("worker_configuration_may_select_provider") is not False
        or discovery.get("scheduler_state_machine_dependency") is not False
        or _versions(discovery.get("protocol_versions"), "discovery.protocol_versions")
        != {1}
    ):
        raise ContractError("discovery must be host-owned and scheduler-neutral")
    trust = contract.get("trust_boundary")
    if (
        not isinstance(trust, dict)
        or trust.get("issuance_must_be_outside_repository_and_worker_control")
        is not True
        or trust.get("provider_may_not_authorize_unrequested_actions") is not True
        or trust.get("outage_preserves_existing_state") is not True
        or _strings(
            trust.get("provider_may_not_bypass"),
            "trust_boundary.provider_may_not_bypass",
        )
        != _FORBIDDEN
    ):
        raise ContractError("authority trust boundary is incomplete")
    profiles = contract.get("compatibility_profiles")
    profile = (
        profiles.get("local-root-helper-v1") if isinstance(profiles, dict) else None
    )
    expected_issue = {
        "issue-budget",
        "issue-relaunch",
        "issue-recovery",
        "issue-review-repair",
        "issue-restart",
    }
    expected_runtime = {
        "activate-budget",
        "activate-relaunch",
        "consume-recovery",
        "activate-review-repair",
        "activate-restart",
    }
    expected_revoke = {
        "revoke-budget",
        "revoke-relaunch",
        "revoke-recovery",
        "revoke-review-repair",
        "revoke-restart",
    }
    if (
        not isinstance(profile, dict)
        or profile.get("provider_id") != "orka.local-root-helper"
        or profile.get("issuer_identity") != "host-root"
        or profile.get("storage") != "root-owned private files"
        or profile.get("compatibility_adapter_required") is not True
        or _strings(profile.get("issue_commands"), "local root helper issue commands")
        != expected_issue
        or _strings(
            profile.get("runtime_commands"), "local root helper runtime commands"
        )
        != expected_runtime
        or _strings(profile.get("revoke_commands"), "local root helper revoke commands")
        != expected_revoke
    ):
        raise ContractError("local root-helper compatibility profile is incomplete")
    return {
        "contract_id": contract["contract_id"],
        "protocol_version": 1,
        "authority_classes": len(_CLASSES),
    }


def validate_discovery(
    discovery: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(discovery, dict):
        raise ContractError("provider discovery must be an object")
    provider_id = discovery.get("provider_id")
    if type(provider_id) is not str or not re.fullmatch(
        r"orka\.[a-z0-9][a-z0-9.-]{1,126}", provider_id
    ):
        raise ContractError("provider discovery has an invalid provider_id")
    versions = _versions(
        discovery.get("protocol_versions"), "provider.protocol_versions"
    )
    if 1 not in versions:
        raise ContractError("provider does not support protocol v1")
    supported = _strings(
        discovery.get("authority_classes"), "provider.authority_classes"
    )
    unknown = supported - set(_CLASSES)
    if unknown:
        raise ContractError("provider advertises an unknown authority class")
    issuer = discovery.get("issuer_identity")
    if type(issuer) is not str or not issuer:
        raise ContractError("provider discovery has no issuer identity")
    return {
        "provider_id": provider_id,
        "issuer_identity": issuer,
        "authority_classes": sorted(supported),
    }


def validate_capability_receipt(
    receipt: dict[str, Any],
    *,
    provider_id: str,
    authority_class: str,
    repository_id: str,
    scope: dict[str, Any],
    action: str,
    now: float,
) -> dict[str, Any]:
    """Validate an opaque provider receipt before scheduler integration.

    This validates shape and exact bindings only. Whether a receipt is revoked
    or consumed remains a durable provider query; a caller must treat any
    unavailable query as unknown rather than infer permission here.
    """
    fields = {
        "provider_id",
        "capability_id",
        "authority_class",
        "repository_id",
        "scope",
        "issuer_id",
        "issued_at",
        "expires_at",
        "one_use",
        "authorized_actions",
        "receipt_id",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ContractError("capability receipt has unexpected fields")
    if (
        receipt["provider_id"] != provider_id
        or receipt["authority_class"] != authority_class
        or receipt["repository_id"] != repository_id
        or receipt["scope"] != scope
    ):
        raise ContractError("capability receipt binding mismatch")
    validate_scope(authority_class, receipt["scope"])
    if (
        authority_class not in _CLASSES
        or _strings(receipt["authorized_actions"], "capability authorized actions")
        != _CLASSES[authority_class]
        or action not in _CLASSES[authority_class]
    ):
        raise ContractError("capability receipt authorizes an unsafe action")
    if receipt["one_use"] is not True:
        raise ContractError("capability receipt is not one-use")
    if not all(
        isinstance(receipt[field], str) and receipt[field]
        for field in ("capability_id", "issuer_id", "receipt_id")
    ):
        raise ContractError("capability receipt identity is incomplete")
    if (
        type(receipt["issued_at"]) not in {int, float}
        or type(receipt["expires_at"]) not in {int, float}
        or receipt["issued_at"] > now
        or receipt["expires_at"] <= receipt["issued_at"]
        or receipt["expires_at"] <= now
    ):
        raise ContractError(
            "capability receipt is future-issued, expired, or has invalid timestamps"
        )
    return {
        "capability_id": receipt["capability_id"],
        "receipt_id": receipt["receipt_id"],
    }


@dataclass(frozen=True)
class LocalRootAuthorityProvider:
    """Compatibility adapter for the installed root-owned helper.

    This is intentionally constructed from the host's installed-helper policy,
    never from repository configuration, a worker request, or a receipt handed
    to the scheduler.  It does not deserialize a caller-fabricated provider
    receipt: every verify/consume/revoke transition executes or queries the
    helper itself.
    """

    helper: Path
    test_mode: bool = False
    environment: dict[str, str] | None = None

    def __post_init__(self) -> None:
        """Make the trust claim true even for direct constructor calls.

        The dataclass is importable by repository code, so factory-method
        convention alone is not a security boundary. Production instances are
        pinned to the one installed path and re-check its ownership and mode.
        Tests require an explicit isolated environment and cannot silently
        become production providers.
        """
        helper = self.helper.resolve()
        object.__setattr__(self, "helper", helper)
        if self.test_mode:
            if (
                self.environment is None
                or self.environment.get("ORCHESTRATION_AUTHORITY_TEST_MODE") != "1"
            ):
                raise ContractError(
                    "test local root authority requires explicit test mode"
                )
            if not helper.is_file():
                raise ContractError("test local root authority helper is unavailable")
            return
        if self.environment is not None:
            raise ContractError("production authority environment cannot be overridden")
        if helper != _INSTALLED_HELPER:
            raise ContractError("production authority helper path is host-pinned")
        try:
            info = helper.lstat()
        except OSError as exc:
            raise ContractError(
                f"installed local root authority helper is unavailable: {exc}"
            ) from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ContractError(
                "installed local root authority helper must be a root-owned, "
                "non-writable regular file"
            )

    @classmethod
    def from_installed_helper(cls) -> "LocalRootAuthorityProvider":
        """Discover the fixed helper after its ownership check has passed."""
        import operator_authority as authority  # local to avoid CLI dependency

        helper, test_mode = authority._helper()
        if test_mode:
            raise ContractError(
                "installed authority discovery cannot select a test helper"
            )
        return cls(helper=helper, test_mode=False, environment=None)

    @classmethod
    def for_test(
        cls, helper: Path, environment: dict[str, str]
    ) -> "LocalRootAuthorityProvider":
        """Test-only trusted host configuration, not a production selector."""
        if environment.get("ORCHESTRATION_AUTHORITY_TEST_MODE") != "1":
            raise ContractError("test local root authority requires explicit test mode")
        return cls(
            helper=helper.resolve(), test_mode=True, environment=dict(environment)
        )

    def discover(self) -> dict[str, Any]:
        if not self.helper.is_file():
            raise ContractError("installed local root authority helper is unavailable")
        return {
            "provider_id": "orka.local-root-helper",
            "issuer_identity": "host-root",
            "protocol_versions": [1],
            "authority_classes": sorted(_CLASSES),
        }

    @staticmethod
    def _legacy_scope(
        repository: Path, authority_class: str, scope: dict[str, Any]
    ) -> str:
        repository_id = str(repository.resolve())
        if authority_class == "review-repair":
            raw = {
                "kind": "review-repair",
                "repository": repository_id,
                "pr": scope["pr"],
            }
        else:
            kind = {
                "budget-extension": "budget",
                "relaunch-extension": "relaunch",
                "terminal-recovery": "recovery",
                "restart": "restart",
            }[authority_class]
            raw = {"kind": kind, "repository": repository_id, "ticket": scope["ticket"]}
            if authority_class == "terminal-recovery":
                raw["attempt"] = scope["attempt"]
        return json.dumps(raw, sort_keys=True, separators=(",", ":"))

    def _run(
        self, command: str, *args: str, token: str = "", operator: bool = False
    ) -> subprocess.CompletedProcess[str]:
        # Runtime actions are exposed via narrow sudo on a real host.  Root-only
        # issue/revoke actions deliberately need an already-root caller; this
        # adapter never broadens the sudo surface on behalf of repository code.
        argv = [str(self.helper), command, *args]
        if not self.test_mode and not operator:
            argv = ["sudo", "-n", *argv]
        try:
            result = subprocess.run(
                argv,
                input=(token + "\n") if token else None,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
                env=self.environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ContractError(f"local root authority is unavailable: {exc}") from exc
        return result

    @staticmethod
    def _require(result: subprocess.CompletedProcess[str], action: str) -> str:
        if result.returncode != 0:
            detail = result.stderr.strip() or "request denied"
            raise ContractError(f"local root authority denied {action}: {detail}")
        return result.stdout.strip()

    def issue(
        self,
        repository: Path,
        authority_class: str,
        scope: dict[str, Any],
        *,
        reason: str = "operator-approved bounded authority",
    ) -> str:
        """Root-only compatibility issuance with a closed class/scope map."""
        scope = validate_scope(authority_class, scope)
        if not repository.is_dir():
            raise ContractError("repository does not exist")
        common = ["--repository", str(repository), "--expires-hours", "24"]
        if authority_class == "budget-extension":
            command, extra = (
                "issue-budget",
                ["--ticket", scope["ticket"], "--ceiling-usd", scope["ticket_usd"]],
            )
        elif authority_class == "relaunch-extension":
            command, extra = (
                "issue-relaunch",
                [
                    "--ticket",
                    scope["ticket"],
                    "--ceiling-attempts",
                    str(scope["attempts"]),
                ],
            )
        elif authority_class == "terminal-recovery":
            command, extra = (
                "issue-recovery",
                ["--ticket", scope["ticket"], "--attempt", str(scope["attempt"])],
            )
        elif authority_class == "review-repair":
            command, extra = (
                "issue-review-repair",
                [
                    "--pr",
                    scope["pr"],
                    "--ceiling-repair-cycles",
                    str(scope["repair_cycles"]),
                    "--reason",
                    reason,
                ],
            )
        elif authority_class == "restart":
            raise ContractError(
                "restart issuance requires an operator-owned allowances file"
            )
        else:
            raise ContractError("unknown authority class")
        return self._require(
            self._run(command, *common, *extra, operator=True), "issue"
        ).strip()

    def consume(
        self, repository: Path, authority_class: str, scope: dict[str, Any], token: str
    ) -> dict[str, Any]:
        scope = validate_scope(authority_class, scope)
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ContractError("capability token is malformed")
        legacy = self._legacy_scope(repository, authority_class, scope)
        command = {
            "budget-extension": "activate-budget",
            "relaunch-extension": "activate-relaunch",
            "terminal-recovery": "consume-recovery",
            "review-repair": "activate-review-repair",
            "restart": "activate-restart",
        }[authority_class]
        output = self._require(
            self._run(command, "--scope", legacy, token=token), "consume"
        )
        # The helper durably moves pending to consumed before it prints success.
        # Return only non-secret evidence derived from that trusted command.
        return {
            "provider_id": "orka.local-root-helper",
            "authority_class": authority_class,
            "scope": scope,
            "result": output,
            "consumed_at": time.time(),
        }

    def verify(
        self, repository: Path, authority_class: str, scope: dict[str, Any]
    ) -> dict[str, Any] | None:
        scope = validate_scope(authority_class, scope)
        legacy = self._legacy_scope(repository, authority_class, scope)
        command = {
            "budget-extension": "budget-ceiling",
            "relaunch-extension": "relaunch-ceiling",
            "review-repair": "review-repair-grant",
            "restart": "restart-grant",
        }.get(authority_class)
        if command is None:
            # Recovery has no active grant by design: it is consumed exactly
            # once, so a later verification cannot recreate authority.
            return None
        result = self._run(command, "--scope", legacy)
        if result.returncode == 3:
            return None
        output = self._require(result, "verify")
        return {
            "provider_id": "orka.local-root-helper",
            "authority_class": authority_class,
            "scope": scope,
            "result": output,
        }

    def revoke(
        self, repository: Path, authority_class: str, scope: dict[str, Any]
    ) -> None:
        scope = validate_scope(authority_class, scope)
        if authority_class == "budget-extension":
            command = ("revoke-budget", ["--ticket", scope["ticket"]])
        elif authority_class == "relaunch-extension":
            command = ("revoke-relaunch", ["--ticket", scope["ticket"]])
        elif authority_class == "terminal-recovery":
            command = (
                "revoke-recovery",
                ["--ticket", scope["ticket"], "--attempt", str(scope["attempt"])],
            )
        elif authority_class == "review-repair":
            command = ("revoke-review-repair", ["--pr", scope["pr"]])
        else:
            command = ("revoke-restart", ["--ticket", scope["ticket"]])
        self._require(
            self._run(
                command[0], "--repository", str(repository), *command[1], operator=True
            ),
            "revoke",
        )
