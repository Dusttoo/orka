#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATOR="$ROOT/scripts/phase_worker_contract.py"
CONTRACT="$ROOT/contracts/phase-worker-protocol-v1.json"
FIXTURES="$ROOT/tests/fixtures/phase-worker-protocol-v1.json"
TMP="$(mktemp -d)"
trap 'find "$TMP" -type f -delete; rmdir "$TMP"' EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

validate() {
  python3 "$VALIDATOR" "$1" --fixtures "${2:-$FIXTURES}" >/dev/null 2>&1
}

validate "$CONTRACT" || fail "canonical phase-worker contract and fixtures validate"
pass "canonical phase-worker contract and fixtures validate"

jq '.context_lifecycle.conversation_reuse_across_dispatches = true' \
  "$CONTRACT" >"$TMP/reused-context.json"
if validate "$TMP/reused-context.json"; then
  fail "conversation reuse across dispatches is rejected"
fi
pass "conversation reuse across dispatches is rejected"

jq '.capabilities.mandatory -= ["attempt-fencing"]' \
  "$CONTRACT" >"$TMP/no-fence.json"
if validate "$TMP/no-fence.json"; then
  fail "removing attempt fencing is rejected"
fi
pass "removing attempt fencing is rejected"

jq 'del(.envelopes.terminal.identity_fields[] | select(. == "supervisor_fence"))' \
  "$CONTRACT" >"$TMP/unbound-terminal.json"
if python3 "$VALIDATOR" "$TMP/unbound-terminal.json" >/dev/null 2>&1; then
  fail "terminal results without a supervisor fence are rejected"
fi
pass "terminal results without a supervisor fence are rejected"

jq 'del(.envelopes.progress.identity_fields[] | select(. == "attempt_token"))' \
  "$CONTRACT" >"$TMP/unbound-progress.json"
if python3 "$VALIDATOR" "$TMP/unbound-progress.json" >/dev/null 2>&1; then
  fail "progress without an attempt binding is rejected"
fi
pass "progress without an attempt binding is rejected"

jq '.adapter_profiles |= del(.api)' "$CONTRACT" >"$TMP/missing-adapter.json"
if validate "$TMP/missing-adapter.json"; then
  fail "missing adapter capability profiles are rejected"
fi
pass "missing adapter capability profiles are rejected"

jq '.cases |= map(if .name == "stale supervisor fence is rejected" then .valid = true | del(.error) else . end)' \
  "$FIXTURES" >"$TMP/stale-as-valid.json"
if validate "$CONTRACT" "$TMP/stale-as-valid.json"; then
  fail "a stale supervisor fence cannot become a valid fixture"
fi
pass "a stale supervisor fence cannot become a valid fixture"

jq '.cases |= map(if .name == "conversation resumption is forbidden" then .valid = true | del(.error) else . end)' \
  "$FIXTURES" >"$TMP/resume-as-valid.json"
if validate "$CONTRACT" "$TMP/resume-as-valid.json"; then
  fail "a resumed model conversation cannot become a valid fixture"
fi
pass "a resumed model conversation cannot become a valid fixture"

jq '.artifact_bindings.external_verification_required = false' \
  "$CONTRACT" >"$TMP/untrusted-artifacts.json"
if python3 "$VALIDATOR" "$TMP/untrusted-artifacts.json" >/dev/null 2>&1; then
  fail "terminal artifact bindings require external verification"
fi
pass "terminal artifact bindings require external verification"

jq '.capabilities.optional += ["future-additive-capability"]
    | .adapter_profiles.api.required_capabilities += ["future-additive-capability"]' \
  "$CONTRACT" >"$TMP/additive-contract.json"
jq '.cases |= map(
      if .name == "api advertises provider receipts"
      then .envelope.supported_capabilities += ["future-additive-capability"]
      else .
      end
    )' "$FIXTURES" >"$TMP/additive-fixtures.json"
validate "$TMP/additive-contract.json" "$TMP/additive-fixtures.json" || \
  fail "additive optional capabilities remain negotiable"
pass "additive optional capabilities remain negotiable"

printf '\nALL PASS\n'
