#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
HELPER="$ROOT/host-tools/orchestration-recovery-authority.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo"
export ORCHESTRATION_AUTHORITY_TEST_MODE=1
export ORCHESTRATION_AUTHORITY_STATE_DIR="$TMP/state"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

budget_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"budget","repository":str(pathlib.Path(sys.argv[1]).resolve()),"ticket":"PROJ-1"},sort_keys=True,separators=(",",":")))' "$TMP/repo")"
recovery_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"recovery","repository":str(pathlib.Path(sys.argv[1]).resolve()),"ticket":"PROJ-1","attempt":2},sort_keys=True,separators=(",",":")))' "$TMP/repo")"
relaunch_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"relaunch","repository":str(pathlib.Path(sys.argv[1]).resolve()),"ticket":"PROJ-1"},sort_keys=True,separators=(",",":")))' "$TMP/repo")"
review_repair_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"review-repair","repository":str(pathlib.Path(sys.argv[1]).resolve()),"pr":"50"},sort_keys=True,separators=(",",":")))' "$TMP/repo")"

budget_token="$($HELPER issue-budget --repository "$TMP/repo" --ticket PROJ-1 --ceiling-usd 35.25)"
if printf '%s\n' "$budget_token" | "$HELPER" activate-budget --scope "$budget_scope" | grep -qx 35.25; then
  ok "budget capability activates its exact absolute ceiling"
else bad "budget capability activates its exact absolute ceiling"; fi
if "$HELPER" budget-ceiling --scope "$budget_scope" | grep -qx 35.25; then
  ok "active budget ceiling remains queryable"
else bad "active budget ceiling remains queryable"; fi
if printf '%s\n' "$budget_token" | "$HELPER" activate-budget --scope "$budget_scope" >/dev/null 2>&1; then
  bad "budget capability is one-shot"
else ok "budget capability is one-shot"; fi

recovery_token="$($HELPER issue-recovery --repository "$TMP/repo" --ticket PROJ-1 --attempt 2)"
if printf '%s\n' "$recovery_token" | "$HELPER" consume-recovery --scope "$recovery_scope"; then
  ok "recovery capability consumes for its exact attempt"
else bad "recovery capability consumes for its exact attempt"; fi
if printf '%s\n' "$recovery_token" | "$HELPER" consume-recovery --scope "$recovery_scope" >/dev/null 2>&1; then
  bad "recovery capability is one-shot"
else ok "recovery capability is one-shot"; fi

review_repair_token="$($HELPER issue-review-repair --repository "$TMP/repo" --pr 50 --ceiling-repair-cycles 3 --reason 'operator approved one bounded repair')"
review_repair_result="$(printf '%s\n' "$review_repair_token" | "$HELPER" activate-review-repair --scope "$review_repair_scope")"
if python3 -c 'import json,sys; v=json.load(sys.stdin); assert v["ceiling_repair_cycles"] == 3 and v["reason"]' <<<"$review_repair_result"; then
  ok "review repair capability activates its exact PR-bound ceiling"
else bad "review repair capability activates its exact PR-bound ceiling"; fi
if "$HELPER" review-repair-grant --scope "$review_repair_scope" | \
  python3 -c 'import json,sys; assert json.load(sys.stdin)["ceiling_repair_cycles"] == 3'; then
  ok "active review repair grant remains queryable"
else bad "active review repair grant remains queryable"; fi
if printf '%s\n' "$review_repair_token" | "$HELPER" activate-review-repair --scope "$review_repair_scope" >/dev/null 2>&1; then
  bad "review repair capability is one-shot"
else ok "review repair capability is one-shot"; fi
$HELPER revoke-review-repair --repository "$TMP/repo" --pr 50
if "$HELPER" review-repair-grant --scope "$review_repair_scope" >/dev/null 2>&1; then
  bad "revoked review repair grant is unavailable"
else ok "revoked review repair grant is unavailable"; fi

relaunch_token="$($HELPER issue-relaunch --repository "$TMP/repo" --ticket PROJ-1 --ceiling-attempts 4)"
if printf '%s\n' "$relaunch_token" | "$HELPER" activate-relaunch --scope "$relaunch_scope" | grep -qx 4; then
  ok "relaunch capability activates its exact absolute attempt ceiling"
else bad "relaunch capability activates its exact absolute attempt ceiling"; fi
if "$HELPER" relaunch-ceiling --scope "$relaunch_scope" | grep -qx 4; then
  ok "active relaunch ceiling remains queryable"
else bad "active relaunch ceiling remains queryable"; fi
if printf '%s\n' "$relaunch_token" | "$HELPER" activate-relaunch --scope "$relaunch_scope" >/dev/null 2>&1; then
  bad "relaunch capability is one-shot"
else ok "relaunch capability is one-shot"; fi
run_lower="$($HELPER issue-relaunch --repository "$TMP/repo" --ticket PROJ-1 --ceiling-attempts 3)"
if printf '%s\n' "$run_lower" | "$HELPER" activate-relaunch --scope "$relaunch_scope" | grep -qx 4; then
  ok "a lower relaunch grant cannot narrow an active higher ceiling"
else bad "a lower relaunch grant cannot narrow an active higher ceiling"; fi
$HELPER revoke-relaunch --repository "$TMP/repo" --ticket PROJ-1
if "$HELPER" relaunch-ceiling --scope "$relaunch_scope" >/dev/null 2>&1; then
  bad "revoked relaunch ceiling is unavailable"
else ok "revoked relaunch ceiling is unavailable"; fi

mkdir "$TMP/symlink-target"
ln -s "$TMP/symlink-target" "$TMP/symlink-state"
ORCHESTRATION_AUTHORITY_STATE_DIR="$TMP/symlink-state" \
  "$HELPER" issue-budget --repository "$TMP/repo" --ticket PROJ-1 \
  --ceiling-usd 40 >/dev/null 2>&1
if [ "$?" -eq 0 ]; then
  bad "authority rejects a symlinked state root"
else
  ok "authority rejects a symlinked state root"
fi

if "$HELPER" issue-budget --repository "$TMP/repo" --ticket 'not-a-ticket' \
  --ceiling-usd 40 >/dev/null 2>&1; then
  bad "authority rejects a non-canonical ticket scope"
else
  ok "authority rejects a non-canonical ticket scope"
fi
if "$HELPER" issue-budget --repository "$TMP/repo" --ticket PROJ-1 \
  --ceiling-usd NaN >/dev/null 2>&1; then
  bad "authority rejects a non-finite budget ceiling"
else
  ok "authority rejects a non-finite budget ceiling"
fi
if "$HELPER" issue-relaunch --repository "$TMP/repo" --ticket PROJ-1 \
  --ceiling-attempts 0 >/dev/null 2>&1; then
  bad "authority rejects a non-positive relaunch ceiling"
else
  ok "authority rejects a non-positive relaunch ceiling"
fi
if "$HELPER" issue-review-repair --repository "$TMP/repo" --pr not-a-pr \
  --ceiling-repair-cycles 3 --reason approved >/dev/null 2>&1; then
  bad "authority rejects a non-canonical review PR scope"
else
  ok "authority rejects a non-canonical review PR scope"
fi
if "$HELPER" issue-review-repair --repository "$TMP/repo" --pr 50 \
  --ceiling-repair-cycles 0 --reason approved >/dev/null 2>&1; then
  bad "authority rejects a non-positive review repair ceiling"
else
  ok "authority rejects a non-positive review repair ceiling"
fi

# --- standing repository budget policy ---------------------------------------
if ORCHESTRATION_AUTHORITY_STATE_DIR="$TMP/never-created" "$HELPER" budget-policy \
  --scope "$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"budget-policy","repository":str(pathlib.Path(sys.argv[1]).resolve())},sort_keys=True,separators=(",",":")))' "$TMP/repo")" >/dev/null 2>&1; then
  bad "an authority with no state holds no budget policy"
elif [ "$?" -eq 3 ]; then ok "an authority with no state holds no budget policy"
else bad "an authority with no state exits 3 for budget-policy"; fi
policy_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"budget-policy","repository":str(pathlib.Path(sys.argv[1]).resolve())},sort_keys=True,separators=(",",":")))' "$TMP/repo")"
if "$HELPER" budget-policy --scope "$policy_scope" >/dev/null 2>&1; then
  bad "no budget policy is reported before one is set"
elif [ "$?" -eq 3 ]; then ok "no budget policy is reported before one is set"
else bad "an absent budget policy exits 3"; fi
cat > "$TMP/policy.json" <<'JSON'
{"max_usd_per_run":"50","max_usd_per_ticket":"400","max_usd_per_sprint":"4000","pause_usd_per_ticket":"300","max_usd_per_code_review_phase":"25","max_model_runs_per_ticket":40,"max_usd_without_progress":"60"}
JSON
if "$HELPER" set-budget-policy --repository "$TMP/repo" --policy "$TMP/policy.json" \
  --reason "Breed Ledger sprints run 30 tickets" >/dev/null; then
  ok "root sets a standing repository budget policy"
else bad "root sets a standing repository budget policy"; fi
policy_result="$("$HELPER" budget-policy --scope "$policy_scope")"
if python3 -c 'import json,sys; v=json.load(sys.stdin); c=v["caps"]; assert c["max_usd_per_sprint"]=="4000" and c["max_model_runs_per_ticket"]==40 and v["reason"] and v["policy_id"] and "expires_at" not in v' <<<"$policy_result"; then
  ok "the runtime reads the standing policy caps"
else bad "the runtime reads the standing policy caps"; fi
if [ "$(stat -f '%Lp' "$TMP/state/policies" 2>/dev/null || stat -c '%a' "$TMP/state/policies")" = "700" ]; then
  ok "budget policies live in a private authority directory"
else bad "budget policies live in a private authority directory"; fi
for invalid in '{"max_usd_per_run":"0"}' '{"max_usd_per_run":"NaN"}' '{"max_usd_per_run":"100001"}' \
  '{"max_model_runs_per_ticket":1001}' '{"max_model_runs_per_ticket":"40"}' '{"max_usd_per_run":50}' \
  '{"max_output_tokens_per_turn":"9"}' '{}' '[]'; do
  printf '%s' "$invalid" > "$TMP/policy-invalid.json"
  if "$HELPER" set-budget-policy --repository "$TMP/repo" --policy "$TMP/policy-invalid.json" \
    --reason "invalid" >/dev/null 2>&1; then
    bad "set-budget-policy refuses $invalid"
  else ok "set-budget-policy refuses $invalid"; fi
done
if "$HELPER" set-budget-policy --repository "$TMP/repo" --policy "$TMP/policy.json" \
  --reason " " >/dev/null 2>&1; then
  bad "set-budget-policy requires a reason"
else ok "set-budget-policy requires a reason"; fi
if "$HELPER" budget-policy --scope "$policy_scope" | \
  python3 -c 'import json,sys; assert json.load(sys.stdin)["caps"]["max_usd_per_run"] == "50"'; then
  ok "a refused policy leaves the standing policy unchanged"
else bad "a refused policy leaves the standing policy unchanged"; fi
printf '{"max_usd_per_run":"20"}' > "$TMP/policy-replace.json"
"$HELPER" set-budget-policy --repository "$TMP/repo" --policy "$TMP/policy-replace.json" --reason "narrowed" >/dev/null
if "$HELPER" budget-policy --scope "$policy_scope" | \
  python3 -c 'import json,sys; c=json.load(sys.stdin)["caps"]; assert c == {"max_usd_per_run": "20"}, c'; then
  ok "setting a policy replaces the previous policy"
else bad "setting a policy replaces the previous policy"; fi
if "$HELPER" show-budget-policy --repository "$TMP/repo" | \
  python3 -c 'import json,sys; assert json.load(sys.stdin)["reason"] == "narrowed"'; then
  ok "root can show the standing policy by repository"
else bad "root can show the standing policy by repository"; fi
"$HELPER" clear-budget-policy --repository "$TMP/repo"
if "$HELPER" budget-policy --scope "$policy_scope" >/dev/null 2>&1; then
  bad "a cleared budget policy is unavailable"
else ok "a cleared budget policy is unavailable"; fi
other_scope="$(python3 -c 'import json,sys,pathlib; print(json.dumps({"kind":"budget-policy","repository":str(pathlib.Path(sys.argv[1]).resolve())},sort_keys=True,separators=(",",":")))' "$TMP")"
"$HELPER" set-budget-policy --repository "$TMP/repo" --policy "$TMP/policy.json" --reason "restored" >/dev/null
if "$HELPER" budget-policy --scope "$other_scope" >/dev/null 2>&1; then
  bad "a budget policy is bound to its exact repository"
else ok "a budget policy is bound to its exact repository"; fi
if "$HELPER" budget-policy --scope '{"kind":"budget-policy","repository":"relative"}' >/dev/null 2>&1; then
  bad "the runtime refuses a non-absolute policy scope"
else ok "the runtime refuses a non-absolute policy scope"; fi

INSTALLER="$ROOT/scripts/install-operator-authority.sh"
if grep -q "budget-policy --scope" "$INSTALLER"; then
  ok "the runtime sudo policy can read a repository budget policy"
else bad "the runtime sudo policy can read a repository budget policy"; fi
if grep -Eq "(set|clear|show)-budget-policy" "$INSTALLER"; then
  bad "the runtime sudo policy never sets, clears, or shows budget policies"
else ok "the runtime sudo policy never sets, clears, or shows budget policies"; fi
if grep -q '"$STATE/policies"' "$INSTALLER"; then
  ok "the installer creates the private policy directory"
else bad "the installer creates the private policy directory"; fi

if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails failure(s)"; fi
exit "$fails"
