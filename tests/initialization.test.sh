#!/usr/bin/env bash
# initialization.test.sh -- default scaffolding must remain legacy-safe and
# initialization adapters must validate through the shared engine.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
PREFLIGHT_REPO="$(mktemp -d)"
INCOMPLETE_PLUGIN="$(mktemp -d)"
UNSUPPORTED_ROUTE_REPO="$(mktemp -d)"
FAKE_BIN="$(mktemp -d)"
trap 'rm -rf "$PREFLIGHT_REPO" "$INCOMPLETE_PLUGIN" "$UNSUPPORTED_ROUTE_REPO" "$FAKE_BIN"' EXIT
mkdir -p "$PREFLIGHT_REPO/.orchestration"
cp "$ROOT/templates/config.yaml" "$PREFLIGHT_REPO/.orchestration/config.yaml"
mkdir -p "$UNSUPPORTED_ROUTE_REPO/.orchestration"
python3 - "$ROOT/templates/config.yaml" "$UNSUPPORTED_ROUTE_REPO/.orchestration/config.yaml" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
Path(sys.argv[2]).write_text(source.replace("provider: openai", "provider: anthropic", 1))
PY
printf '#!/bin/sh\nexit 0\n' > "$FAKE_BIN/claude"
printf '#!/bin/sh\n[ "$1 $2" = "login status" ] && echo "Logged in using ChatGPT"\nexit 0\n' > "$FAKE_BIN/codex"
chmod +x "$FAKE_BIN/claude" "$FAKE_BIN/codex"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else fail_case "$desc"; fi
}
check_not() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then fail_case "$desc"; else ok "$desc"; fi
}

check "template declares legacy schema by default" \
  grep -Eq '^schema_version:[[:space:]]*1([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template defaults to the portable cooperative worker profile" \
  grep -Eq '^worker_trust_profile:[[:space:]]*cooperative-worker([[:space:]]|$)' "$ROOT/templates/config.yaml"
check_not "template does not actively enable schema v2" \
  grep -Eq '^schema_version:[[:space:]]*2([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template leaves integration branch for repo detection" \
  grep -Eq '^integration_branch:[[:space:]]*""' "$ROOT/templates/config.yaml"
check "template leaves production branch for repo detection" \
  grep -Eq '^production_branch:[[:space:]]*""' "$ROOT/templates/config.yaml"
check "template defaults worktree cleanup to manual" \
  grep -Eq '^worktree_cleanup:[[:space:]]*manual([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template preserves desktop LLM execution by default" \
  grep -Eq '^[[:space:]]+execution:[[:space:]]*desktop([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template exposes optional per-role LLM routes" \
  rg -q '^[[:space:]]+roles:' "$ROOT/templates/config.yaml"
check "template gives API runs a hard USD ceiling" \
  rg -q '^[[:space:]]+max_usd_per_run:' "$ROOT/templates/config.yaml"
check "reviewers use ledger-issued phase permits without config bypass" \
  sh -c '! grep -q "require_review_authorization" "$1" && grep -q "permit-review" "$2"' _ \
  "$ROOT/templates/config.yaml" "$ROOT/skills/gate-pr/SKILL.md"
check "template sets ticket warning and pause thresholds" \
  rg -q '^[[:space:]]+pause_usd_per_ticket:' "$ROOT/templates/config.yaml"
check "template bounds model and reviewer run counts" \
  rg -q '^[[:space:]]+max_reviewer_runs_per_ticket:' "$ROOT/templates/config.yaml"
check "template bounds lane relaunches" \
  grep -Eq '^max_lane_relaunches:[[:space:]]*[0-9]+' "$ROOT/templates/config.yaml"
check "template drains preserved PR work before fresh tickets" \
  grep -Eq '^pr_drain_first:[[:space:]]*true([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template keeps automatic preserved PR recovery opt-in" \
  grep -Eq '^preserved_pr_auto_recovery:[[:space:]]*false([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template exposes optional slice contracts without project policy" \
  grep -Eq '^[[:space:]]+required_slice_contracts:[[:space:]]*\[\]([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template requires explicit model pricing" \
  rg -q '^[[:space:]]+pricing:' "$ROOT/templates/config.yaml"
check "template configures an active Jira sprint by default" \
  grep -Eq '^sprint_id:[[:space:]]*active([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template keeps sprint checkpoints under orchestration runtime state" \
  grep -Eq '^sprint_checkpoint_dir:[[:space:]]*\.orchestration/\.sprint-state([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template defaults captain updates to event-driven" \
  grep -Eq '^sprint_status_update_mode:[[:space:]]*event([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template keeps a bounded captain heartbeat" \
  grep -Eq '^sprint_status_heartbeat_minutes:[[:space:]]*30([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template declares directional Jira dependency mapping" \
  rg -q '^sprint_dependency_links:' "$ROOT/templates/config.yaml"
check "Claude init command validates through shared engine" \
  rg -q 'orchestration-engine\.py validate-config' "$ROOT/commands/orchestration-init.md"
check "Codex init skill validates through shared engine" \
  rg -q 'orchestration-engine\.py validate-config' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init runs plugin-owned conformance tests" \
  rg -q 'run-plugin-conformance\.sh' "$ROOT/commands/orchestration-init.md"
check "Codex init runs plugin-owned conformance tests" \
  rg -q 'run-plugin-conformance\.sh' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init gitignores sprint checkpoints" \
  rg -q '\.orchestration/\.sprint-state/' "$ROOT/commands/orchestration-init.md"
check "Codex init gitignores sprint checkpoints" \
  rg -q '\.orchestration/\.sprint-state/' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init gitignores API run state" \
  rg -q '\.orchestration/\.llm-runs/' "$ROOT/commands/orchestration-init.md"
  rg -q '\.orchestration/\.review-results/' "$ROOT/commands/orchestration-init.md"
  rg -q '\.orchestration/\.review-results/' "$ROOT/skills/orchestration-init/SKILL.md"
check "Codex init gitignores API usage state" \
  rg -q '\.orchestration/\.llm-usage/' "$ROOT/skills/orchestration-init/SKILL.md"
check "Claude init gitignores repository API credentials" \
  rg -q '\.orchestration/\.env' "$ROOT/commands/orchestration-init.md"
check "Codex init gitignores repository API credentials" \
  rg -q '\.orchestration/\.env' "$ROOT/skills/orchestration-init/SKILL.md"
check "API docs preserve container secret precedence" \
  rg -q 'take precedence, making platform secret injection' "$ROOT/docs/api-agent.md"
check_not "Claude init does not copy process docs into target repos" \
  rg -q 'Copy `templates/ORCHESTRATION\.md`' "$ROOT/commands/orchestration-init.md"
check_not "Codex init does not copy process docs into target repos" \
  rg -q 'Copy `templates/ORCHESTRATION\.md`' "$ROOT/skills/orchestration-init/SKILL.md"
check_not "Claude init does not vendor hooks into project settings" \
  rg -q 'Add .*hooks.*\.claude/settings\.json' "$ROOT/commands/orchestration-init.md"
check_not "Codex init does not vendor hooks into project settings" \
  rg -q 'add `hooks/hooks\.json` entries to `\.claude/settings\.json`' "$ROOT/skills/orchestration-init/SKILL.md"
check "plugin conformance runner owns merge-guard suite" \
  rg -q 'merge-guard\.test\.sh' "$ROOT/scripts/run-plugin-conformance.sh"
check "plugin conformance runner owns worktree-cleanup suite" \
  rg -q 'worktree\.test\.sh' "$ROOT/scripts/run-plugin-conformance.sh"
check "plugin conformance runner owns host-parity suite" \
  rg -q 'plugin-parity\.test\.sh' "$ROOT/scripts/run-plugin-conformance.sh"
check_runtime_unverified() {
  python3 "$ROOT/scripts/captain-preflight.py" --plugin-root "$ROOT" --repo "$PREFLIGHT_REPO" --host codex > "$PREFLIGHT_REPO/preflight.json"
  local result=$?
  python3 - "$PREFLIGHT_REPO/preflight.json" "$result" <<'CHECK'
import json,sys
v=json.load(open(sys.argv[1]))
assert sys.argv[2]=='2' and v['installation_status']=='ready' and not v['execution_ready']
CHECK
}
check "captain separates installed plugin from unverified provider readiness" check_runtime_unverified
check_runtime_verified_subscription() {
  PATH="$FAKE_BIN:$PATH" python3 "$ROOT/scripts/captain-preflight.py" \
    --plugin-root "$ROOT" --repo "$PREFLIGHT_REPO" --host claude \
    --verify-runtime > "$PREFLIGHT_REPO/subscription-preflight.json"
  python3 - "$PREFLIGHT_REPO/subscription-preflight.json" <<'CHECK'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['status']=='ready' and v['execution_ready']
assert all(x['state']=='healthy' and x['mode']=='subscription' for x in v['routes'])
CHECK
}
check "verified preflight accepts model-less desktop subscription routes" \
  check_runtime_verified_subscription
check_unsupported_claude_route() {
  ! PATH="$FAKE_BIN:$PATH" python3 "$ROOT/scripts/captain-preflight.py" \
    --plugin-root "$ROOT" --repo "$UNSUPPORTED_ROUTE_REPO" --host claude \
    --verify-runtime > "$UNSUPPORTED_ROUTE_REPO/blocked-preflight.json"
  python3 - "$UNSUPPORTED_ROUTE_REPO/blocked-preflight.json" <<'CHECK'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['status']=='blocked' and not v['execution_ready']
assert any(x['state']=='incompatible' and 'explicit model' in x['reason'] for x in v['routes'])
CHECK
}
check "model-less Claude preflight fails closed with structured status" \
  check_unsupported_claude_route
check "captain preflight fails when the active plugin is incomplete" \
  sh -c '! python3 "$1/scripts/captain-preflight.py" --plugin-root "$2" --repo "$3" --host claude' sh "$ROOT" "$INCOMPLETE_PLUGIN" "$PREFLIGHT_REPO"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
