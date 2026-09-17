#!/usr/bin/env bash
# workflow-engine.test.sh -- table-driven checks for schema_version 2 workflow
# validation and transition enforcement.
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
run_ok() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else fail_case "$desc"; fi
}
run_fail() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then fail_case "$desc"; else ok "$desc"; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for fixture in gecktopia-adr-008 protected-mainline simple-integration legacy-v1; do
  run_ok "valid fixture: $fixture" "$ENGINE" --config "$FIX/$fixture.yaml" validate-config
done
run_fail "invalid fixture: unsupported schema" "$ENGINE" --config "$FIX/invalid-unsupported-version.yaml" validate-config
run_fail "invalid fixture: undefined branch role" "$ENGINE" --config "$FIX/invalid-undefined-branch.yaml" validate-config
run_fail "invalid fixture: undefined adapter" "$ENGINE" --config "$FIX/invalid-undefined-adapter.yaml" validate-config
cp "$FIX/legacy-v1.yaml" "$TMP/invalid-trust-profile.yaml"
printf '\nworker_trust_profile: omnipotent-worker\n' >> "$TMP/invalid-trust-profile.yaml"
run_fail "invalid worker trust profile" "$ENGINE" --config "$TMP/invalid-trust-profile.yaml" validate-config

cp "$FIX/legacy-v1.yaml" "$TMP/budget-above-caps.yaml"
printf '\nllm:\n  budgets:\n    max_usd_per_run: 200\n    max_usd_per_ticket: 400\n    max_usd_per_sprint: 4000\n    max_usd_per_code_review_phase: 8\n' >> "$TMP/budget-above-caps.yaml"
BUDGET_OUT="$("$ENGINE" --config "$TMP/budget-above-caps.yaml" validate-config 2>"$TMP/budget-warnings.txt")"
eq "budget values above hard caps stay non-fatal" "0" "$?"
eq "budget cap warnings keep the validation result on stdout" "OK schema_version=1" "$BUDGET_OUT"
BUDGET_WARNINGS="$(cat "$TMP/budget-warnings.txt")"
eq "each budget value above its hard cap is reported" "WARNING llm.budgets.max_usd_per_run=200 exceeds the hard cap 10.00; Orka enforces 10.00
WARNING llm.budgets.max_usd_per_ticket=400 exceeds the hard cap 30.00; Orka enforces 30.00
WARNING llm.budgets.max_usd_per_sprint=4000 exceeds the hard cap 300.00; Orka enforces 300.00" "$BUDGET_WARNINGS"
eq "budgets within hard caps produce no warnings" "" \
  "$("$ENGINE" --config "$FIX/legacy-v1.yaml" validate-config 2>&1 >/dev/null)"

eq "branch role resolves Gecktopia candidate template" "release/2026.08.05" \
  "$("$ENGINE" --config "$FIX/gecktopia-adr-008.yaml" branch-name candidate --var candidate_id=2026.08.05)"
eq "branch role resolves custom simple topic template" "work/ABC-1-thing" \
  "$("$ENGINE" --config "$FIX/simple-integration.yaml" branch-name topic --var ticket_key=ABC-1 --var slug=thing)"
run_fail "omitted integration role is rejected only when referenced" \
  "$ENGINE" --config "$FIX/protected-mainline.yaml" branch-name integration

eq "Gecktopia state graph has ADR release states" "from=draft
to=frozen" \
  "$("$ENGINE" --config "$FIX/gecktopia-adr-008.yaml" plan-transition freeze --var candidate_id=rc1 | awk '/^(from|to)=/')"
eq "mainline fixture has different state names" "from=created
to=verified" \
  "$("$ENGINE" --config "$FIX/protected-mainline.yaml" plan-transition verify --var ticket_key=ONE --var slug=x | awk '/^(from|to)=/')"

setup_repo() {
  local name="$1" fixture="$2"
  local dir="$TMP/$name"
  mkdir -p "$dir/.orchestration"
  cp "$FIX/$fixture.yaml" "$dir/.orchestration/config.yaml"
  (cd "$dir" && git init -q && git config user.email t@t.t && git config user.name t && : > init.txt && git add init.txt && git commit -qm init)
  printf '%s' "$dir"
}

ev() {
  local dir="$1" name="$2"
  mkdir -p "$dir/evidence"
  : > "$dir/evidence/$name"
  printf '%s=%s/evidence/%s' "$name" "$dir" "$name"
}

GECK_REPO="$(setup_repo geck gecktopia-adr-008)"
cd "$GECK_REPO" || exit 1
run_ok "Gecktopia candidate initialized" "$ENGINE" init-candidate rc1 --candidate-sha sha-a
run_fail "transition refuses missing evidence" "$ENGINE" transition rc1 freeze --candidate-sha sha-a --ci integration=green
run_ok "freeze accepts transition-specific evidence and CI" "$ENGINE" transition rc1 freeze \
  --candidate-sha sha-a \
  --evidence "$(ev "$GECK_REPO" release_membership)" \
  --evidence "$(ev "$GECK_REPO" candidate_branch)" \
  --ci integration=green
run_fail "artifact identity is required before candidate verification" "$ENGINE" transition rc1 start-verification \
  --candidate-sha sha-a \
  --evidence "$(ev "$GECK_REPO" candidate_deployed)" \
  --evidence "$(ev "$GECK_REPO" artifact_recorded)" \
  --ci candidate=green
run_ok "candidate verification records exact artifact identity" "$ENGINE" transition rc1 start-verification \
  --candidate-sha sha-a --artifact-id artifact-a \
  --evidence "$(ev "$GECK_REPO" candidate_deployed)" \
  --evidence "$(ev "$GECK_REPO" artifact_recorded)" \
  --ci candidate=green
run_fail "human QA approval is required by policy" "$ENGINE" transition rc1 qa-approve \
  --candidate-sha sha-a --artifact-id artifact-a \
  --evidence "$(ev "$GECK_REPO" qa_evidence)" \
  --ci candidate=green
run_fail "non-human QA approval is rejected" "$ENGINE" record-approval rc1 qa-approve qa qa-bot independent-agent \
  --candidate-sha sha-a --artifact-id artifact-a
run_ok "human QA approval is recorded separately from state" "$ENGINE" record-approval rc1 qa-approve qa qa-user human \
  --candidate-sha sha-a --artifact-id artifact-a
run_ok "QA approval transition accepts bound human approval" "$ENGINE" transition rc1 qa-approve \
  --candidate-sha sha-a --artifact-id artifact-a \
  --evidence "$(ev "$GECK_REPO" qa_evidence)" \
  --ci candidate=green
run_fail "artifact mismatch blocks candidate merge" "$ENGINE" transition rc1 merge-candidate \
  --candidate-sha sha-a --artifact-id artifact-b \
  --evidence "$(ev "$GECK_REPO" merge_record)" \
  --ci production=green
run_ok "candidate merge requires prior QA transition and exact artifact" "$ENGINE" transition rc1 merge-candidate \
  --candidate-sha sha-a --artifact-id artifact-a \
  --evidence "$(ev "$GECK_REPO" merge_record)" \
  --ci production=green
run_fail "production promotion requires a tag" "$ENGINE" transition rc1 promote-production \
  --candidate-sha sha-a --artifact-id artifact-a \
  --evidence "$(ev "$GECK_REPO" promotion_record)"
run_ok "production promotion keeps artifact identity separate from merge" "$ENGINE" transition rc1 promote-production \
  --candidate-sha sha-a --artifact-id artifact-a --tag release/rc1 \
  --evidence "$(ev "$GECK_REPO" promotion_record)"
run_ok "release reconciliation is an explicit transition" "$ENGINE" transition rc1 reconcile \
  --candidate-sha sha-a \
  --evidence "$(ev "$GECK_REPO" reconciliation_record)"
run_ok "release cleanup closes the candidate" "$ENGINE" transition rc1 close \
  --candidate-sha sha-a \
  --evidence "$(ev "$GECK_REPO" cleanup_record)"
eq "candidate reached terminal state" "state=closed" "$(grep '^state=' .orchestration/candidates/rc1/state.env)"

run_ok "stale approval candidate initialized" "$ENGINE" init-candidate stale --candidate-sha old-sha
run_ok "stale approval freeze" "$ENGINE" transition stale freeze \
  --candidate-sha old-sha \
  --evidence "$(ev "$GECK_REPO" release_membership)" \
  --evidence "$(ev "$GECK_REPO" candidate_branch)" \
  --ci integration=green
run_ok "stale approval verification" "$ENGINE" transition stale start-verification \
  --candidate-sha old-sha --artifact-id artifact-old \
  --evidence "$(ev "$GECK_REPO" candidate_deployed)" \
  --evidence "$(ev "$GECK_REPO" artifact_recorded)" \
  --ci candidate=green
run_ok "approval bound to old candidate identity" "$ENGINE" record-approval stale qa-approve qa qa-user human \
  --candidate-sha old-sha --artifact-id artifact-old
run_ok "candidate can move to blocked" "$ENGINE" transition stale block \
  --candidate-sha old-sha \
  --evidence "$(ev "$GECK_REPO" blocking_finding)"
run_ok "configured release-fix transition updates candidate identity" "$ENGINE" transition stale resume-verification \
  --candidate-sha new-sha \
  --evidence "$(ev "$GECK_REPO" fix_merged_to_integration)" \
  --evidence "$(ev "$GECK_REPO" candidate_updated)" \
  --ci candidate=green
run_fail "approval becomes stale after candidate identity changes" "$ENGINE" transition stale qa-approve \
  --candidate-sha new-sha --artifact-id artifact-old \
  --evidence "$(ev "$GECK_REPO" qa_evidence)" \
  --ci candidate=green
run_ok "manual-edit candidate initialized" "$ENGINE" init-candidate skip --candidate-sha sha-skip
printf 'candidate_id=skip\nstate=qa-approved\ncandidate_sha=sha-skip\nartifact_id=artifact-skip\n' \
  > .orchestration/candidates/skip/state.env
run_fail "manual state edit cannot skip required transitions" "$ENGINE" transition skip merge-candidate \
  --candidate-sha sha-skip --artifact-id artifact-skip \
  --evidence "$(ev "$GECK_REPO" merge_record)" \
  --ci production=green
cd "$ROOT" || exit 1

MAINLINE_REPO="$(setup_repo mainline protected-mainline)"
cd "$MAINLINE_REPO" || exit 1
run_ok "mainline candidate initialized without integration branch" "$ENGINE" init-candidate main1
run_ok "mainline verification omits human approval by policy" "$ENGINE" transition main1 verify \
  --evidence "$(ev "$MAINLINE_REPO" review_notes)" \
  --ci review=green
run_ok "mainline merge uses only configured production branch" "$ENGINE" transition main1 merge \
  --evidence "$(ev "$MAINLINE_REPO" merge_record)"
cd "$ROOT" || exit 1

run_fail "legacy config refuses configurable transition commands" \
  "$ENGINE" --config "$FIX/legacy-v1.yaml" plan-transition freeze

# --- Prettier-formatted flow sequences -----------------------------------------
# Prettier rewraps long flow lists onto the line after the key, or across lines.
# js-yaml accepts every form below, so the engine must parse each one exactly as
# it parses the equivalent one-line list.
parsed_json() {
  python3 - "$ENGINE" "$1" 2>&1 <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("orka_engine_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.load_simple_yaml(__import__("pathlib").Path(sys.argv[2])), sort_keys=True))
PY
}

cp "$FIX/legacy-v1.yaml" "$TMP/flow-one-line.yaml"
cat >> "$TMP/flow-one-line.yaml" <<'YAML'
llm:
  roles:
    implementer:
      allowed_tools: [read_file, search, git_diff, git_status, run_check, apply_patch]
      effort: high
security_required_when: [migrations/, "SECURITY DEFINER", 'a, b', "x [y]", "c # d"]
security_required_source_branches: ["hotfix/**"]
YAML
cp "$FIX/legacy-v1.yaml" "$TMP/flow-prettier.yaml"
cat >> "$TMP/flow-prettier.yaml" <<'YAML'
llm:
  roles:
    implementer:
      allowed_tools:
        [read_file, search, git_diff, git_status, run_check, apply_patch]
      effort: high
security_required_when: # comment after the key
  [
    migrations/, # inline comment inside the list
    "SECURITY DEFINER",

    # a full-line comment inside the list
    'a, b',
    "x [y]",
    "c # d",
  ]
security_required_source_branches: [
    "hotfix/**",
  ]
YAML
cp "$FIX/legacy-v1.yaml" "$TMP/flow-wrapped.yaml"
cat >> "$TMP/flow-wrapped.yaml" <<'YAML'
llm:
  roles:
    implementer:
      allowed_tools: [read_file, search, git_diff,
        git_status, run_check, apply_patch,]
      effort: high
security_required_when: [migrations/, "SECURITY DEFINER",
  'a, b', "x [y]",
  "c # d"]
security_required_source_branches:
  - hotfix/**
YAML
run_ok "one-line flow sequences validate" "$ENGINE" --config "$TMP/flow-one-line.yaml" validate-config
run_ok "Prettier next-line flow sequence under a role map validates" "$ENGINE" --config "$TMP/flow-prettier.yaml" validate-config
run_ok "flow sequence wrapped across lines validates" "$ENGINE" --config "$TMP/flow-wrapped.yaml" validate-config
eq "quoted flow items keep commas, brackets, and hashes" \
  '["migrations/", "SECURITY DEFINER", "a, b", "x [y]", "c # d"]' \
  "$(parsed_json "$TMP/flow-one-line.yaml" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["security_required_when"]))')"
eq "Prettier next-line form parses identically to the one-line form" \
  "$(parsed_json "$TMP/flow-one-line.yaml")" "$(parsed_json "$TMP/flow-prettier.yaml")"
eq "wrapped flow form with trailing comma parses identically to the one-line form" \
  "$(parsed_json "$TMP/flow-one-line.yaml")" "$(parsed_json "$TMP/flow-wrapped.yaml")"
printf 'diff --git a/x b/x\n+++ b/db/migrations/1.sql\n+select 1\n' > "$TMP/flow.diff"
eq "security gate reads Prettier triggers identically to one-line triggers" \
  "$("$ENGINE" --config "$TMP/flow-one-line.yaml" security-gate --source-branch hotfix/a --target-branch develop --diff-file "$TMP/flow.diff")" \
  "$("$ENGINE" --config "$TMP/flow-prettier.yaml" security-gate --source-branch hotfix/a --target-branch develop --diff-file "$TMP/flow.diff")"

cp "$FIX/legacy-v1.yaml" "$TMP/flow-unterminated.yaml"
cat >> "$TMP/flow-unterminated.yaml" <<'YAML'
security_required_when: [migrations/,
  auth
gates:
  - code-review
YAML
run_fail "unterminated flow sequence is refused" "$ENGINE" --config "$TMP/flow-unterminated.yaml" validate-config
unterminated_err="$("$ENGINE" --config "$TMP/flow-unterminated.yaml" validate-config 2>&1 >/dev/null)"
case "$unterminated_err" in
  *"line 13"*unterminated*) ok "unterminated flow sequence refusal names the opening line" ;;
  *) fail_case "unterminated flow sequence refusal names the opening line: $unterminated_err" ;;
esac
cp "$FIX/legacy-v1.yaml" "$TMP/flow-eof.yaml"
printf 'gates: [code-review,\n  security-review\n' >> "$TMP/flow-eof.yaml"
run_fail "flow sequence left open at end of file is refused" "$ENGINE" --config "$TMP/flow-eof.yaml" validate-config
cp "$FIX/legacy-v1.yaml" "$TMP/flow-map.yaml"
printf 'merge_guard: {block_squash: true}\n' >> "$TMP/flow-map.yaml"
flow_map_err="$("$ENGINE" --config "$TMP/flow-map.yaml" validate-config 2>&1 >/dev/null)"
case "$flow_map_err" in
  *"line 13"*"flow mapping"*hint:*) ok "flow mapping is refused with its line and a block-form hint" ;;
  *) fail_case "flow mapping is refused with its line and a block-form hint: $flow_map_err" ;;
esac
cp "$FIX/legacy-v1.yaml" "$TMP/flow-nested.yaml"
printf 'gates: [code-review, [security-review]]\n' >> "$TMP/flow-nested.yaml"
nested_err="$("$ENGINE" --config "$TMP/flow-nested.yaml" validate-config 2>&1 >/dev/null)"
case "$nested_err" in
  *"line 13"*nested*hint:*) ok "nested flow sequence is refused with its line and a hint" ;;
  *) fail_case "nested flow sequence is refused with its line and a hint: $nested_err" ;;
esac

if rg -n "Vercel|Supabase|TestFlight|Play Console|Jira|GECK" "$ROOT/scripts/orchestration-engine.py" >/dev/null; then
  fail_case "provider or ticket prefix leaked into core engine"
else
  ok "provider-specific names do not leak into core engine"
fi

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
