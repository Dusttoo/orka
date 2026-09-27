#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATOR="$ROOT/scripts/supervisor_contract.py"
CONTRACT="$ROOT/contracts/supervisor-lifecycle-v1.json"
CONTROLLER="$ROOT/scripts/sprint-controller.py"
TMP="$(mktemp -d)"
trap 'find "$TMP" -type f -delete; rmdir "$TMP"' EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

validate() {
  python3 "$VALIDATOR" "$1" --controller "$CONTROLLER" >/dev/null 2>&1
}

validate "$CONTRACT" || fail "canonical lifecycle contract validates"
pass "canonical lifecycle contract validates"

jq '.transitions[0].to = "undefined"' "$CONTRACT" >"$TMP/undefined-state.json"
if validate "$TMP/undefined-state.json"; then
  fail "undefined transition states are rejected"
fi
pass "undefined transition states are rejected"

jq '.transitions[0].event = "undefined"' "$CONTRACT" >"$TMP/undefined-event.json"
if validate "$TMP/undefined-event.json"; then
  fail "undefined transition events are rejected"
fi
pass "undefined transition events are rejected"

jq '.transitions += [.transitions[0]]' "$CONTRACT" >"$TMP/ambiguous.json"
if validate "$TMP/ambiguous.json"; then
  fail "ambiguous transitions are rejected"
fi
pass "ambiguous transitions are rejected"

jq 'del(.current_ticket_state_compatibility.pending)' "$CONTRACT" >"$TMP/missing-compat.json"
if validate "$TMP/missing-compat.json"; then
  fail "missing current-state mappings are rejected"
fi
pass "missing current-state mappings are rejected"

jq '.worker_terminal_results.completed = "route_degraded"' "$CONTRACT" >"$TMP/missing-result.json"
if validate "$TMP/missing-result.json"; then
  fail "worker results without one running transition are rejected"
fi
pass "worker results without one running transition are rejected"

jq '.events.route_degraded.scope = "global" | .events.route_degraded.entity = "supervisor" | .transitions += [{"entity":"supervisor","from":"degraded","event":"route_recovered","to":"paused"}]' "$CONTRACT" >"$TMP/route-stop.json"
if validate "$TMP/route-stop.json"; then
  fail "route-local events cannot pause the supervisor"
fi
pass "route-local events cannot pause the supervisor"

jq '.queue_policy.skipped_jobs_remain_queued = false' "$CONTRACT" >"$TMP/consuming-queue.json"
if validate "$TMP/consuming-queue.json"; then
  fail "weakened non-consuming queue policy is rejected"
fi
pass "weakened non-consuming queue policy is rejected"

printf '\nALL PASS\n'
