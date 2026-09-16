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
trap 'rm -rf "$TMP" "$LANE"' EXIT
git -C "$TMP" init -q .
git -C "$TMP" -c user.name=Test -c user.email=test@example.com commit --allow-empty -qm initial
mkdir -p "$TMP/.orchestration"
printf 'minimum_orka_version: ""\n' > "$TMP/.orchestration/config.yaml"

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
cat > "$TMP/scope-repair.json" <<'JSON'
{"schema_version":1,"head":"abcdef2","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"},{"component":"src/b.ts:bar","status":"closed","root_cause":"missing guard","change":"added guard","verification":"guard regression passes"}]}
JSON
led record-repair 2 --report "$TMP/scope-repair.json" >/dev/null
out="$(led record 2 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/new.ts:nit' --head abcdef2)"
eq "a new non-regression finding is demoted in a frozen round" "src/new.ts:nit" "$(printf '%s' "$out" | field demoted_to_advisory)"
eq "a known component still blocks in a frozen round" "src/a.ts:foo" "$(printf '%s' "$out" | field accepted_blocking)"
led complete-repair-review 2 >/dev/null
eq "a component the repaired generation stopped reporting auto-resolves" "resolved" \
  "$(led status 2 | python3 -c 'import json,sys; print(json.load(sys.stdin)["components"]["src/b.ts:bar"]["status"])')"
eq "the blocking set shrank" "src/a.ts:foo" "$(led status 2 | field open_blocking)"

led open 3 >/dev/null
led record 3 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' >/dev/null
cat > "$TMP/regression-repair.json" <<'JSON'
{"schema_version":1,"head":"abcdef3","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"}]}
JSON
led record-repair 3 --report "$TMP/regression-repair.json" >/dev/null
eq "a declared regression keeps blocking authority in a frozen round" \
  "src/a.ts:foo,src/broke.ts:oops" \
  "$(led record 3 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --blocking 'src/broke.ts:oops' --regression 'src/broke.ts:oops' --head abcdef3 | field accepted_blocking)"

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
cat > "$TMP/staged-repair.json" <<'JSON'
{"schema_version":1,"head":"abcdef9","findings":[{"component":"src/staged.py:check","status":"closed","root_cause":"shared boundary","change":"fixed shared boundary","verification":"both gate regressions pass"}]}
JSON
led record-repair staged-generation --report "$TMP/staged-repair.json" >/dev/null
led record staged-generation --gate security-review --verdict FAIL --head abcdef9 >/dev/null
eq "partial generation does not finalize component claims" "src/staged.py:check" \
  "$(led status staged-generation | field open_blocking)"
led record staged-generation --gate code-review --verdict FAIL --head abcdef9 >/dev/null
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
printf 'minimum_orka_version: 99.0.0\n' > "$TMP/.orchestration/config.yaml"
if led permit-review minimum-version-review --role code-reviewer --head "$BLOCKED_REBIND_HEAD" >/dev/null 2>&1; then
  bad "review permits fail closed below the repository minimum Orka version"
else ok "review permits fail closed below the repository minimum Orka version"; fi
printf 'minimum_orka_version: ""\n' > "$TMP/.orchestration/config.yaml"

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
STALE_PERMIT="$(led permit-review 5 --role code-reviewer --head "$STALE_HEAD" | field review_phase_permit)"
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef1 >/dev/null
led repair-brief 5 | grep -q 'stable finding ID' && ok "repair brief carries stable IDs" || bad "repair brief carries stable IDs"
cat > "$TMP/repair-1.json" <<'JSON'
{"schema_version":1,"head":"abcdef1","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"wrong branch","change":"corrected branch","verification":"named regression passes"}]}
JSON
eq "recording a repair starts a pending review" "True" "$(led record-repair 5 --report "$TMP/repair-1.json" | field repair_pending_review)"
if led complete-review 5 --role code-reviewer --phase-permit "$STALE_PERMIT" --result "$TMP/concurrent-code.json" >/dev/null 2>&1; then
  bad "superseded generation permit cannot complete"
else ok "superseded generation permit cannot complete"; fi
if led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef2 >/dev/null 2>&1; then
  bad "a reviewer cannot record against the wrong repaired head"
else ok "a reviewer cannot record against the wrong repaired head"; fi
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef1 >/dev/null
eq "a repaired head must complete its required gate set" \
  "redesign" "$(led complete-repair-review 5 | field next_action)"
eq "a passing design gate releases the component for another fix" \
  "review" "$(led redesign 5 --key 'src/a.ts:foo' --verdict PASS | field next_action)"
cat > "$TMP/repair-2.json" <<'JSON'
{"schema_version":1,"head":"abcdef2","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"boundary missed","change":"fixed boundary","verification":"boundary regression passes"}]}
JSON
led record-repair 5 --report "$TMP/repair-2.json" >/dev/null
led record 5 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef2 >/dev/null
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
if led brief 7 | grep -q 'Key every finding as `\[component:'; then
  bad "review brief does not instruct reviewers to wrap JSON component keys"
else ok "review brief does not instruct reviewers to wrap JSON component keys"; fi
led brief 7 | grep -q "Investigate uncertainty before the verdict" && ok "round 1 briefs require evidence before blocking" || bad "round 1 briefs require evidence before blocking"
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef7 >/dev/null
cat > "$TMP/repair-7.json" <<'JSON'
{"schema_version":1,"head":"abcdef7","findings":[{"component":"src/a.ts:foo","status":"closed","root_cause":"bad condition","change":"fixed condition","verification":"regression passes"}]}
JSON
led record-repair 7 --report "$TMP/repair-7.json" >/dev/null
led record 7 --gate code-review --verdict FAIL --blocking 'src/a.ts:foo' --head abcdef7 >/dev/null
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
cat > "$TMP/recorded-alias-repair.json" <<'JSON'
{"schema_version":1,"head":"abcdef8","findings":[{"component":"src/a.ts:canonical","status":"closed","root_cause":"drift","change":"canonicalized","verification":"alias regression"}]}
JSON
led record-repair recorded-alias --report "$TMP/recorded-alias-repair.json" >/dev/null
led record recorded-alias --gate code-review --verdict FAIL --blocking 'src/a.ts:old' --head abcdef8 >/dev/null
led complete-repair-review recorded-alias >/dev/null
eq "staged recording resolves persisted aliases" "src/a.ts:canonical" \
  "$(led status recorded-alias | field open_blocking)"

# --- v0.7 ledgers preserve their already-spent budget -------------------------
cat > "$TMP/.orchestration/.review-ledger/pr-legacy.json" <<'JSON'
{"schema_version":1,"pr":"legacy","created_at":"2026-01-01T00:00:00+00:00","updated_at":"2026-01-01T00:00:00+00:00","max_rounds":2,"rounds":[{"round":1,"gate":"code-review","scope_mode":"full-authority","claimed_verdict":"FAIL","effective_verdict":"FAIL","recorded_at":"2026-01-01T00:00:00+00:00","blocking":["src/a.ts:foo"],"advisory":[],"resolved":[]}],"components":{"src/a.ts:foo":{"key":"src/a.ts:foo","display":"src/a.ts:foo","strikes":1,"status":"open","first_round":1,"last_round":1,"rounds":[1],"gates":["code-review"],"redesigned_at_strike":0}},"escalated":false}
JSON
eq "v0.7 failed passes retain their spent repair budget" "1" "$(led status legacy | field fix_cycles)"
if led --ledger-dir "$TMP/fresh-ledger" open escape >/dev/null 2>&1; then
  bad "absolute review ledger override must fail closed"
else ok "absolute review ledger override fails closed"; fi

echo
if [ "$fails" -eq 0 ]; then echo "review ledger tests passed"; else echo "$fails FAILED"; fi
exit "$fails"
