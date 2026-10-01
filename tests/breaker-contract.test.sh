#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATOR="$ROOT/scripts/breaker_contract.py"
CONTRACT="$ROOT/contracts/breaker-classification-v1.json"
CONTROLLER="$ROOT/scripts/sprint-controller.py"
TMP="$(mktemp -d)"
trap 'find "$TMP" -type f -delete; rmdir "$TMP"' EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

validate() {
  python3 "$VALIDATOR" "$1" --controller "$CONTROLLER" >/dev/null 2>&1
}

validate "$CONTRACT" || fail "canonical breaker contract validates"
pass "canonical breaker contract validates"

jq 'del(.stop_sources[0])' "$CONTRACT" >"$TMP/missing-source.json"
if validate "$TMP/missing-source.json"; then
  fail "missing current stop source is rejected"
fi
pass "missing current stop source is rejected"

jq '.stop_sources += [.stop_sources[0]]' "$CONTRACT" >"$TMP/duplicate-source.json"
if validate "$TMP/duplicate-source.json"; then
  fail "duplicate stop source is rejected"
fi
pass "duplicate stop source is rejected"

jq '.stop_sources[0].class = "undefined"' "$CONTRACT" >"$TMP/undefined-class.json"
if validate "$TMP/undefined-class.json"; then
  fail "undefined breaker class is rejected"
fi
pass "undefined breaker class is rejected"

jq '.classes.ticket_retry_wait.global_transition = true' "$CONTRACT" >"$TMP/ticket-global.json"
if validate "$TMP/ticket-global.json"; then
  fail "ticket breaker cannot declare a global transition"
fi
pass "ticket breaker cannot declare a global transition"

jq '.classes.route_transient_hold.global_transition = true' "$CONTRACT" >"$TMP/route-global.json"
if validate "$TMP/route-global.json"; then
  fail "route breaker cannot declare a global transition"
fi
pass "route breaker cannot declare a global transition"

jq '.classes.ticket_hard_decision.strength = "soft" | .classes.ticket_hard_decision.immutable_hard = false' "$CONTRACT" >"$TMP/softened-budget.json"
if validate "$TMP/softened-budget.json"; then
  fail "protected hard controls cannot be softened"
fi
pass "protected hard controls cannot be softened"

jq '.stop_sources |= map(if .id == "max_usd_per_sprint" then .class = "ticket_hard_decision" else . end)' "$CONTRACT" >"$TMP/rescoped-sprint.json"
if validate "$TMP/rescoped-sprint.json"; then
  fail "sprint budget cannot be reclassified as ticket scope"
fi
pass "sprint budget cannot be reclassified as ticket scope"

jq '.stop_sources |= map(if .id == "provider_authentication" then .class = "route_transient_hold" else . end)' "$CONTRACT" >"$TMP/softened-route-auth.json"
if validate "$TMP/softened-route-auth.json"; then
  fail "provider authentication hold cannot be softened"
fi
pass "provider authentication hold cannot be softened"

jq 'del(.current_ticket_state_compatibility.user_action)' "$CONTRACT" >"$TMP/missing-legacy.json"
if validate "$TMP/missing-legacy.json"; then
  fail "missing legacy state mapping is rejected"
fi
pass "missing legacy state mapping is rejected"

jq '.invariants.history_is_never_erased = false' "$CONTRACT" >"$TMP/weakened-invariant.json"
if validate "$TMP/weakened-invariant.json"; then
  fail "weakened breaker invariant is rejected"
fi
pass "weakened breaker invariant is rejected"

printf '\nALL PASS\n'
