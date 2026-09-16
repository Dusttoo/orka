#!/usr/bin/env bash
# plugin-parity.test.sh -- Claude commands and Codex skills must expose the same
# configured workflow operations through the shared engine.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
ENGINE="$ROOT/scripts/orchestration-engine.py"
FIX="$HERE/fixtures/workflows"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else
    printf 'FAIL %s\n     want: [%s]\n     got:  [%s]\n' "$1" "$2" "$3"
    fails=$((fails + 1))
  fi
}
contains_engine() {
  local file="$1"
  if rg -q 'orchestration-engine\.py' "$ROOT/$file"; then ok "$file uses shared engine"
  else fail_case "$file does not reference shared engine"; fi
}

contains_engine commands/release.md
contains_engine skills/release-integration/SKILL.md
contains_engine commands/orchestrate.md
contains_engine skills/orchestrate-ticket/SKILL.md
contains_engine commands/gate.md
contains_engine skills/gate-pr/SKILL.md

if python3 - "$ROOT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
claude = json.loads((root / ".claude-plugin/plugin.json").read_text())
codex = json.loads((root / ".codex-plugin/plugin.json").read_text())
marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
assert claude["name"] == codex["name"] == marketplace["plugins"][0]["name"] == "orka"
assert claude["version"] == codex["version"]
assert codex["interface"]["displayName"] == "Orka"
assert codex["repository"] == "https://github.com/Dusttoo/orka"
PY
then
  ok "Claude, Codex, and marketplace manifests share the Orka identity"
else
  fail_case "plugin manifests disagree on the Orka identity"
fi

retired_slug="claude"'-orchestrator'
retired_title="claude"' orchestrator'
if rg -i --hidden -g '!.git' -g '!.git/**' -g '!tests/fixtures/**' \
  "$retired_slug|$retired_title|Dusttoo/$retired_slug" "$ROOT" >/dev/null; then
  fail_case "retired plugin branding remains in the source tree"
else
  ok "retired plugin branding is absent from the source tree"
fi

contains_contract() {
  local label="$1" pattern="$2"; shift 2
  local file
  for file in "$@"; do
    if ! rg -q -- "$pattern" "$ROOT/$file"; then
      fail_case "$label missing from $file"
      return
    fi
  done
  ok "$label present in Claude and Codex adapters"
}

contains_contract "shared sprint controller" 'sprint-controller\.py' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "sprint restart reconciliation" 'needs_reconcile' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "atomic sprint reservation" 'reserve' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "controller-owned local launch" 'launch-local --sprint <id> --ticket <key>' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "launch evidence attach" 'attach --sprint <id> --ticket <key> --launch-evidence <launch_evidence>' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "Codex worker prompt uses controller stdin" '--stdin-file <checkpoint-dir>/<run-ref>\.prompt' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "Codex stdin sentinel" '--cd <repository> -' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "adapter-owned Jira fetch" 'sync --inventory-template' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "adapter-owned provider batch submission" 'submit-batch --batch' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "per-ticket workflow dispatch" 'orchestrate' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "three-way sprint summary" 'user-action' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "optional sprint ticket priority ordering" 'priority' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "pruned Jira field requests" 'key,summary,status,priority,subtasks,parent,issuelinks' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "sanitized Jira context" 'sanitize-jira' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "asynchronous Anthropic sprint batches" 'prepare-batch' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "per-role sprint execution routing" 'context_pipeline\.py route' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "budgeted API sprint runner" 'api_agent\.py run' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "event-driven quiet captain" 'sprint_status_heartbeat_minutes' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "root-authorized bounded ticket continuation" 'grant-budget' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "root-authorized terminal recovery" 'recover-terminal' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "root-authorized ticket relaunch ceiling" 'grant-relaunch' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "root-authorized PR review continuation" 'authorize-repair' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "autonomous ticket decomposition" 'jira_decomposition\.py' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "fresh ticket scoper preserves captain context" 'ticket-scoper' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "progress-aware sprint watchdog" 'record-progress' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "recoverable sprint queues" 'plan\.recovery' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "verified preserved PR recovery" 'reconcile-preserved-pr' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "repository decision reuse" 'sprint_decisions' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md
contains_contract "operator capability stdin hygiene" 'operator-capability-stdin' \
  commands/orchestrate-sprint.md skills/orchestrate-sprint/SKILL.md

contains_contract "durable review ledger" 'review-ledger\.py' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "mechanical security gate decision" 'orchestration-engine\.py security-gate' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "structured reviewer result recording" '--result' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "bounded review loop" 'escalate-human' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "per-role ticket execution routing" 'context_pipeline\.py route' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md
contains_contract "per-role gate execution routing" 'context_pipeline\.py route' \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "constrained API ticket runner" 'api_agent\.py run' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "safe pre-acknowledgement fallback" 'uncertain' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md

contains_contract "host-neutral merge evidence" 'plugin version' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md \
  commands/gate.md skills/gate-pr/SKILL.md
contains_contract "explicit cleanup policy" 'worktree_cleanup' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md
contains_contract "hooks are optional" 'defense in depth' \
  commands/orchestrate.md skills/orchestrate-ticket/SKILL.md

contains_contract "usage reporting entry point" 'api_agent\.py report' \
  commands/orchestration-report.md skills/orchestration-report/SKILL.md
contains_contract "reporting states its desktop coverage limit" 'execution: desktop' \
  commands/orchestration-report.md skills/orchestration-report/SKILL.md

if rg -q 'merge-guard\.sh" --assert-green "\$PR" "\$BRANCH"' "$ROOT/scripts/merge-on-green.sh"; then
  ok "sanctioned merge enforces evidence independently of host hooks"
else
  fail_case "sanctioned merge does not enforce evidence independently of host hooks"
fi

if rg -n '(^|[ `])scripts/[A-Za-z0-9_-]+\.(sh|py)' "$ROOT/commands" -g '*.md' >/dev/null; then
  fail_case "Claude commands contain target-relative plugin script paths"
else
  ok "Claude commands resolve every plugin script through CLAUDE_PLUGIN_ROOT"
fi

compare_plan() {
  local fixture="$1" transition="$2"; shift 2
  local claude codex
  claude="$("$ENGINE" --config "$FIX/$fixture.yaml" adapter-plan --host claude "$transition" "$@")"
  codex="$("$ENGINE" --config "$FIX/$fixture.yaml" adapter-plan --host codex "$transition" "$@")"
  eq "Claude/Codex adapter plan parity: $fixture:$transition" "$claude" "$codex"
}

compare_plan gecktopia-adr-008 freeze --var candidate_id=rc1
compare_plan gecktopia-adr-008 promote-production --var candidate_id=rc1
compare_plan protected-mainline verify --var ticket_key=ONE --var slug=x
compare_plan simple-integration merge --var ticket_key=ONE --var slug=x

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
