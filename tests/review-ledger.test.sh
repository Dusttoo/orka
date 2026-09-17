#!/usr/bin/env bash
# review-ledger.test.sh -- the review loop must terminate and its blocking set
# must shrink. These are the properties that keep a PR from looping forever.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEDGER="$ROOT/scripts/review-ledger.py"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else printf 'FAIL %s\n     want: [%s]\n     got:  [%s]\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi; }

TMP="$(mktemp -d)"
LANE="${TMP}-lane"
REVIEW_WT="${TMP}-review"
trap 'rm -rf "$TMP" "$LANE" "$REVIEW_WT"' EXIT
git -C "$TMP" init -q .
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm initial
mkdir -p "$TMP/.orchestration"
CODE_GATE=$'gates:\n  - code-review\n'
BOTH_GATES=$'gates:\n  - code-review\n  - security-review\n'
write_config() { printf 'minimum_orka_version: %s\n%s' "${2:-\"\"}" "$1" > "$TMP/.orchestration/config.yaml"; }
# Most scenarios below exercise one gate; configured-gate enforcement has its own section.
write_config "$CODE_GATE"
# A real, unreachable commit id that does not move HEAD.
fake_commit() { git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit-tree 'HEAD^{tree}' -p HEAD -m "$1"; }

led() { (cd "$TMP" && python3 "$LEDGER" "$@"); }
field() { python3 -c "import json,sys; v=json.load(sys.stdin)['$1']; print(','.join(v) if isinstance(v,list) else v)"; }
review_record() {
  local pr="$1" gate="$2" file="$3" role
  role="${gate}-reviewer"
  local head permit
  head="$(git -C "$TMP" rev-parse HEAD)"
  permit="$(led permit-review "$pr" --role "$role" --head "$head" | field review_phase_permit)" || return
  led complete-review "$pr" --role "$role" --phase-permit "$permit" --result "$file" >/dev/null || return
  led record "$pr" --gate "$gate-review" --result "$file" --head "$head" --phase-permit "$permit"
}
record_pass() {
  local pr="$1" gate="$2" advisory="${3:-}" file
  file="$TMP/pass-$pr-$gate.json"
  python3 - "$file" "$gate" "$advisory" <<'PY'
import json,sys
findings=[]
if sys.argv[3]: findings=[{"component":sys.argv[3],"disposition":"advisory","severity":"low","title":"follow-up","explanation":"non-blocking follow-up","regression":False}]
json.dump({"schema_version":1,"gate":sys.argv[2]+"-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":findings},open(sys.argv[1],"w"))
PY
  review_record "$pr" "$gate" "$file"
}

git -C "$TMP" worktree add -qb review-ledger-lane "$LANE"
(cd "$LANE" && python3 "$LEDGER" open shared-pr >/dev/null)
eq "linked worktrees share one review ledger" "review" "$(led status shared-pr | field next_action)"

# --- key normalization --------------------------------------------------------
led open 1 >/dev/null
json_subject="$(led status 1 | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["work_subject"],sort_keys=True))')"
eq "a no-tracker PR owns an immutable repository-bound subject" \
  "{\"id\": \"1\", \"kind\": \"pr\", \"repository\": \"$(cd "$TMP" && pwd -P)\"}" "$json_subject"
if led open 1 --work-kind jira --work-id PROJ-1 >/dev/null 2>&1; then
  bad "an existing ledger work subject cannot be rebound"
else ok "an existing ledger work subject cannot be rebound"; fi
led open PROJ-100 --work-kind jira --work-id proj-100 >/dev/null
eq "a Jira-backed ledger normalizes its work subject" "PROJ-100" \
  "$(led status PROJ-100 | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"
led open 30 --work-kind jira --work-id PROJ-101 >/dev/null
eq "a Jira-backed ledger resolves through its distinct PR number" "PROJ-101" \
  "$(led status 30 | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"
cat > "$TMP/jira-pr-pass.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
eq "distinct Jira and PR ids complete permit and record end to end" "gates-clear" \
  "$(review_record 30 code "$TMP/jira-pr-pass.json" | field next_action)"
if led design-open 30 >/dev/null 2>&1; then
  bad "the same PR cannot collide across work-subject kinds"
else ok "the same PR cannot collide across work-subject kinds"; fi
led open 31 --work-kind jira --work-id PROJ-102 >/dev/null
JIRA_CANCEL_HEAD="$(git -C "$TMP" rev-parse HEAD)"
JIRA_CANCEL_PERMIT="$(led permit-review 31 --role code-reviewer --head "$JIRA_CANCEL_HEAD" | field review_phase_permit)"
python3 - "$TMP" "$JIRA_CANCEL_PERMIT" "$JIRA_CANCEL_HEAD" "$LEDGER" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("review_permit",str(Path(sys.argv[4]).with_name("review_permit.py")))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
common=dict(shared_root=Path(sys.argv[1]),ledger_dir=".orchestration/.review-ledger",pr="31",token=sys.argv[2],role="code-reviewer",head=sys.argv[3])
module.consume(**common,timestamp="start")
module.cancel_started(**common,timestamp="cancel")
PY
if led complete-review 31 --role code-reviewer --phase-permit "$JIRA_CANCEL_PERMIT" --result "$TMP/jira-pr-pass.json" >/dev/null 2>&1; then
  bad "cancelled permit resolves by PR when Jira id differs"
else ok "cancelled permit resolves by PR when Jira id differs"; fi
led open no-tracker-e2e >/dev/null
cat > "$TMP/no-tracker-pass.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
eq "a no-tracker PR completes permit, receipt, and record end to end" "gates-clear" \
  "$(review_record no-tracker-e2e code "$TMP/no-tracker-pass.json" | field next_action)"

# Invalid desktop output is rejected before execution starts. Reissuing the
# exact gate/head permit is idempotent so a corrected result can finish without
# fabricating a second reviewer or requiring ledger surgery.
led open invalid-output-recovery >/dev/null
INVALID_OUTPUT_HEAD="$(git -C "$TMP" rev-parse HEAD)"
invalid_permit_json="$(led permit-review invalid-output-recovery --role code-reviewer --head "$INVALID_OUTPUT_HEAD")"
INVALID_OUTPUT_PERMIT="$(printf '%s' "$invalid_permit_json" | field review_phase_permit)"
eq "a new review permit is not reported as reused" "False" \
  "$(printf '%s' "$invalid_permit_json" | field review_phase_permit_reused)"
cat > "$TMP/invalid-output-review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[{"component":"tests/e2e/particles.spec.ts:cold hot reset flow","disposition":"advisory","severity":"low","title":"Add flow coverage","explanation":"The flow would benefit from a broader regression.","regression":false}]}
JSON
if led complete-review invalid-output-recovery --role code-reviewer \
  --phase-permit "$INVALID_OUTPUT_PERMIT" --result "$TMP/invalid-output-review.json" \
  > /dev/null 2> "$TMP/invalid-output-error"; then
  bad "invalid reviewer output is rejected before permit consumption"
elif grep -q 'retry with the same phase permit' "$TMP/invalid-output-error"; then
  ok "invalid reviewer output is rejected before permit consumption"
else bad "invalid reviewer output explains permit recovery"; fi
reissued_permit_json="$(led permit-review invalid-output-recovery --role code-reviewer --head "$INVALID_OUTPUT_HEAD")"
eq "an unstarted review permit is reissued idempotently" "$INVALID_OUTPUT_PERMIT" \
  "$(printf '%s' "$reissued_permit_json" | field review_phase_permit)"
eq "permit recovery is explicit in the issuance result" "True" \
  "$(printf '%s' "$reissued_permit_json" | field review_phase_permit_reused)"
cat > "$TMP/corrected-output-review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[{"component":"tests/e2e/particles.spec.ts:cold_hot_reset_flow","disposition":"advisory","severity":"low","title":"Add flow coverage","explanation":"The flow would benefit from a broader regression.","regression":false}]}
JSON
led complete-review invalid-output-recovery --role code-reviewer \
  --phase-permit "$INVALID_OUTPUT_PERMIT" --result "$TMP/corrected-output-review.json" >/dev/null
eq "the corrected result completes through the recovered permit" "gates-clear" \
  "$(led record invalid-output-recovery --gate code-review --result "$TMP/corrected-output-review.json" --head "$INVALID_OUTPUT_HEAD" --phase-permit "$INVALID_OUTPUT_PERMIT" | field next_action)"
eq "line numbers are stripped from component keys" \
  "src/auth/session.ts:refreshtoken" \
  "$(led record 1 --gate code-review --verdict FAIL --blocking 'src/auth/session.ts:refreshToken:142' | field accepted_blocking)"
eq "the [component: ...] wrapper and casing normalize to the same key" \
  "src/auth/session.ts:refreshtoken" \
  "$(led record 1 --gate code-review --verdict FAIL --blocking '[component: SRC/auth/Session.ts:RefreshToken]' | field open_blocking)"
eq "the same defect named twice accumulates a second strike" \
  "2" "$(led status 1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["components"]["src/auth/session.ts:refreshtoken"]["strikes"])')"
led open cap-test --max-rounds 1 >/dev/null
led open cap-test --max-rounds 99 >/dev/null
eq "worker CLI cannot raise a durable repair cap" "1" "$(led status cap-test | field max_rounds)"

led design-open 'free/form' >/dev/null
led design-open 'free-form' >/dev/null
eq "sanitized free-form ids retain exact collision-free identity" "free/form" \
  "$(led status 'free/form' | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"
eq "colliding free-form ids own distinct ledgers" "free-form" \
  "$(led status 'free-form' | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["id"])')"
if led status 'free form' >/dev/null 2>&1; then
  bad "lossy aliases cannot select a canonical subject ledger"
else ok "fallback lookup requires the exact immutable subject"; fi

# --- round 1 has full blocking authority --------------------------------------
led open 2 >/dev/null
out="$(led record 2 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/b.ts:bar')"
eq "round 1 accepts every blocking finding" "src/a.ts:foo,src/b.ts:bar" "$(printf '%s' "$out" | field accepted_blocking)"
eq "round 1 is recorded as full-authority scope" "full-authority" "$(printf '%s' "$out" | field scope_mode)"
eq "the initial generation remains full-authority until a repair is recorded" \
  "full-authority" "$(printf '%s' "$out" | field next_scope_mode)"

# --- the scope freeze ---------------------------------------------------------
SCOPE_REPAIR_HEAD="$(fake_commit scope-repair)"
cat > "$TMP/scope-repair.json" <<JSON
{"schema_version":1,"head":"$SCOPE_REPAIR_HEAD","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"},{"component":"src/b.ts:bar","status":"closed","root_cause":"missing guard","change":"added guard","verification":"guard regression passes"}]}
JSON
led record-repair 2 --report "$TMP/scope-repair.json" >/dev/null
out="$(led record 2 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/new.ts:nit' --head "$SCOPE_REPAIR_HEAD")"
eq "a new non-regression finding is demoted in a frozen round" "src/new.ts:nit" "$(printf '%s' "$out" | field demoted_to_advisory)"
eq "a known component still blocks in a frozen round" "src/a.ts:foo" "$(printf '%s' "$out" | field accepted_blocking)"
led complete-repair-review 2 >/dev/null
eq "a component the repaired generation stopped reporting auto-resolves" "resolved" \
  "$(led status 2 | python3 -c 'import json,sys; print(json.load(sys.stdin)["components"]["src/b.ts:bar"]["status"])')"
eq "the blocking set shrank" "src/a.ts:foo" "$(led status 2 | field open_blocking)"

led open 3 >/dev/null
led record 3 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
REGRESSION_REPAIR_HEAD="$(fake_commit regression-repair)"
cat > "$TMP/regression-repair.json" <<JSON
{"schema_version":1,"head":"$REGRESSION_REPAIR_HEAD","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"}]}
JSON
led record-repair 3 --report "$TMP/regression-repair.json" >/dev/null
eq "a declared regression keeps blocking authority in a frozen round" \
  "src/a.ts:foo,src/broke.ts:oops" \
  "$(led record 3 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/broke.ts:oops' --regression 'src/broke.ts:oops' --head "$REGRESSION_REPAIR_HEAD" | field accepted_blocking)"

# --- the security gate is never scope-frozen ----------------------------------
led open 4 >/dev/null
led record 4 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
out="$(led record 4 --gate security-review --verdict FAIL --blocking 'src/rls/policy.sql:tenantIsolation')"
eq "a late security finding is never demoted" "src/rls/policy.sql:tenantisolation" "$(printf '%s' "$out" | field accepted_blocking)"
eq "a late security finding still fails the gate" "FAIL" "$(printf '%s' "$out" | field effective_verdict)"

led open gate-owned >/dev/null
led record gate-owned --gate code-review --verdict FAIL --blocking 'src/shared.py:check' >/dev/null
led record gate-owned --gate security-review --verdict FAIL --blocking 'src/shared.py:check' >/dev/null
eq "one gate cannot auto-resolve another gate claim" "src/shared.py:check" \
  "$(led record gate-owned --gate code-review --verdict FAIL | field open_blocking)"
eq "aggregate resolves only after every owning gate clears its claim" "" \
  "$(led record gate-owned --gate security-review --verdict FAIL | field open_blocking)"

led open staged-generation >/dev/null
led record staged-generation --gate code-review --verdict FAIL --blocking 'src/staged.py:check' >/dev/null
led record staged-generation --gate security-review --verdict FAIL --blocking 'src/staged.py:check' >/dev/null
STAGED_REPAIR_HEAD="$(fake_commit staged-repair)"
cat > "$TMP/staged-repair.json" <<JSON
{"schema_version":1,"head":"$STAGED_REPAIR_HEAD","findings":[{"component":"src/staged.py:check","status":"closed","root_cause":"shared boundary","change":"fixed shared boundary","verification":"both gate regressions pass"}]}
JSON
led record-repair staged-generation --report "$TMP/staged-repair.json" >/dev/null
led record staged-generation --gate security-review --verdict FAIL --head "$STAGED_REPAIR_HEAD" >/dev/null
eq "partial generation does not finalize component claims" "src/staged.py:check" \
  "$(led status staged-generation | field open_blocking)"
led record staged-generation --gate code-review --verdict FAIL --head "$STAGED_REPAIR_HEAD" >/dev/null
eq "all gate results remain staged until explicit generation finalization" "src/staged.py:check" \
  "$(led status staged-generation | field open_blocking)"
eq "finalization applies every gate claim atomically" "" \
  "$(led complete-repair-review staged-generation | field open_blocking)"

led open concurrent-permits >/dev/null
HEAD_CONCURRENT="$(git -C "$TMP" rev-parse HEAD)"
CODE_PERMIT="$(led permit-review concurrent-permits --role code-reviewer --head "$HEAD_CONCURRENT" | field review_phase_permit)"
SEC_PERMIT="$(led permit-review concurrent-permits --role security-reviewer --head "$HEAD_CONCURRENT" | field review_phase_permit)"
cat > "$TMP/concurrent-code.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
cat > "$TMP/concurrent-security.json" <<'JSON'
{"schema_version":1,"gate":"security-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
led complete-review concurrent-permits --role code-reviewer --phase-permit "$CODE_PERMIT" --result "$TMP/concurrent-code.json" >/dev/null
led record concurrent-permits --gate code-review --result "$TMP/concurrent-code.json" --head "$HEAD_CONCURRENT" --phase-permit "$CODE_PERMIT" >/dev/null
if led complete-review concurrent-permits --role security-reviewer --phase-permit "$SEC_PERMIT" --result "$TMP/concurrent-security.json" >/dev/null \
  && led record concurrent-permits --gate security-review --result "$TMP/concurrent-security.json" --head "$HEAD_CONCURRENT" --phase-permit "$SEC_PERMIT" >/dev/null; then
  ok "concurrent gate permits remain completable and recordable in either order"
else
  bad "concurrent gate permits remain completable and recordable in either order"
fi
CONCURRENT_LEDGER="$(led open concurrent-permits | field ledger)"
eq "concurrent gates share one logical review round" "1" \
  "$(python3 -c 'import json,sys; print(len({r["round"] for r in json.load(open(sys.argv[1]))["rounds"]}))' "$CONCURRENT_LEDGER")"

led open concurrent-fail-order >/dev/null
FAIL_ORDER_CODE_PERMIT="$(led permit-review concurrent-fail-order --role code-reviewer --head "$HEAD_CONCURRENT" | field review_phase_permit)"
FAIL_ORDER_SEC_PERMIT="$(led permit-review concurrent-fail-order --role security-reviewer --head "$HEAD_CONCURRENT" | field review_phase_permit)"
cat > "$TMP/concurrent-code-fail.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"review","status":"fail"}],"findings":[{"component":"tests/flow.test.ts:cold-hot-reset","disposition":"blocking","severity":"high","title":"Flow coverage missing","explanation":"The cold, hot, and reset sequence has no end-to-end regression assertion.","regression":false},{"component":"tests/copy.test.ts:misconception-scan","disposition":"blocking","severity":"high","title":"Misconception scan missing","explanation":"The shipped copy is not checked for the prohibited misconception vocabulary.","regression":false}]}
JSON
led complete-review concurrent-fail-order --role security-reviewer --phase-permit "$FAIL_ORDER_SEC_PERMIT" --result "$TMP/concurrent-security.json" >/dev/null
first_gate="$(led record concurrent-fail-order --gate security-review --result "$TMP/concurrent-security.json" --head "$HEAD_CONCURRENT" --phase-permit "$FAIL_ORDER_SEC_PERMIT")"
eq "one completed concurrent gate cannot clear the generation" "code-review" \
  "$(printf '%s' "$first_gate" | field missing_gates)"
led complete-review concurrent-fail-order --role code-reviewer --phase-permit "$FAIL_ORDER_CODE_PERMIT" --result "$TMP/concurrent-code-fail.json" >/dev/null
second_gate="$(led record concurrent-fail-order --gate code-review --result "$TMP/concurrent-code-fail.json" --head "$HEAD_CONCURRENT" --phase-permit "$FAIL_ORDER_CODE_PERMIT")"
eq "a failing initial gate recorded second retains full authority" "full-authority" \
  "$(printf '%s' "$second_gate" | field scope_mode)"
eq "a failing initial gate recorded second keeps every blocker" \
  "tests/copy.test.ts:misconception-scan,tests/flow.test.ts:cold-hot-reset" \
  "$(printf '%s' "$second_gate" | field accepted_blocking)"
eq "concurrent gate responses do not consume repair cycles" "0" \
  "$(printf '%s' "$second_gate" | field fix_cycles)"
if led permit-review concurrent-fail-order --role code-reviewer --head "$HEAD_CONCURRENT" >/dev/null 2>&1; then
  bad "a completed gate cannot receive another permit in the same generation"
else ok "a completed gate cannot receive another permit in the same generation"; fi

led open generation-head-binding >/dev/null
BOUND_HEAD="$(git -C "$TMP" rev-parse HEAD)"
led permit-review generation-head-binding --role code-reviewer --head "$BOUND_HEAD" >/dev/null
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm moved-head
MOVED_HEAD="$(git -C "$TMP" rev-parse HEAD)"
if led permit-review generation-head-binding --role security-reviewer --head "$MOVED_HEAD" >/dev/null 2>&1; then
  bad "one generation cannot combine review permits from different heads"
else ok "one generation cannot combine review permits from different heads"; fi
git -C "$TMP" reset --hard -q "$BOUND_HEAD"

# A non-repair commit (for example a branch update or policy-only resolution)
# starts a preserved review generation without spending a fix cycle.
led open rebind-clean >/dev/null
record_pass rebind-clean code >/dev/null
REBIND_OLD_HEAD="$(git -C "$TMP" rev-parse HEAD)"
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm branch-update
REBIND_NEW_HEAD="$(git -C "$TMP" rev-parse HEAD)"
if led permit-review rebind-clean --role code-reviewer --head "$REBIND_NEW_HEAD" >/dev/null 2>&1; then
  bad "a moved head still requires an explicit generation rebind"
else ok "a moved head still requires an explicit generation rebind"; fi
rebound="$(led rebind-generation rebind-clean --head "$REBIND_NEW_HEAD" --reason 'merged current develop without findings repair')"
eq "new-head rebind starts the next review generation" "2" \
  "$(printf '%s' "$rebound" | field review_generation)"
eq "new-head rebind does not spend a repair cycle" "0" \
  "$(printf '%s' "$rebound" | field fix_cycles)"
eq "new-head rebind preserves the prior required gate set" "code-review" \
  "$(printf '%s' "$rebound" | field required_gates)"
eq "new-head rebind binds the exact replacement head" "$REBIND_NEW_HEAD" \
  "$(printf '%s' "$rebound" | field generation_head)"
REBIND_LEDGER="$(led open rebind-clean | field ledger)"
eq "new-head rebind keeps prior review history" "1" \
  "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["rounds"]))' "$REBIND_LEDGER")"
eq "new-head rebind records its auditable reason" "merged current develop without findings repair" \
  "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["generation_rebinds"][-1]["reason"])' "$REBIND_LEDGER")"
if led rebind-generation rebind-clean --head "$REBIND_NEW_HEAD" --reason 'duplicate' >/dev/null 2>&1; then
  bad "the same head cannot be rebound twice"
else ok "the same head cannot be rebound twice"; fi
if led permit-review rebind-clean --role code-reviewer --head "$REBIND_NEW_HEAD" >/dev/null; then
  ok "the new generation can issue a permit for its exact head"
else bad "the new generation can issue a permit for its exact head"; fi

# A moved head starts a mandatory review generation even when historical
# repairs spent the entire fix budget and left a durable escalation marker.
# The budget limits subsequent repairs, not review of a non-findings commit.
led open rebind-exhausted --max-rounds 1 >/dev/null
led record rebind-exhausted --gate code-review --verdict FAIL \
  --blocking 'src/exhausted.ts:boundary' >/dev/null
REBIND_EXHAUSTED_REPAIR_HEAD="$(git -C "$TMP" rev-parse HEAD)"
cat > "$TMP/rebind-exhausted-repair.json" <<JSON
{"schema_version":1,"head":"$REBIND_EXHAUSTED_REPAIR_HEAD","findings":[{"component":"src/exhausted.ts:boundary","status":"closed","root_cause":"missing boundary","change":"added boundary","verification":"boundary regression passes"}]}
JSON
led record-repair rebind-exhausted --report "$TMP/rebind-exhausted-repair.json" >/dev/null
record_pass rebind-exhausted code >/dev/null
eq "the last authorized repair can clear before a later head move" "gates-clear" \
  "$(led complete-repair-review rebind-exhausted | field next_action)"
led escalate rebind-exhausted --reason 'historical repair budget exhausted' >/dev/null
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm non-findings-update
REBIND_EXHAUSTED_NEW_HEAD="$(git -C "$TMP" rev-parse HEAD)"
rebound_exhausted="$(led rebind-generation rebind-exhausted --head "$REBIND_EXHAUSTED_NEW_HEAD" --reason 'merged a non-findings update')"
eq "a rebound generation remains reviewable with no fix cycles left" "review" \
  "$(printf '%s' "$rebound_exhausted" | field next_action)"
eq "a rebound generation does not replenish the repair budget" "0" \
  "$(printf '%s' "$rebound_exhausted" | field fix_cycles_remaining)"
eq "a rebound generation reports its pending gate review" "True" \
  "$(printf '%s' "$rebound_exhausted" | field rebound_generation_pending_review)"
if record_pass rebind-exhausted code >/dev/null; then
  ok "a rebound generation can issue and complete its required review"
else bad "a rebound generation can issue and complete its required review"; fi
eq "a passing rebound review clears despite historical escalation" "gates-clear" \
  "$(led status rebind-exhausted | field next_action)"

led open rebind-resolved >/dev/null
cat > "$TMP/rebind-resolved-fail.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"review","status":"fail"}],"findings":[{"component":"src/policy.ts:obsolete","disposition":"blocking","severity":"medium","title":"Policy mismatch","explanation":"The old policy required a behavior that is no longer applicable.","regression":false}]}
JSON
review_record rebind-resolved code "$TMP/rebind-resolved-fail.json" >/dev/null
led resolve rebind-resolved --key 'src/policy.ts:obsolete' >/dev/null
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm policy-resolution
RESOLVED_HEAD="$(git -C "$TMP" rev-parse HEAD)"
resolved_rebind="$(led rebind-generation rebind-resolved --head "$RESOLVED_HEAD" --reason 'operator resolved obsolete policy finding')"
eq "a manually resolved generation can bind a new head" "2" \
  "$(printf '%s' "$resolved_rebind" | field review_generation)"
eq "manual resolution rebind preserves zero fix cycles" "0" \
  "$(printf '%s' "$resolved_rebind" | field fix_cycles)"

led open rebind-blocked >/dev/null
led record rebind-blocked --gate code-review --verdict FAIL --blocking 'src/open.ts:blocker' >/dev/null
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm unrelated-update
BLOCKED_REBIND_HEAD="$(git -C "$TMP" rev-parse HEAD)"
if led rebind-generation rebind-blocked --head "$BLOCKED_REBIND_HEAD" --reason 'try to skip repair' >/dev/null 2>&1; then
  bad "new-head rebind cannot bypass open blocking findings"
else ok "new-head rebind cannot bypass open blocking findings"; fi

led open minimum-version-review >/dev/null
write_config "$CODE_GATE" 99.0.0
if led permit-review minimum-version-review --role code-reviewer --head "$BLOCKED_REBIND_HEAD" >/dev/null 2>&1; then
  bad "review permits fail closed below the repository minimum Orka version"
else ok "review permits fail closed below the repository minimum Orka version"; fi
write_config "$CODE_GATE"

# Recreate the exact legacy corruption: security PASS was stored as round one,
# then code FAIL was scope-frozen as round two and its blocker became advisory.
LEGACY_LEDGER="$CONCURRENT_LEDGER"
python3 - "$LEGACY_LEDGER" <<'PY'
import json,sys
path=sys.argv[1]
state=json.load(open(path))
security=next(r for r in state["rounds"] if r["gate"]=="security-review")
code=next(r for r in state["rounds"] if r["gate"]=="code-review")
for entry in (security,code):
    entry.pop("generation",None); entry.pop("head",None)
security.update(round=1,scope_mode="full-authority")
key="tests/legacy.test.ts:cold-hot-reset"
code.update(round=2,scope_mode="scope-frozen",claimed_verdict="FAIL",effective_verdict="PASS",blocking=[],advisory=[key])
state.setdefault("advisories",[]).append({"key":key,"display":key,"reason":"out-of-scope-in-frozen-round","round":2,"gate":"code-review","finding":{"component":key,"disposition":"blocking","severity":"high","title":"Flow coverage missing","explanation":"The initial full-authority reviewer required the missing flow assertion.","regression":False}})
json.dump(state,open(path,"w"),indent=2,sort_keys=True)
open(path,"a").write("\n")
PY
migrated="$(led migrate-concurrent-review concurrent-permits --reason 'restore blockers hidden by legacy gate completion ordering')"
eq "legacy concurrent-review migration restores hidden blockers" \
  "tests/legacy.test.ts:cold-hot-reset" "$(printf '%s' "$migrated" | field open_blocking)"
eq "legacy concurrent-review migration preserves one logical round" "1" \
  "$(python3 -c 'import json,sys; print(len({r["round"] for r in json.load(open(sys.argv[1]))["rounds"]}))' "$LEGACY_LEDGER")"

# A provider-side pre-ack cancellation is terminal, not a reusable permit.
led open cancelled-permit >/dev/null
CANCEL_HEAD="$(git -C "$TMP" rev-parse HEAD)"
CANCEL_PERMIT="$(led permit-review cancelled-permit --role code-reviewer --head "$CANCEL_HEAD" | field review_phase_permit)"
python3 - "$TMP" "$CANCEL_PERMIT" "$CANCEL_HEAD" "$LEDGER" <<'PY'
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("review_permit",str(Path(sys.argv[4]).with_name("review_permit.py")))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.consume(shared_root=Path(sys.argv[1]),ledger_dir=".orchestration/.review-ledger",pr="cancelled-permit",token=sys.argv[2],role="code-reviewer",head=sys.argv[3],timestamp="start")
module.cancel_started(shared_root=Path(sys.argv[1]),ledger_dir=".orchestration/.review-ledger",pr="cancelled-permit",token=sys.argv[2],role="code-reviewer",head=sys.argv[3],timestamp="cancel")
PY
if led complete-review cancelled-permit --role code-reviewer --phase-permit "$CANCEL_PERMIT" --result "$TMP/concurrent-code.json" >/dev/null 2>&1; then
  bad "cancelled permit cannot complete"
else ok "cancelled permit cannot complete"; fi

# --- explicit repairs, redesign, and the cap ----------------------------------
led open 5 --max-rounds 2 >/dev/null
STALE_HEAD="$(git -C "$TMP" rev-parse HEAD)"
REPAIR_ONE_HEAD="$(fake_commit repair-one)"
REPAIR_TWO_HEAD="$(fake_commit repair-two)"
STALE_PERMIT="$(led permit-review 5 --role code-reviewer --head "$STALE_HEAD" | field review_phase_permit)"
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head "$REPAIR_ONE_HEAD" >/dev/null
led repair-brief 5 | grep -q 'stable finding ID' && ok "repair brief carries stable IDs" || bad "repair brief carries stable IDs"
cat > "$TMP/repair-1.json" <<JSON
{"schema_version":1,"head":"$REPAIR_ONE_HEAD","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"}]}
JSON
eq "recording a repair starts a pending review" "True" "$(led record-repair 5 --report "$TMP/repair-1.json" | field repair_pending_review)"
if led complete-review 5 --role code-reviewer --phase-permit "$STALE_PERMIT" --result "$TMP/concurrent-code.json" >/dev/null 2>&1; then
  bad "superseded generation permit cannot complete"
else ok "superseded generation permit cannot complete"; fi
if led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head "$REPAIR_TWO_HEAD" >/dev/null 2>&1; then
  bad "a reviewer cannot record against the wrong repaired head"
else ok "a reviewer cannot record against the wrong repaired head"; fi
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head "$REPAIR_ONE_HEAD" >/dev/null
eq "a repaired head must complete its required gate set" \
  "redesign" "$(led complete-repair-review 5 | field next_action)"
eq "a passing design gate releases the component for another fix" \
  "review" "$(led redesign 5 --key 'src/a.ts:foo' --verdict PASS | field next_action)"
cat > "$TMP/repair-2.json" <<JSON
{"schema_version":1,"head":"$REPAIR_TWO_HEAD","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"boundary missed","change":"fixed boundary","verification":"boundary regression passes"}]}
JSON
led record-repair 5 --report "$TMP/repair-2.json" >/dev/null
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head "$REPAIR_TWO_HEAD" >/dev/null
eq "spending the round cap with findings open stops the loop" \
  "escalate-human" "$(led complete-repair-review 5 | field next_action)"
if led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null 2>&1; then
  bad "an escalated ledger must refuse further rounds"
else ok "an escalated ledger refuses further rounds"; fi
led handoff 5 2>/dev/null | grep -q "Still blocking" && ok "handoff renders the human report" || bad "handoff renders the human report"

# A human may authorize a bounded absolute repair ceiling without deleting the
# escalation, findings, or prior attempts. The capability is bound to this PR.
AUTH_HELPER="$ROOT/host-tools/orchestration-recovery-authority.py"
AUTH_STATE="$TMP/review-authority"
authorized_led() {
  (cd "$TMP" && \
    ORCHESTRATION_TEST_MODE=1 \
    ORCHESTRATION_TEST_AUTHORITY_HELPER="$AUTH_HELPER" \
    ORCHESTRATION_AUTHORITY_TEST_MODE=1 \
    ORCHESTRATION_AUTHORITY_STATE_DIR="$AUTH_STATE" \
    python3 "$LEDGER" "$@")
}
review_token="$(ORCHESTRATION_AUTHORITY_TEST_MODE=1 ORCHESTRATION_AUTHORITY_STATE_DIR="$AUTH_STATE" \
  "$AUTH_HELPER" issue-review-repair --repository "$TMP" --pr 5 \
  --ceiling-repair-cycles 3 --reason 'approve one additional bounded repair')"
authorized="$(printf '%s\n' "$review_token" | \
  authorized_led authorize-repair 5 --operator-capability-stdin)"
eq "root review authority raises only the absolute repair ceiling" "3" \
  "$(printf '%s' "$authorized" | field operator_repair_ceiling)"
eq "the acknowledged escalation returns to its required redesign" "redesign" \
  "$(printf '%s' "$authorized" | field next_action)"
eq "operator repair authority preserves both prior repair cycles" "2" \
  "$(printf '%s' "$authorized" | field fix_cycles)"
if printf '%s\n' "$review_token" | \
  authorized_led authorize-repair 5 --operator-capability-stdin >/dev/null 2>&1; then
  bad "review repair authority cannot be replayed"
else ok "review repair authority cannot be replayed"; fi

led open 50 --max-rounds 2 >/dev/null
led record 50 --gate code-review --verdict FAIL --blocking 'src/support.ts:validation' >/dev/null
led escalate 50 --reason 'human decision required before repair' >/dev/null
manual_token="$(ORCHESTRATION_AUTHORITY_TEST_MODE=1 ORCHESTRATION_AUTHORITY_STATE_DIR="$AUTH_STATE" \
  "$AUTH_HELPER" issue-review-repair --repository "$TMP" --pr 50 \
  --ceiling-repair-cycles 2 --reason 'human approved the existing repair budget')"
manual_authorized="$(printf '%s\n' "$manual_token" | \
  authorized_led authorize-repair 50 --operator-capability-stdin)"
eq "human acknowledgement can reopen a manually escalated first repair" "review" \
  "$(printf '%s' "$manual_authorized" | field next_action)"
eq "human acknowledgement does not erase the escalation record" "True" \
  "$(authorized_led status 50 | python3 -c 'import json,sys; print(json.load(sys.stdin)["escalated"])')"

# Repository-writable ledger fields cannot manufacture repair authority. The
# live root-owned grant remains the only source of the effective ceiling.
python3 - "$TMP" <<'PY'
import json,sys
from pathlib import Path
path=next((Path(sys.argv[1])/".orchestration/.review-ledger").glob("subject-pr-50-*.json"))
state=json.loads(path.read_text())
state["operator_repair_ceiling"]=999
path.write_text(json.dumps(state,indent=2,sort_keys=True)+"\n")
PY
ORCHESTRATION_AUTHORITY_TEST_MODE=1 ORCHESTRATION_AUTHORITY_STATE_DIR="$AUTH_STATE" \
  "$AUTH_HELPER" revoke-review-repair --repository "$TMP" --pr 50
revoked_status="$(authorized_led status 50)"
eq "a writable ledger cannot forge root-owned repair authority" "0" \
  "$(printf '%s' "$revoked_status" | field operator_repair_ceiling)"
eq "revoking root authority restores the escalation stop" "escalate-human" \
  "$(printf '%s' "$revoked_status" | field next_action)"

# Recording the last authorized repair spends the remaining cycle immediately,
# but the repaired head still owns a mandatory review. A durable escalation
# must not deadlock that review against authorize-repair. If the review fails,
# escalation resumes after its result is finalized.
led open pending-review-at-cap --max-rounds 1 >/dev/null
led record pending-review-at-cap --gate code-review --verdict FAIL \
  --blocking 'src/final.ts:boundary' >/dev/null
PENDING_REPAIR_HEAD="$(git -C "$TMP" rev-parse HEAD)"
cat > "$TMP/pending-review-at-cap.json" <<JSON
{"schema_version":1,"head":"$PENDING_REPAIR_HEAD","findings":[{"component":"src/final.ts:boundary","status":"closed","root_cause":"missing boundary","change":"added boundary","verification":"boundary regression passes"}]}
JSON
led record-repair pending-review-at-cap --report "$TMP/pending-review-at-cap.json" >/dev/null
pending_escalated="$(led escalate pending-review-at-cap --reason 'last authorized repair requires review')"
eq "a pending repair review outranks durable escalation" "review" \
  "$(printf '%s' "$pending_escalated" | field next_action)"
eq "a pending repair review remains allowed with no fix cycles left" "0" \
  "$(printf '%s' "$pending_escalated" | field fix_cycles_remaining)"
cat > "$TMP/pending-review-fail.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"repair verification","status":"fail"}],"findings":[{"component":"src/final.ts:boundary","disposition":"blocking","severity":"high","title":"Boundary remains open","explanation":"The repaired head does not close the boundary.","regression":true}]}
JSON
if review_record pending-review-at-cap code "$TMP/pending-review-fail.json" >/dev/null; then
  ok "an escalated ledger permits the pending repaired-head review"
else bad "an escalated ledger permits the pending repaired-head review"; fi
eq "a failed last-cycle repair escalates after review completion" "escalate-human" \
  "$(led complete-repair-review pending-review-at-cap | field next_action)"

# --- the cap counts explicit repairs, not review passes ------------------------
led open 9 --max-rounds 2 >/dev/null
led record 9 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
record_pass 9 security >/dev/null
eq "review passes do not spend a repair cycle" "0" "$(led status 9 | field fix_cycles)"
if record_pass 9 code >/dev/null 2>&1; then
  bad "a second same-generation gate result cannot erase its prior failure"
else ok "a second same-generation gate result cannot erase its prior failure"; fi
eq "the original blocker survives until a repair is recorded" "src/a.ts:foo" \
  "$(led status 9 | field open_blocking)"

# --- the clean path -----------------------------------------------------------
led open 6 >/dev/null
eq "a clean gate clears the loop" "gates-clear" "$(record_pass 6 code | field next_action)"
led open 11 >/dev/null
eq "advisory-only findings do not fail a gate" \
  "PASS" "$(record_pass 11 code 'src/x.ts:nit' | field effective_verdict)"

# --- structured reviewer results ---------------------------------------------
led open 10 >/dev/null
cat > "$TMP/review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"tests","status":"fail"}],"findings":[{"component":"src/a.ts:parse","disposition":"blocking","severity":"high","title":"Missing rejection","explanation":"Invalid input reaches parse and is accepted; reject it and add the regression assertion.","regression":true}]}
JSON
eq "structured results populate the durable ledger" \
  "src/a.ts:parse" "$(review_record 10 code "$TMP/review.json" | field accepted_blocking)"
led handoff 10 | grep -q "Invalid input reaches parse" && ok "finding-only explanation survives handoff" || bad "finding-only explanation survives handoff"
if led record 10 --gate code-review --result "$TMP/review.json" --verdict FAIL >/dev/null 2>&1; then
  bad "structured and manual review inputs must not be mixed"
else ok "structured and manual review inputs cannot be mixed"; fi

# --- contradictions are rejected ----------------------------------------------
if led record 6 --gate code-review --verdict PASS --blocking 'src/a.ts:foo' >/dev/null 2>&1; then
  bad "a PASS listing blocking findings must be rejected"
else ok "a PASS listing blocking findings is rejected"; fi

# --- round-aware guidance -----------------------------------------------------
led open 7 >/dev/null
led brief 7 | grep -q 'JSON `component` field to the bare `<path>:<symbol>` key' && ok "review brief requests a bare JSON component key" || bad "review brief requests a bare JSON component key"
led brief 7 | grep -q 'never use whitespace' && ok "review brief forbids whitespace in component keys" || bad "review brief forbids whitespace in component keys"
if led brief 7 | grep -q 'Key every finding as `\[component:'; then
  bad "review brief does not instruct reviewers to wrap JSON component keys"
else ok "review brief does not instruct reviewers to wrap JSON component keys"; fi
led brief 7 | grep -q "Investigate uncertainty before the verdict" && ok "round 1 briefs require evidence before blocking" || bad "round 1 briefs require evidence before blocking"
REPAIR_SEVEN_HEAD="$(fake_commit repair-seven)"
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head "$REPAIR_SEVEN_HEAD" >/dev/null
cat > "$TMP/repair-7.json" <<JSON
{"schema_version":1,"head":"$REPAIR_SEVEN_HEAD","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"bad condition","change":"fixed condition","verification":"regression passes"}]}
JSON
led record-repair 7 --report "$TMP/repair-7.json" >/dev/null
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head "$REPAIR_SEVEN_HEAD" >/dev/null
led complete-repair-review 7 >/dev/null
led brief 7 | grep -q "ADVISORY and name the exact evidence" && ok "round 3 briefs advisory-on-doubt" || bad "round 3 briefs advisory-on-doubt"
led brief 7 | grep -q "REDESIGN REQUIRED" && ok "the brief flags a component needing redesign" || bad "the brief flags a component needing redesign"

# --- pre-code design rounds have their own durable cap -------------------------
led design-open PROJ-1 --max-design-rounds 2 >/dev/null
eq "a failed design returns to redesign" "redesign" "$(led design-record PROJ-1 --verdict FAIL --evidence 'boundary incomplete' | field next_action)"
eq "the independent design cap escalates" "escalate-human" "$(led design-record PROJ-1 --verdict FAIL --evidence 'boundary still incomplete' | field next_action)"

DESIGN_ID='free form architecture'
led design-open "$DESIGN_ID" >/dev/null
eq "a free-form design owns a design subject" "design" \
  "$(led status "$DESIGN_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["work_subject"]["kind"])')"
HEAD_SHA="$(git -C "$TMP" rev-parse HEAD)"
printf 'reviewed boundary\n' > "$TMP/design-free-form.md"
ARTIFACT_SHA="$(shasum -a 256 "$TMP/design-free-form.md" | awk '{print $1}')"
PERMIT="$(led permit-review "$DESIGN_ID" --role design-reviewer --head "$HEAD_SHA" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_phase_permit"])')"
cat > "$TMP/design-pass.json" <<JSON
{"schema_version":1,"gate":"design-review","verdict":"PASS","source_sha":"$HEAD_SHA","artifact":"design-free-form.md","artifact_sha256":"$ARTIFACT_SHA","phase_permit":"$PERMIT","checks":[{"name":"trust-boundary","status":"pass"}]}
JSON
led complete-review "$DESIGN_ID" --role design-reviewer --phase-permit "$PERMIT" --result "$TMP/design-pass.json" >/dev/null
python3 - "$TMP/design-pass.json" "$TMP/design-short.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1])); value['source_sha']=value['source_sha'][:12]
json.dump(value, open(sys.argv[2], 'w'))
PY
if led design-record "$DESIGN_ID" --result "$TMP/design-short.json" >/dev/null 2>&1; then
  bad "abbreviated design source SHA must fail closed"
else ok "abbreviated design source SHA fails closed"; fi
python3 - "$TMP/design-pass.json" "$TMP/design-bad-digest.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1])); value['artifact_sha256']='0'*64
json.dump(value, open(sys.argv[2], 'w'))
PY
if led design-record "$DESIGN_ID" --result "$TMP/design-bad-digest.json" >/dev/null 2>&1; then
  bad "mismatched design artifact digest must fail closed"
else ok "mismatched design artifact digest fails closed"; fi
eq "free-form design PASS completes end to end" "implement" "$(led design-record "$DESIGN_ID" --result "$TMP/design-pass.json" | field next_action)"
if led permit-review "$DESIGN_ID" --role design-reviewer --head "$HEAD_SHA" >/dev/null 2>&1; then
  bad "passed design phase must not mint another reviewer permit"
else ok "passed design phase cannot mint another reviewer permit"; fi
led design-handoff PROJ-1 | grep -q 'No production implementation is authorized' && ok "design handoff blocks implementation" || bad "design handoff blocks implementation"

# --- aliasing merges a drifted key --------------------------------------------
led open 8 >/dev/null
led record 8 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
led record 8 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/a.ts:fooHelper' --regression 'src/a.ts:fooHelper' >/dev/null
eq "aliasing a drifted key merges its strikes" "3" "$(led alias 8 --from 'src/a.ts:fooHelper' --to 'src/a.ts:foo' | field strikes)"

led open alias-claims >/dev/null
led record alias-claims --gate code-review --verdict FAIL --blocking 'src/a.ts:canonical' >/dev/null
led record alias-claims --gate security-review --verdict FAIL --blocking 'src/a.ts:drifted' >/dev/null
led alias alias-claims --from 'src/a.ts:drifted' --to 'src/a.ts:canonical' >/dev/null
eq "aliasing preserves every gate owner" "code-review,security-review" \
  "$(led status alias-claims | python3 -c 'import json,sys; print(",".join(sorted(json.load(sys.stdin)["components"]["src/a.ts:canonical"]["claims"])))')"
eq "code gate cannot resolve aliased security ownership" "src/a.ts:canonical" \
  "$(led record alias-claims --gate code-review --verdict FAIL | field open_blocking)"

led open legacy-alias-claims >/dev/null
led record legacy-alias-claims --gate code-review --verdict FAIL --blocking 'src/a.ts:canonical' >/dev/null
led record legacy-alias-claims --gate security-review --verdict FAIL --blocking 'src/a.ts:drifted' >/dev/null
python3 - "$TMP/.orchestration/.review-ledger" <<'PY'
import json,sys
from pathlib import Path
path=next(p for p in Path(sys.argv[1]).glob("subject-*.json") if json.loads(p.read_text()).get("pr") == "legacy-alias-claims")
state=json.loads(path.read_text())
for component in state["components"].values(): component.pop("claims", None)
path.write_text(json.dumps(state, indent=2, sort_keys=True)+"\n")
PY
led alias legacy-alias-claims --from 'src/a.ts:drifted' --to 'src/a.ts:canonical' >/dev/null
eq "alias backfills every legacy per-gate owner before merge" "code-review,security-review" \
  "$(led status legacy-alias-claims | python3 -c 'import json,sys; print(",".join(sorted(json.load(sys.stdin)["components"]["src/a.ts:canonical"]["claims"])))')"

led open recorded-alias >/dev/null
led record recorded-alias --gate code-review --verdict FAIL --blocking 'src/a.ts:canonical' --blocking 'src/a.ts:old' >/dev/null
led alias recorded-alias --from 'src/a.ts:old' --to 'src/a.ts:canonical' >/dev/null
eq "direct recording resolves persisted aliases" "src/a.ts:canonical" \
  "$(led record recorded-alias --gate code-review --verdict FAIL --blocking 'src/a.ts:old' | field accepted_blocking)"
eq "direct recording cannot recreate an aliased component" "src/a.ts:canonical" \
  "$(led status recorded-alias | python3 -c 'import json,sys; print(",".join(sorted(json.load(sys.stdin)["components"])))')"
ALIAS_REPAIR_HEAD="$(fake_commit alias-repair)"
cat > "$TMP/recorded-alias-repair.json" <<JSON
{"schema_version":1,"head":"$ALIAS_REPAIR_HEAD","findings":[{"component":"src/a.ts:canonical","status":"closed","root_cause":"drift","change":"canonicalized","verification":"alias regression"}]}
JSON
led record-repair recorded-alias --report "$TMP/recorded-alias-repair.json" >/dev/null
led record recorded-alias --gate code-review --verdict FAIL --blocking 'src/a.ts:old' --head "$ALIAS_REPAIR_HEAD" >/dev/null
led complete-repair-review recorded-alias >/dev/null
eq "staged recording resolves persisted aliases" "src/a.ts:canonical" \
  "$(led status recorded-alias | field open_blocking)"

# --- operator cancellation of a started review permit -------------------------
led open cancel-permit-cli >/dev/null
CANCEL_CLI_HEAD="$(git -C "$TMP" rev-parse HEAD)"
UNSTARTED_PERMIT="$(led permit-review cancel-permit-cli --role code-reviewer --head "$CANCEL_CLI_HEAD" | field review_phase_permit)"
if led cancel-permit cancel-permit-cli --phase-permit "$UNSTARTED_PERMIT" --reason "not started" >/dev/null 2> "$TMP/cancel-unstarted-error"; then
  bad "cancel-permit refuses an unstarted permit"
elif grep -q 'reissues it' "$TMP/cancel-unstarted-error"; then
  ok "cancel-permit refuses an unstarted permit"
else bad "cancel-permit explains idempotent reissue for an unstarted permit"; fi
python3 - "$TMP" "$UNSTARTED_PERMIT" "$CANCEL_CLI_HEAD" "$LEDGER" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("review_permit",str(Path(sys.argv[4]).with_name("review_permit.py")))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.consume(shared_root=Path(sys.argv[1]),ledger_dir=".orchestration/.review-ledger",pr="cancel-permit-cli",token=sys.argv[2],role="code-reviewer",head=sys.argv[3],timestamp="start")
PY
if led permit-review cancel-permit-cli --role code-reviewer --head "$CANCEL_CLI_HEAD" >/dev/null 2> "$TMP/started-refusal"; then
  bad "a started permit blocks another reviewer"
elif grep -q "permit ${UNSTARTED_PERMIT:0:14}" "$TMP/started-refusal" \
  && ! grep -q "$UNSTARTED_PERMIT" "$TMP/started-refusal" \
  && grep -q 'api_agent.py reconcile' "$TMP/started-refusal" \
  && grep -q 'cancel-permit cancel-permit-cli' "$TMP/started-refusal"; then
  ok "started-permit refusal names the permit prefix and both recovery commands"
else bad "started-permit refusal names the permit prefix and both recovery commands"; fi
if led cancel-permit cancel-permit-cli --phase-permit "$UNSTARTED_PERMIT" --role security-reviewer --reason "wrong role" >/dev/null 2>&1; then
  bad "cancel-permit refuses a mismatched role"
else ok "cancel-permit refuses a mismatched role"; fi
eq "cancel-permit cancels a started permit with no open provider work" "cancelled" \
  "$(led cancel-permit cancel-permit-cli --phase-permit "$UNSTARTED_PERMIT" --role code-reviewer --reason 'reviewer process was killed' | python3 -c 'import json,sys; print(json.load(sys.stdin)["permit_cancelled"]["status"])')"
eq "cancel-permit records its reason without a receipt" "reviewer process was killed|" \
  "$(python3 -c 'import json,sys; p=next(p for p in json.load(open(sys.argv[1]))["review_permits"] if p["token"]==sys.argv[2]); print(p["cancellation_reason"]+"|"+p["completion_receipt"])' "$(led open cancel-permit-cli | field ledger)" "$UNSTARTED_PERMIT")"
if led cancel-permit cancel-permit-cli --phase-permit "$UNSTARTED_PERMIT" --reason "again" >/dev/null 2>&1; then
  bad "cancel-permit refuses an already cancelled permit"
else ok "cancel-permit refuses an already cancelled permit"; fi
RECOVERED_PERMIT="$(led permit-review cancel-permit-cli --role code-reviewer --head "$CANCEL_CLI_HEAD" | field review_phase_permit)"
if [ -n "$RECOVERED_PERMIT" ] && [ "$RECOVERED_PERMIT" != "$UNSTARTED_PERMIT" ]; then
  ok "a cancelled permit frees the gate for a new permit"
else bad "a cancelled permit frees the gate for a new permit"; fi
led complete-review cancel-permit-cli --role code-reviewer --phase-permit "$RECOVERED_PERMIT" --result "$TMP/concurrent-code.json" >/dev/null
if led cancel-permit cancel-permit-cli --phase-permit "$RECOVERED_PERMIT" --reason "completed" >/dev/null 2> "$TMP/cancel-completed-error"; then
  bad "cancel-permit refuses a completed permit"
elif grep -q 'completion receipt' "$TMP/cancel-completed-error"; then
  ok "cancel-permit refuses a completed permit"
else bad "cancel-permit explains a completed permit refusal"; fi

# --- repair briefs keep every gate's explanation for a shared component -------
cat > "$TMP/shared-code-fail.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"review","status":"fail"}],"findings":[{"component":".github/workflows/owner-qa-hold.yml:owner-qa-hold","disposition":"blocking","severity":"high","title":"Hold never releases","explanation":"CODE-EXPLANATION the hold label is never removed after owner QA passes.","regression":false}]}
JSON
cat > "$TMP/shared-security-fail.json" <<'JSON'
{"schema_version":1,"gate":"security-review","verdict":"FAIL","checks":[{"name":"review","status":"fail"}],"findings":[{"component":".github/workflows/owner-qa-hold.yml:owner-qa-hold","disposition":"blocking","severity":"high","title":"Untrusted checkout with secrets","explanation":"SECURITY-EXPLANATION pull_request_target checks out fork code with write secrets.","regression":false}]}
JSON
shared_gate_record() {
  local pr="$1" gate="$2" file="$3" head permit
  head="$(git -C "$TMP" rev-parse HEAD)"
  permit="$(led permit-review "$pr" --role "$gate-reviewer" --head "$head" | field review_phase_permit)" || return
  led complete-review "$pr" --role "$gate-reviewer" --phase-permit "$permit" --result "$file" >/dev/null || return
  led record "$pr" --gate "$gate-review" --result "$file" --head "$head" --phase-permit "$permit" >/dev/null
}
for order in code-first security-first; do
  led open "shared-brief-$order" >/dev/null
  if [ "$order" = code-first ]; then first=code; second=security; else first=security; second=code; fi
  shared_gate_record "shared-brief-$order" "$first" "$TMP/shared-$first-fail.json"
  shared_gate_record "shared-brief-$order" "$second" "$TMP/shared-$second-fail.json"
  brief="$(led repair-brief "shared-brief-$order")"
  if printf '%s' "$brief" | grep -q '\[code-review\] Hold never releases: CODE-EXPLANATION' \
    && printf '%s' "$brief" | grep -q '\[security-review\] Untrusted checkout with secrets: SECURITY-EXPLANATION'; then
    ok "repair brief keeps both gate explanations for a shared component ($order)"
  else
    printf 'FAIL repair brief keeps both gate explanations for a shared component (%s)\n%s\n' "$order" "$brief"; fails=$((fails + 1))
  fi
  eq "repair brief labels gates in stable order ($order)" "code-review,security-review" \
    "$(printf '%s' "$brief" | sed -n 's/^  \[\([a-z-]*\)\].*/\1/p' | paste -sd, -)"
  handoff="$(led handoff "shared-brief-$order")"
  if printf '%s' "$handoff" | grep -q 'CODE-EXPLANATION' && printf '%s' "$handoff" | grep -q 'SECURITY-EXPLANATION'; then
    ok "handoff keeps both gate explanations for a shared component ($order)"
  else bad "handoff keeps both gate explanations for a shared component ($order)"; fi
done
SHARED_HEAD="$(git -C "$TMP" rev-parse HEAD)"
cat > "$TMP/shared-repair.json" <<JSON
{"schema_version":1,"head":"$SHARED_HEAD","findings":[{"component":".github/workflows/owner-qa-hold.yml:owner-qa-hold","status":"closed","root_cause":"hold","change":"release hold","verification":"workflow regression"}]}
JSON
led record-repair shared-brief-code-first --report "$TMP/shared-repair.json" >/dev/null
shared_gate_record shared-brief-code-first security "$TMP/shared-security-fail.json"
shared_gate_record shared-brief-code-first code "$TMP/shared-code-fail.json"
led complete-repair-review shared-brief-code-first >/dev/null
staged_brief="$(led repair-brief shared-brief-code-first)"
if printf '%s' "$staged_brief" | grep -q '\[code-review\].*CODE-EXPLANATION' \
  && printf '%s' "$staged_brief" | grep -q '\[security-review\].*SECURITY-EXPLANATION'; then
  ok "staged repair finalization keeps both gate explanations"
else printf 'FAIL staged repair finalization keeps both gate explanations\n%s\n' "$staged_brief"; fails=$((fails + 1)); fi
python3 - "$TMP/.orchestration/.review-ledger" <<'PY'
import json,sys
from pathlib import Path
path=next(p for p in Path(sys.argv[1]).glob("subject-*.json") if json.loads(p.read_text()).get("pr") == "shared-brief-security-first")
state=json.loads(path.read_text())
for component in state["components"].values(): component.pop("findings_by_gate", None)
path.write_text(json.dumps(state, indent=2, sort_keys=True)+"\n")
PY
legacy_brief="$(led repair-brief shared-brief-security-first)"
if printf '%s' "$legacy_brief" | grep -q '^  Hold never releases: CODE-EXPLANATION'; then
  ok "legacy single-finding ledgers still render in the repair brief"
else printf 'FAIL legacy single-finding ledgers still render in the repair brief\n%s\n' "$legacy_brief"; fails=$((fails + 1)); fi
if led handoff shared-brief-security-first | grep -q 'CODE-EXPLANATION'; then
  ok "legacy single-finding ledgers still render in the handoff"
else bad "legacy single-finding ledgers still render in the handoff"; fi

# --- v0.7 ledgers preserve their already-spent budget -------------------------
cat > "$TMP/.orchestration/.review-ledger/pr-legacy.json" <<'JSON'
{"schema_version":1,"pr":"legacy","created_at":"2026-01-01T00:00:00+00:00","updated_at":"2026-01-01T00:00:00+00:00","max_rounds":2,"rounds":[{"round":1,"gate":"code-review","scope_mode":"full-authority","claimed_verdict":"FAIL","effective_verdict":"FAIL","recorded_at":"2026-01-01T00:00:00+00:00","blocking":["src/a.ts:foo"],"advisory":[],"resolved":[]}],"components":{"src/a.ts:foo":{"key":"src/a.ts:foo","display":"src/a.ts:foo","strikes":1,"status":"open","first_round":1,"last_round":1,"rounds":[1],"gates":["code-review"],"redesigned_at_strike":0}},"escalated":false}
JSON
eq "v0.7 failed passes retain their spent repair budget" "1" "$(led status legacy | field fix_cycles)"
if led --ledger-dir "$TMP/fresh-ledger" open escape >/dev/null 2>&1; then
  bad "absolute review ledger override must fail closed"
else ok "absolute review ledger override fails closed"; fi

# --- configured gates are required whether or not a permit was issued ---------
# Required gates used to be derived only from issued permits, so a generation
# that issued only a security permit could clear without any code review.
write_config "$BOTH_GATES"
led open security-permit-only >/dev/null
SECURITY_ONLY_HEAD="$(git -C "$TMP" rev-parse HEAD)"
led permit-review security-permit-only --role security-reviewer --head "$SECURITY_ONLY_HEAD" >/dev/null
record_pass security-permit-only security >/dev/null
security_only_status="$(led status security-permit-only)"
eq "a configured code gate is required without an issued code permit" "code-review" \
  "$(printf '%s' "$security_only_status" | field missing_gates)"
eq "a security-only generation cannot clear configured gates" "review" \
  "$(printf '%s' "$security_only_status" | field next_action)"

write_config $'gates: [code-review, security-review]  # flow list\n'
led open flow-list-gates >/dev/null
eq "a one-line flow gate list is enforced like a block list" "security-review" \
  "$(record_pass flow-list-gates code | field missing_gates)"

write_config ""
led open default-gates >/dev/null
eq "a missing gates key fails closed to the template gate set" "code-review,security-review" \
  "$(led status default-gates | field required_gates)"

write_config "$CODE_GATE"
led open configured-code-only >/dev/null
eq "a code-only gate configuration requires only code review" "code-review" \
  "$(led status configured-code-only | field required_gates)"
eq "a code-only gate configuration clears after code review" "gates-clear" \
  "$(record_pass configured-code-only code | field next_action)"

write_config $'gates:\n  - security-review\n'
led open code-gate-omitted >/dev/null
eq "a gate list that omits code review still requires it" "code-review,security-review" \
  "$(led status code-gate-omitted | field required_gates)"

write_config $'gates:\n  - visual-qa\n'
led open non-ledger-gates-only >/dev/null
eq "a gate list with only non-ledger gates still requires code review" "code-review" \
  "$(led status non-ledger-gates-only | field required_gates)"

write_config "$BOTH_GATES"
led open security-decision-missing >/dev/null
missing_decision="$(record_pass security-decision-missing code)"
eq "a configured security gate without a recorded decision stays required" "security-review" \
  "$(printf '%s' "$missing_decision" | field missing_gates)"
eq "a missing security decision cannot clear the gates" "review" \
  "$(printf '%s' "$missing_decision" | field next_action)"

SECURITY_DECISION_HEAD="$(git -C "$TMP" rev-parse HEAD)"
cat > "$TMP/security-not-required.json" <<'JSON'
{"reasons":[],"required":false,"source_branch":"feature/docs","target_branch":"develop"}
JSON
cat > "$TMP/security-required.json" <<'JSON'
{"reasons":[{"kind":"diff_path","pattern":"auth","value":"src/auth.ts"}],"required":true,"source_branch":"feature/auth","target_branch":"develop"}
JSON
cat > "$TMP/security-inconsistent.json" <<'JSON'
{"reasons":[{"kind":"diff","pattern":"always","value":"always"}],"required":false,"source_branch":"feature/x","target_branch":"develop"}
JSON
led open security-decision-waived >/dev/null
waived="$(led record-security-gate security-decision-waived --head "$SECURITY_DECISION_HEAD" --decision "$TMP/security-not-required.json")"
eq "a not-required security decision waives the configured security gate" "code-review" \
  "$(printf '%s' "$waived" | field required_gates)"
eq "the waived generation reports its security decision" "not-required" \
  "$(printf '%s' "$waived" | field security_gate_decision)"
eq "a waived security gate clears with only code review" "gates-clear" \
  "$(record_pass security-decision-waived code | field next_action)"
if led record-security-gate security-decision-waived --head "${SECURITY_DECISION_HEAD:0:12}" \
  --decision "$TMP/security-not-required.json" >/dev/null 2>&1; then
  bad "a security decision requires the exact full head"
else ok "a security decision requires the exact full head"; fi
if led record-security-gate security-decision-waived --head "$SECURITY_DECISION_HEAD" \
  --decision "$TMP/security-inconsistent.json" >/dev/null 2>&1; then
  bad "a security decision whose reasons contradict required=false is refused"
else ok "a security decision whose reasons contradict required=false is refused"; fi
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm security-rebind
SECURITY_REBIND_HEAD="$(git -C "$TMP" rev-parse HEAD)"
eq "a security waiver does not carry into a new-head generation" "code-review,security-review" \
  "$(led rebind-generation security-decision-waived --head "$SECURITY_REBIND_HEAD" --reason 'merged develop' | field required_gates)"

led open security-decision-required >/dev/null
led record-security-gate security-decision-required --head "$SECURITY_REBIND_HEAD" \
  --decision "$TMP/security-required.json" >/dev/null
led record-security-gate security-decision-required --head "$SECURITY_REBIND_HEAD" \
  --decision "$TMP/security-not-required.json" >/dev/null
eq "any required security decision for the generation keeps the gate required" "code-review,security-review" \
  "$(led status security-decision-required | field required_gates)"

led open security-permit-decision >/dev/null
led permit-review security-permit-decision --role security-reviewer --head "$SECURITY_REBIND_HEAD" >/dev/null
led record-security-gate security-permit-decision --head "$SECURITY_REBIND_HEAD" \
  --decision "$TMP/security-not-required.json" >/dev/null
eq "an issued security permit keeps the gate required despite a waiver" "code-review,security-review" \
  "$(led status security-permit-decision | field required_gates)"

# Ledgers written by the affected runtime stored a short gate set on the
# pending repair attempt. Reading them must recompute the configured set.
led open short-repair-gates >/dev/null
led record short-repair-gates --gate code-review --verdict FAIL --blocking 'src/short.ts:gate' >/dev/null
SHORT_GATES_HEAD="$(fake_commit short-repair-gates)"
cat > "$TMP/short-repair-gates.json" <<JSON
{"schema_version":1,"head":"$SHORT_GATES_HEAD","findings":[{"component":"src/short.ts:gate","status":"closed","root_cause":"missing gate","change":"added gate","verification":"gate regression passes"}]}
JSON
led record-repair short-repair-gates --report "$TMP/short-repair-gates.json" >/dev/null
SHORT_GATES_LEDGER="$(led open short-repair-gates | field ledger)"
python3 - "$SHORT_GATES_LEDGER" <<'PY'
import json,sys
state=json.load(open(sys.argv[1]))
state["repair_attempts"][-1]["required_gates"]=["code-review"]
json.dump(state,open(sys.argv[1],"w"),indent=2,sort_keys=True)
PY
led record short-repair-gates --gate code-review --verdict FAIL --head "$SHORT_GATES_HEAD" >/dev/null
eq "a stored short repair gate set is recomputed from configured gates" "security-review" \
  "$(led status short-repair-gates | field missing_gates)"
if led complete-repair-review short-repair-gates >/dev/null 2>&1; then
  bad "a repair review cannot complete without a configured-required gate"
else ok "a repair review cannot complete without a configured-required gate"; fi
write_config "$CODE_GATE"

# --- abbreviated repaired heads resolve to one exact commit -------------------
led open abbreviated-repair >/dev/null
led record abbreviated-repair --gate code-review --verdict FAIL --blocking 'src/abbrev.ts:head' >/dev/null
ABBREV_HEAD="$(git -C "$TMP" rev-parse HEAD)"
cat > "$TMP/abbreviated-repair.json" <<JSON
{"schema_version":1,"head":"${ABBREV_HEAD:0:7}","findings":[{"component":"src/abbrev.ts:head","status":"closed","root_cause":"short sha","change":"full sha","verification":"head regression passes"}]}
JSON
eq "record-repair stores the full resolved repaired head" "$ABBREV_HEAD" \
  "$(led record-repair abbreviated-repair --report "$TMP/abbreviated-repair.json" | field head)"
if record_pass abbreviated-repair code >/dev/null 2>&1; then
  ok "an abbreviated repair report accepts the full-SHA review"
else bad "an abbreviated repair report accepts the full-SHA review"; fi
eq "an abbreviated repair report no longer deadlocks the ledger" "gates-clear" \
  "$(led complete-repair-review abbreviated-repair 2>/dev/null | field next_action)"

led open unresolvable-repair >/dev/null
led record unresolvable-repair --gate code-review --verdict FAIL --blocking 'src/abbrev.ts:missing' >/dev/null
cat > "$TMP/unresolvable-repair.json" <<'JSON'
{"schema_version":1,"head":"0000000","findings":[{"component":"src/abbrev.ts:missing","status":"closed","root_cause":"x","change":"y","verification":"z"}]}
JSON
if led record-repair unresolvable-repair --report "$TMP/unresolvable-repair.json" >/dev/null 2>&1; then
  bad "a repair head that does not resolve to a commit is refused"
else ok "a repair head that does not resolve to a commit is refused"; fi

# Existing ledgers already stored the abbreviation.
legacy_abbreviate() {
  python3 - "$(led open "$1" | field ledger)" <<'PY'
import json,sys
state=json.load(open(sys.argv[1]))
state["repair_attempts"][-1]["head"]=state["repair_attempts"][-1]["head"][:7]
json.dump(state,open(sys.argv[1],"w"),indent=2,sort_keys=True)
PY
}
led open legacy-abbreviated >/dev/null
led record legacy-abbreviated --gate code-review --verdict FAIL --blocking 'src/abbrev.ts:legacy' >/dev/null
cat > "$TMP/legacy-abbreviated.json" <<JSON
{"schema_version":1,"head":"$ABBREV_HEAD","findings":[{"component":"src/abbrev.ts:legacy","status":"closed","root_cause":"short sha","change":"full sha","verification":"head regression passes"}]}
JSON
led record-repair legacy-abbreviated --report "$TMP/legacy-abbreviated.json" >/dev/null
legacy_abbreviate legacy-abbreviated
OTHER_ABBREV_HEAD="$(fake_commit other-abbreviated)"
if led record legacy-abbreviated --gate code-review --verdict FAIL --head "$OTHER_ABBREV_HEAD" >/dev/null 2>&1; then
  bad "a stored abbreviation does not accept an unrelated full head"
else ok "a stored abbreviation does not accept an unrelated full head"; fi
if record_pass legacy-abbreviated code >/dev/null 2>&1; then
  ok "a stored abbreviation accepts its unambiguous full expansion"
else bad "a stored abbreviation accepts its unambiguous full expansion"; fi

led open correct-abbreviated >/dev/null
led record correct-abbreviated --gate code-review --verdict FAIL --blocking 'src/abbrev.ts:correct' >/dev/null
cat > "$TMP/correct-abbreviated.json" <<JSON
{"schema_version":1,"head":"$ABBREV_HEAD","findings":[{"component":"src/abbrev.ts:correct","status":"closed","root_cause":"short sha","change":"full sha","verification":"head regression passes"}]}
JSON
led record-repair correct-abbreviated --report "$TMP/correct-abbreviated.json" >/dev/null
legacy_abbreviate correct-abbreviated
if led correct-repair-head correct-abbreviated --head "$OTHER_ABBREV_HEAD" --reason 'wrong commit' >/dev/null 2>&1; then
  bad "correct-repair-head refuses a different commit"
else ok "correct-repair-head refuses a different commit"; fi
if led correct-repair-head correct-abbreviated --head "$ABBREV_HEAD" --reason ' ' >/dev/null 2>&1; then
  bad "correct-repair-head requires an audit reason"
else ok "correct-repair-head requires an audit reason"; fi
corrected="$(led correct-repair-head correct-abbreviated --head "$ABBREV_HEAD" --reason 'expand abbreviated repair head')"
eq "correct-repair-head stores the unambiguous full expansion" "$ABBREV_HEAD" \
  "$(printf '%s' "$corrected" | field head)"
CORRECT_LEDGER="$(led open correct-abbreviated | field ledger)"
eq "correct-repair-head records an audit entry" "${ABBREV_HEAD:0:7}->$ABBREV_HEAD:expand abbreviated repair head" \
  "$(python3 -c 'import json,sys; e=json.load(open(sys.argv[1]))["repair_head_corrections"][-1]; print(e["from_head"]+"->"+e["head"]+":"+e["reason"])' "$CORRECT_LEDGER")"
if led correct-repair-head correct-abbreviated --head "$ABBREV_HEAD" --reason 'again' >/dev/null 2>&1; then
  bad "correct-repair-head refuses a non-abbreviated stored head"
else ok "correct-repair-head refuses a non-abbreviated stored head"; fi
record_pass correct-abbreviated code >/dev/null
led complete-repair-review correct-abbreviated >/dev/null
python3 - "$CORRECT_LEDGER" <<'PY'
import json,sys
state=json.load(open(sys.argv[1]))
state["repair_attempts"][-1]["head"]=state["repair_attempts"][-1]["head"][:7]
json.dump(state,open(sys.argv[1],"w"),indent=2,sort_keys=True)
PY
if led correct-repair-head correct-abbreviated --head "$ABBREV_HEAD" --reason 'completed' >/dev/null 2>&1; then
  bad "correct-repair-head refuses a completed repair attempt"
else ok "correct-repair-head refuses a completed repair attempt"; fi

# --- a desktop completion cannot mint a receipt for a started API review ------
led open api-started >/dev/null
STARTED_HEAD="$(git -C "$TMP" rev-parse HEAD)"
STARTED_PERMIT="$(led permit-review api-started --role code-reviewer --head "$STARTED_HEAD" | field review_phase_permit)"
python3 - "$TMP" "$STARTED_PERMIT" "$STARTED_HEAD" "$LEDGER" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("review_permit",str(Path(sys.argv[4]).with_name("review_permit.py")))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.consume(shared_root=Path(sys.argv[1]),ledger_dir=".orchestration/.review-ledger",pr="api-started",token=sys.argv[2],role="code-reviewer",head=sys.argv[3],timestamp="start")
PY
cat > "$TMP/api-started-forged.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
if led complete-review api-started --role code-reviewer --phase-permit "$STARTED_PERMIT" \
  --result "$TMP/api-started-forged.json" > /dev/null 2> "$TMP/api-started-error"; then
  bad "complete-review refuses a permit an API run started"
elif grep -q 'started by the API runner' "$TMP/api-started-error"; then
  ok "complete-review refuses a permit an API run started"
else bad "complete-review explains why a started API permit is refused"; fi

# --- complete-review after an API runner already completed the permit ---------
led open api-completed >/dev/null
API_HEAD="$(git -C "$TMP" rev-parse HEAD)"
API_PERMIT="$(led permit-review api-completed --role code-reviewer --head "$API_HEAD" | field review_phase_permit)"
cat > "$TMP/api-completed.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"review","status":"pass"}],"findings":[]}
JSON
API_RECEIPT="$(python3 - "$TMP" "$API_PERMIT" "$API_HEAD" "$LEDGER" "$TMP/api-completed.json" <<'PY'
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("review_permit",str(Path(sys.argv[4]).with_name("review_permit.py")))
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
common=dict(shared_root=Path(sys.argv[1]),ledger_dir=".orchestration/.review-ledger",pr="api-completed",token=sys.argv[2],role="code-reviewer",head=sys.argv[3])
module.consume(**common,timestamp="start")
print(module.complete(**common,result=json.load(open(sys.argv[5])),timestamp="done"))
PY
)"
api_complete="$(led complete-review api-completed --role code-reviewer --phase-permit "$API_PERMIT" --result "$TMP/api-completed.json" 2>/dev/null)"
eq "complete-review returns the API runner's existing receipt" "$API_RECEIPT" \
  "$(printf '%s' "$api_complete" | field completion_receipt 2>/dev/null)"
eq "complete-review reports an already completed permit" "True" \
  "$(printf '%s' "$api_complete" | field already_completed 2>/dev/null)"
if led complete-review api-completed --role code-reviewer --phase-permit "$API_PERMIT" \
  --result "$TMP/invalid-output-review.json" >/dev/null 2>&1; then
  bad "an invalid result cannot reuse a completed permit"
else ok "an invalid result cannot reuse a completed permit"; fi
if led complete-review api-completed --role code-reviewer --phase-permit "$API_PERMIT" \
  --result "$TMP/corrected-output-review.json" > /dev/null 2> "$TMP/api-digest-error"; then
  bad "a different result cannot reuse a completed permit"
elif grep -q 'different result' "$TMP/api-digest-error"; then
  ok "a different result cannot reuse a completed permit"
else bad "a different result explains the digest mismatch"; fi
eq "the API receipt records directly after completion" "gates-clear" \
  "$(led record api-completed --gate code-review --result "$TMP/api-completed.json" --head "$API_HEAD" --phase-permit "$API_PERMIT" | field next_action)"
if led complete-review api-completed --role code-reviewer --phase-permit "$API_PERMIT" \
  --result "$TMP/api-completed.json" > /dev/null 2> "$TMP/api-consumed-error"; then
  bad "a consumed completion receipt cannot be returned again"
elif grep -q 'already recorded' "$TMP/api-consumed-error"; then
  ok "a consumed completion receipt cannot be returned again"
else bad "a consumed completion receipt explains that it was recorded"; fi

# --- review from a detached worktree at the exact PR head ---------------------
led open worktree-review >/dev/null
WORKTREE_PR_HEAD="$(fake_commit worktree-pr-head)"
MAIN_HEAD="$(git -C "$TMP" rev-parse HEAD)"
if led permit-review worktree-review --role code-reviewer --head "$WORKTREE_PR_HEAD" \
  > /dev/null 2> "$TMP/head-mismatch-error"; then
  bad "permit-review refuses a PR head that is not checked out"
elif grep -q "$MAIN_HEAD" "$TMP/head-mismatch-error" \
  && grep -q 'git worktree add --detach' "$TMP/head-mismatch-error"; then
  ok "the head mismatch names the actual HEAD and the review worktree procedure"
else bad "the head mismatch names the actual HEAD and the review worktree procedure"; fi
git -C "$TMP" worktree add -q --detach "$REVIEW_WT" "$WORKTREE_PR_HEAD"
wt() { (cd "$REVIEW_WT" && python3 "$LEDGER" "$@"); }
if WT_PERMIT="$(wt permit-review worktree-review --role code-reviewer --head "$WORKTREE_PR_HEAD" | field review_phase_permit)"; then
  ok "permit-review succeeds from a detached worktree at the PR head"
else bad "permit-review succeeds from a detached worktree at the PR head"; fi
wt complete-review worktree-review --role code-reviewer --phase-permit "$WT_PERMIT" --result "$TMP/api-completed.json" >/dev/null
wt record worktree-review --gate code-review --result "$TMP/api-completed.json" --head "$WORKTREE_PR_HEAD" --phase-permit "$WT_PERMIT" >/dev/null
git -C "$TMP" worktree remove "$REVIEW_WT"
eq "the main checkout sees the review recorded from the review worktree" "gates-clear" \
  "$(led status worktree-review | field next_action)"
eq "the review worktree bound the exact PR head" "$WORKTREE_PR_HEAD" \
  "$(led status worktree-review | field generation_head)"

echo
if [ "$fails" -eq 0 ]; then echo "review ledger tests passed"; else echo "$fails FAILED"; fi
exit "$fails"
