#!/usr/bin/env bash
# lib-config.test.sh -- smoke tests for the config parser. No framework: it sets
# up a temp repo with a known config, exercises each reader, and asserts output.
# Run: bash tests/lib-config.test.sh   (exits non-zero on any failure)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../scripts/lib-config.sh"

fails=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n     want: [%s]\n     got:  [%s]\n' "$1" "$2" "$3"; fails=$((fails + 1)); }
eq()   { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.orchestration"
cat > "$TMP/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
production_branch: main
concurrency_max: 2
ci_checks_integration:
  - TypeScript
  - Vitest
  - Build
gates:
  - code-review
  - security-review
self_check:
  - name: typecheck
    run: npx tsc --noEmit
  - name: no-hex
    run: '! grep -rqn "#[0-9a-fA-F]\{3,\}" src'
verification:
  - name: e2e
    run: npx playwright test
YAML
cd "$TMP" && git init -q >/dev/null

# shellcheck source=../scripts/lib-config.sh
. "$LIB"

eq "scalar: integration_branch" "develop" "$(orch_get integration_branch)"
eq "scalar: production_branch"   "main"    "$(orch_get production_branch)"
eq "scalar: default when absent" "X"       "$(orch_get nope X)"

eq "list: ci_checks_integration count" "3" "$(orch_list ci_checks_integration | grep -c .)"
eq "list: ci_checks first"             "TypeScript" "$(orch_list ci_checks_integration | head -1)"
eq "list: gates stops before self_check" "security-review" "$(orch_list gates | tail -1)"

eq "selfchecks: count"        "2" "$(orch_selfchecks | grep -c .)"
eq "selfchecks: first name"   "typecheck" "$(orch_selfchecks | head -1 | cut -f1)"
eq "selfchecks: first run"    "npx tsc --noEmit" "$(orch_selfchecks | head -1 | cut -f2)"
# The single-quoted literal-backslash run survives verbatim after outer-quote strip.
eq "selfchecks: literal backslash run" '! grep -rqn "#[0-9a-fA-F]\{3,\}" src' \
   "$(orch_selfchecks | sed -n 2p | cut -f2)"

eq "named: verification e2e run" "npx playwright test" "$(orch_named verification e2e run)"
eq "named: missing entry is empty" "" "$(orch_named verification nope run)"
eq "named: missing field is empty" "" "$(orch_named verification e2e when)"

# --- flow sequences: every list reader agrees with the block form ----------------
# Prettier rewraps long flow lists onto the next line or across lines. A reader
# that silently returns nothing (or its defaults) for those forms would diverge
# from the engine, so each list reader is checked against the block-list result.
FLOW_ROOT="$TMP/flow"
mkdir -p "$FLOW_ROOT/.orchestration"
(cd "$FLOW_ROOT" && git init -q >/dev/null)
write_flow_config() {
  case "$1" in
    block) cat > "$FLOW_ROOT/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
gates:
  - code-review
  - security-review
sprint_ready_statuses:
  - Ready
  - "Selected, for Development"
jira_priority_order:
  - Highest
  - Low
YAML
    ;;
    one-line) cat > "$FLOW_ROOT/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
gates: [code-review, security-review]
sprint_ready_statuses: [Ready, "Selected, for Development"]
jira_priority_order: [Highest, Low]
YAML
    ;;
    prettier) cat > "$FLOW_ROOT/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
gates:
  [code-review, security-review]
sprint_ready_statuses: # statuses a worker may start from
  [
    Ready, # comment inside the list
    "Selected, for Development",
  ]
jira_priority_order: [Highest,
  Low]
YAML
    ;;
  esac
}
flow_readers() {
  (
    cd "$FLOW_ROOT" || exit 1
    printf 'orch_list gates: %s\n' "$(orch_list gates | paste -sd '|' -)"
    printf 'orch_list sprint_ready_statuses: %s\n' "$(orch_list sprint_ready_statuses | paste -sd '|' -)"
    python3 - "$HERE/../scripts" "$FLOW_ROOT/.orchestration/config.yaml" <<'PY'
import importlib.util, sys
from pathlib import Path
scripts, config = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(scripts))
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, scripts / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
engine = load("flow_engine", "orchestration-engine.py")
parsed = engine.load_simple_yaml(config)
print("engine gates:", "|".join(parsed["gates"]))
print("engine sprint_ready_statuses:", "|".join(parsed["sprint_ready_statuses"]))
print("engine jira_priority_order:", "|".join(parsed["jira_priority_order"]))
controller = load("flow_controller", "sprint-controller.py")
print("sprint-controller config_list:", "|".join(controller.config_list(config, "sprint_ready_statuses", ["DEFAULT"])))
inventory = load("flow_inventory", "jira_inventory_fetch.py")
print("jira_inventory_fetch list_config:", "|".join(inventory.list_config(config, "jira_priority_order", ["DEFAULT"])))
PY
  ) 2>&1
}
write_flow_config block
FLOW_BLOCK="$(flow_readers)"
eq "flow readers: block baseline is complete" "orch_list gates: code-review|security-review
orch_list sprint_ready_statuses: Ready|Selected, for Development
engine gates: code-review|security-review
engine sprint_ready_statuses: Ready|Selected, for Development
engine jira_priority_order: Highest|Low
sprint-controller config_list: Ready|Selected, for Development
jira_inventory_fetch list_config: Highest|Low" "$FLOW_BLOCK"
write_flow_config one-line
eq "flow readers: one-line flow lists agree with block lists" "$FLOW_BLOCK" "$(flow_readers)"
write_flow_config prettier
eq "flow readers: Prettier-wrapped flow lists agree with block lists" "$FLOW_BLOCK" "$(flow_readers)"
printf 'gates: [code-review,\n  security-review\nintegration_branch: develop\n' > "$FLOW_ROOT/.orchestration/config.yaml"
if (cd "$FLOW_ROOT" && orch_list gates >/dev/null 2>&1); then
  bad "flow readers: orch_list refuses an unterminated flow list" "non-zero exit" "exit 0"
else
  ok "flow readers: orch_list refuses an unterminated flow list"
fi

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
