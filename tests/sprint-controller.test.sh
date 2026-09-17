#!/usr/bin/env bash
# sprint-controller.test.sh -- scheduling is bounded, resumable, and continues
# independent work past blocked tickets.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
CONTROLLER="$ROOT/tests/sprint_controller_test_driver.py"
CONTROLLER_MODULE="$ROOT/scripts/sprint-controller.py"
TMP="$(mktemp -d)"
WORKER_PIDS=""
trap 'for pid in $WORKER_PIDS; do kill "$pid" 2>/dev/null || true; done; rm -rf "$TMP"' EXIT

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
run_ok() {
  local label="$1"; shift
  if "$@" >/dev/null; then ok "$label"; else fail_case "$label"; fi
}
run_fail() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then fail_case "$label"; else ok "$label"; fi
}
json_check() {
  local label="$1" file="$2" expression="$3"
  if python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); assert eval(sys.argv[2], {"data": data})' "$file" "$expression"; then
    ok "$label"
  else
    fail_case "$label"
  fi
}
wait_unit_absent() {
  # Poll the controller's own liveness predicate for a ticket's execution unit.
  local sprint="$1" ticket="$2"
  python3 - "$CONTROLLER_MODULE" "$sprint" "$ticket" "${UNIT_ABSENT_TIMEOUT:-30}" <<'PY'
import importlib.util,os,sys,time
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
os.environ["ORCHESTRATION_TEST_MODE"] = "1"
spec = importlib.util.spec_from_file_location("sprint_controller", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
sprint, ticket, timeout = sys.argv[2], sys.argv[3], float(sys.argv[4])
cfg = module.settings(module.parser().parse_args(["summary", "--sprint", sprint]))
deadline = time.monotonic() + timeout
while True:
    lane = module.load(module.state_path(cfg["state_dir"], sprint))["tickets"][ticket]
    status = module.execution_unit_status(lane["worker_identity"])
    if status == "absent":
        break
    if time.monotonic() > deadline:
        raise SystemExit(f"{ticket} execution unit is still {status} after {timeout:g}s")
    time.sleep(0.05)
PY
}
jira_receipt() {
  local inventory="$1" artifact="$1.fetch.json" transport="$1.transport.json"
  python3 - "$inventory" "$transport" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); children=sorted(x.upper() for x in value["subtask_keys"])
parents=sorted(x["key"].upper() for x in value["tickets"] if x["key"].upper() not in set(children))
by_key={x["key"].upper():x for x in value["tickets"]}
sprint=value["sprint"]
def fields(item):
  links=[{"type":{"name":"Blocks"},"outwardIssue":{"key":dep}} for dep in item.get("dependencies",[])]
  priority=item.get("priority")
  priority_names={1:"Highest",2:"High",3:"Medium",4:"Low",5:"Lowest"}
  return {"summary":item.get("summary",""),"description":item.get("description",""),"status":{"name":item.get("status","")},
    "priority":({"id":"opaque-"+str(priority),"name":priority_names[int(priority)]} if priority is not None else None),"sprint":sprint,
    "labels":item.get("labels",[]),"issuetype":{"name":item.get("issue_type","Task"),"subtask":item.get("is_subtask",False)},
    "subtasks":[{"key":x} for x in item.get("subtasks",[])],"issuelinks":links,
    **({"parent":{"key":item["parent"]}} if item.get("parent") else {})}
parent_issues=[{"key":key,"fields":fields(by_key[key])} for key in parents]
child_issues=[{"key":key,"fields":fields(by_key[key])} for key in children]
external=sorted({dep for item in value["tickets"] for dep in item.get("dependencies",[]) if dep not in by_key})
transport={"parents":[{"startAt":0,"total":len(parents),"isLast":True,"issues":parent_issues}],
  "children":[{"startAt":0,"total":len(children),"isLast":True,"issues":child_issues}]}
if external:
  statuses=value.get("dependency_status",{})
  transport["external"]=[{"startAt":0,"total":len(external),"isLast":True,
    "issues":[{"key":key,"fields":{"status":{"name":statuses.get(key,"")}}} for key in external]}]
json.dump(transport,open(sys.argv[2],"w"))
PY
  python3 - "$inventory" "$TMP/repo/.orchestration/config.yaml" <<'PY'
import json,re,sys
inventory=json.load(open(sys.argv[1])); path=sys.argv[2]; text=open(path).read()
text=re.sub(r'(?m)^(  project:)\s*.*$', rf'\1 "{inventory["project"]}"', text)
text=re.sub(r'(?m)^sprint_id:\s*.*$', f'sprint_id: {inventory["sprint"]["id"]}', text)
open(path,"w").write(text)
PY
  python3 "$ROOT/tests/jira_fixture_driver.py" "$transport" \
    "$TMP/repo/.orchestration/config.yaml" --inventory-template "$inventory" \
    --artifact "$artifact" --output "$inventory"
}

mkdir -p "$TMP/repo/.orchestration"
git init -q "$TMP/repo"
cat > "$TMP/operator-authority-helper" <<'SH'
#!/usr/bin/env bash
set -eu
command="$1"; shift
[ "$1" = --scope ] && [ -n "$2" ]
python3 - "$2" <<'PY'
import json,re,sys
scope=json.loads(sys.argv[1])
assert isinstance(scope,dict)
assert re.fullmatch(r'[A-Z][A-Z0-9_]*-[0-9]+',str(scope.get('ticket') or ''))
PY
case "$command" in
  restart-grant) exit 3 ;;
  budget-ceiling)
    [ -f "${ORCHESTRATION_TEST_BUDGET_ACTIVE:?}" ] || exit 3
    cat "$ORCHESTRATION_TEST_BUDGET_ACTIVE"
    ;;
  activate-budget)
    IFS= read -r token
    [ -f "${ORCHESTRATION_TEST_BUDGET_CAP:?}" ]
    [ "$(cat "$ORCHESTRATION_TEST_BUDGET_CAP")" = "$token" ]
    mv "$ORCHESTRATION_TEST_BUDGET_CAP" "$ORCHESTRATION_TEST_BUDGET_ACTIVE"
    printf '35.00\n' > "$ORCHESTRATION_TEST_BUDGET_ACTIVE"
    printf '35.00\n'
    ;;
  consume-recovery)
    IFS= read -r token
    cap="${ORCHESTRATION_TEST_RECOVERY_CAP:?}"
    [ -f "$cap" ] && [ "$(cat "$cap")" = "$token" ]
    rm "$cap"
    ;;
  relaunch-ceiling)
    [ "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["ticket"])' "$2")" = "${ORCHESTRATION_TEST_RELAUNCH_TICKET:?}" ] || exit 3
    [ -f "${ORCHESTRATION_TEST_RELAUNCH_ACTIVE:?}" ] || exit 3
    cat "$ORCHESTRATION_TEST_RELAUNCH_ACTIVE"
    ;;
  activate-relaunch)
    [ "$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["ticket"])' "$2")" = "${ORCHESTRATION_TEST_RELAUNCH_TICKET:?}" ]
    IFS= read -r token
    [ -f "${ORCHESTRATION_TEST_RELAUNCH_CAP:?}" ]
    [ "$(cat "$ORCHESTRATION_TEST_RELAUNCH_CAP")" = "$token" ]
    mv "$ORCHESTRATION_TEST_RELAUNCH_CAP" "$ORCHESTRATION_TEST_RELAUNCH_ACTIVE"
    printf '4\n' > "$ORCHESTRATION_TEST_RELAUNCH_ACTIVE"
    printf '4\n'
    ;;
  *) exit 2 ;;
esac
SH
chmod +x "$TMP/operator-authority-helper"
export ORCHESTRATION_TEST_AUTHORITY_HELPER="$TMP/operator-authority-helper"
export ORCHESTRATION_TEST_RECOVERY_CAP="$TMP/operator-recovery.cap"
export ORCHESTRATION_TEST_BUDGET_CAP="$TMP/operator-budget.cap"
export ORCHESTRATION_TEST_BUDGET_ACTIVE="$TMP/operator-budget.active"
export ORCHESTRATION_TEST_RELAUNCH_CAP="$TMP/operator-relaunch.cap"
export ORCHESTRATION_TEST_RELAUNCH_ACTIVE="$TMP/operator-relaunch.active"
export ORCHESTRATION_TEST_RELAUNCH_TICKET="PROJ-61"
cp "$ROOT/templates/config.yaml" "$TMP/repo/.orchestration/config.yaml"
sed -i.bak 's/^concurrency_max:.*/concurrency_max: 2/' "$TMP/repo/.orchestration/config.yaml"
rm "$TMP/repo/.orchestration/config.yaml.bak"

run_fail "public controller CLI cannot enable test evidence" \
  "$ROOT/scripts/sprint-controller.py" --test-only-evidence sync --inventory missing.json

cat > "$TMP/repo/inventory.json" <<'JSON'
{
  "project": "PROJ",
  "sprint": {"id": "42", "name": "Sprint 42"},
  "source_query": "project = PROJ AND sprint = 42",
  "subtask_source_query": "parent in sprint tickets",
  "subtask_keys": [],
  "tickets": [
    {"key": "PROJ-1", "summary": "root", "status": "Ready", "dependencies": [], "subtasks": []},
    {"key": "PROJ-2", "summary": "after root", "status": "Ready", "dependencies": ["PROJ-1", "PROJ-1"], "subtasks": []},
    {"key": "PROJ-3", "summary": "independent", "status": "Ready", "dependencies": [], "subtasks": []},
    {"key": "PROJ-4", "summary": "jira blocked", "status": "Blocked", "dependencies": [], "subtasks": []},
    {"key": "PROJ-5", "summary": "external wait", "status": "Ready", "dependencies": ["EXT-9"], "subtasks": []},
    {"key": "PROJ-6", "summary": "cycle a", "status": "Ready", "dependencies": ["PROJ-7"], "subtasks": []},
    {"key": "PROJ-7", "summary": "cycle b", "status": "Ready", "dependencies": ["PROJ-6"], "subtasks": []},
    {"key": "PROJ-8", "summary": "needs owner", "status": "In Progress", "dependencies": [], "subtasks": []}
  ],
  "dependency_status": {"EXT-9": "In Progress"}
}
JSON
jira_receipt "$TMP/repo/inventory.json"

cd "$TMP/repo" || exit 1
run_ok "sync creates normalized durable checkpoint" "$CONTROLLER" sync --inventory inventory.json
CHECKPOINT="$(find "$TMP/repo/.orchestration/.sprint-state" -name '42-*.json' -print -quit)"
BEFORE_FAILED_FETCH="$(shasum -a 256 "$CHECKPOINT" | awk '{print $1}')"
run_fail "production sync fails closed without Jira credentials" env -u JIRA_API_TOKEN -u JIRA_BASE_URL \
  "$CONTROLLER" sync --inventory-template inventory.json
AFTER_FAILED_FETCH="$(shasum -a 256 "$CHECKPOINT" | awk '{print $1}')"
if [ "$BEFORE_FAILED_FETCH" = "$AFTER_FAILED_FETCH" ]; then
  ok "failed provider inspection preserves the prior checkpoint"
else
  fail_case "failed provider inspection preserves the prior checkpoint"
fi
if "$ROOT/scripts/sprint-controller.py" sync --inventory inventory.json >/dev/null 2>&1; then
  fail_case "test transport cannot authorize production Jira sync"
else
  ok "test transport cannot authorize production Jira sync"
fi
python3 - inventory.json "$TMP/repo/self-sealed.json" "$TMP/repo/self-sealed.fetch.json" <<'PY'
import hashlib,json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["authority"]="provider-network"; artifact["approved_origin"]="https://jira.example"
json.dump(artifact,open(sys.argv[3],"w"),indent=2,sort_keys=True)
value["fetch_artifact"]={"path":sys.argv[3],"sha256":hashlib.sha256(json.dumps(artifact,sort_keys=True,separators=(",",":")).encode()).hexdigest()}
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "self-sealed caller Jira bundle cannot authorize production sync" "$CONTROLLER" sync --inventory "$TMP/repo/self-sealed.json"
python3 - inventory.json "$TMP/repo/gapped.json" "$TMP/repo/gapped.fetch.json" <<'PY'
import copy,json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["queries"][0]["pages"][0]["start_at"]=1
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "gapped Jira pagination is rejected" "$CONTROLLER" sync --inventory "$TMP/repo/gapped.json"
python3 - inventory.json "$TMP/repo/truncated.json" "$TMP/repo/truncated.fetch.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["queries"][0]["pages"][0]["total"]+=1
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "Jira pages truncated before provider total are rejected" "$CONTROLLER" sync --inventory "$TMP/repo/truncated.json"
python3 - inventory.json "$TMP/repo/wrong-items.json" "$TMP/repo/wrong-items.fetch.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["queries"][0]["pages"][0]["item_keys"][0]="PROJ-999"
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "Jira page evidence must bind exact item keys" "$CONTROLLER" sync --inventory "$TMP/repo/wrong-items.json"
"$CONTROLLER" plan --sprint 42 > "$TMP/plan1.json"
json_check "plan fills exactly two lanes" "$TMP/plan1.json" 'data["launch"] == ["PROJ-1", "PROJ-3"] and data["concurrency_max"] == 2'
json_check "dependency and cycle tickets wait without stopping independent work" "$TMP/plan1.json" 'len(data["waiting"]) == 4'

"$CONTROLLER" reserve --sprint 42 --ticket PROJ-1 --run-ref pending-one > "$TMP/reserve1.json" && ok "first lane reserves atomically" || bad "first lane reserves atomically"
"$CONTROLLER" reserve --sprint 42 --ticket PROJ-3 --run-ref pending-three > "$TMP/reserve3.json" && ok "second lane reserves atomically" || bad "second lane reserves atomically"
TOKEN1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve1.json")"
ATTACH1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attach_capability"])' "$TMP/reserve1.json")"
TOKEN3="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve3.json")"
run_fail "third reservation is rejected at concurrency_max" "$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref should-fail
run_fail "caller-supplied live PID cannot be attached" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --worker-pid "$$" --attach-capability "$ATTACH1"
run_fail "stale capability cannot create controller launch evidence" "$CONTROLLER" launch-local --sprint 42 --ticket PROJ-1 --attach-capability attach_stale --output .orchestration/worker1.log -- /bin/sh -c 'sleep 30'
"$CONTROLLER" launch-local --sprint 42 --ticket PROJ-1 --attach-capability "$ATTACH1" --output .orchestration/worker1.log -- /bin/sh -c 'sleep 30' > "$TMP/launch1.json"
LAUNCH1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["launch_evidence"])' "$TMP/launch1.json")"
PID1="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker_pid"])' "$TMP/launch1.json")"
WORKER_PIDS="$WORKER_PIDS $PID1"
run_ok "attach consumes controller-owned launch evidence" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --launch-evidence "$LAUNCH1"
run_fail "controller launch evidence is one-use" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --launch-evidence "$LAUNCH1"

"$CONTROLLER" plan --sprint 42 > "$TMP/restart.json"
json_check "restart exposes running work for reconciliation" "$TMP/restart.json" 'data["needs_reconcile"] == ["PROJ-1", "PROJ-3"] and data["launch"] == []'
"$CONTROLLER" summary --sprint 42 > "$TMP/restart-summary.json"
json_check "public sprint summary does not disclose attempt capabilities" "$TMP/restart-summary.json" 'all("attempt_token" not in x for x in data["running"])'

run_ok "completed prerequisite checkpoints immediately" "$CONTROLLER" finish --sprint 42 --ticket PROJ-1 --outcome completed --summary merged --pr 101 --branch feature/one --attempt-token "$TOKEN1"
run_ok "blocked independent ticket frees its lane" "$CONTROLLER" finish --sprint 42 --ticket PROJ-3 --outcome blocked --summary 'test failure' --attempt-token "$TOKEN3"
"$CONTROLLER" plan --sprint 42 > "$TMP/plan2.json"
json_check "completed prerequisite unlocks dependent ticket" "$TMP/plan2.json" 'data["launch"] == ["PROJ-2"]'

"$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref "pid:999999" > "$TMP/reserve2.json" && ok "unlocked ticket reserves with dead provisional identity" || bad "unlocked ticket reserves with dead provisional identity"
TOKEN2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve2.json")"
ATTACH2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attach_capability"])' "$TMP/reserve2.json")"
run_ok "running ticket survives inventory resync" "$CONTROLLER" sync --inventory inventory.json
"$CONTROLLER" plan --sprint 42 > "$TMP/resync.json"
json_check "resync does not duplicate a running workflow" "$TMP/resync.json" 'data["needs_reconcile"] == ["PROJ-2"] and "PROJ-2" not in data["launch"]'
run_fail "requeue without stopped-worker proof fails closed" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason missing-proof --attempt-token "$TOKEN2"
run_fail "worker attempt token cannot create launch evidence" "$CONTROLLER" launch-local --sprint 42 --ticket PROJ-2 --attach-capability "$TOKEN2" --output .orchestration/worker2.log -- /bin/sh -c 'sleep 30'
"$CONTROLLER" launch-local --sprint 42 --ticket PROJ-2 --attach-capability "$ATTACH2" --output .orchestration/worker2.log -- /bin/sh -c 'sleep 30' > "$TMP/launch2.json"
LAUNCH2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["launch_evidence"])' "$TMP/launch2.json")"
PID2="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker_pid"])' "$TMP/launch2.json")"
WORKER_PIDS="$WORKER_PIDS $PID2"
run_fail "launch evidence is bound to its exact ticket and attempt" "$CONTROLLER" attach --sprint 42 --ticket PROJ-1 --launch-evidence "$LAUNCH2"
run_ok "controller attach capability establishes launched worker identity" "$CONTROLLER" attach --sprint 42 --ticket PROJ-2 --launch-evidence "$LAUNCH2"
run_fail "live attached worker blocks requeue despite dead provisional identity" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason 'worker no longer exists' --attempt-token "$TOKEN2"
kill "$PID2" 2>/dev/null || true
# PID2 is the supervisor's child, not this shell's, so `wait` cannot block on it.
# The supervisor still has to observe the exit, write its terminal record, and
# exit itself; requeue before that correctly sees a live or unknown unit.
wait_unit_absent 42 PROJ-2 && ok "killed worker's execution unit becomes absent within the bound" || fail_case "killed worker's execution unit becomes absent within the bound"
run_ok "confirmed process absence permits automatic requeue" "$CONTROLLER" requeue --sprint 42 --ticket PROJ-2 --reason 'worker exited' --attempt-token "$TOKEN2"
python3 - "$CONTROLLER_MODULE" "$TMP/repo" <<'PY' && ok "unknown unit inspection, descendant liveness, and identity reuse fail closed" || fail_case "unknown unit inspection, descendant liveness, and identity reuse fail closed"
import importlib.util,sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
spec=importlib.util.spec_from_file_location("sprint_controller", sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
cfg={"shared_root": Path(sys.argv[2])}
ticket={"worker_identity":{"kind":"execution_unit","pid":123,"containment":"cgroup-v2-systemd-scope"}}
for status in ("unknown", "live"):
    module.execution_unit_status=lambda _identity, status=status: status
    try: module.require_worker_stopped(ticket, "", cfg)
    except module.SprintError: pass
    else: raise AssertionError(f"{status} execution unit was treated as absent")
module.execution_unit_status=lambda _identity: "absent"
module.require_worker_stopped(ticket, "", cfg)
ticket["worker_identity"]["containment"]="cooperative-session"
try: module.require_worker_stopped(ticket, "", cfg)
except module.SprintError: pass
else: raise AssertionError("cooperative containment claimed mechanical absence")
ticket["worker_identity"]={"kind":"process","pid":123,"start_fingerprint":"legacy"}
try: module.require_worker_stopped(ticket, "", cfg)
except module.SprintError: pass
else: raise AssertionError("legacy leader-PID identity was treated as descendant proof")
PY
python3 - "$CONTROLLER_MODULE" <<'PY' && ok "macOS process identity uses exact proc_pidinfo birth time" || fail_case "macOS process identity uses exact proc_pidinfo birth time"
import importlib.util,sys
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
spec=importlib.util.spec_from_file_location("sprint_controller_darwin", sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
class Lib:
    def __init__(self,usec): self.usec=usec
    def proc_pidinfo(self,pid,flavor,arg,ptr,size):
        ptr._obj.pid=pid; ptr._obj.start_tvsec=100; ptr._obj.start_tvusec=self.usec
        return size
with mock.patch.object(module.sys,"platform","darwin"), mock.patch.object(module.os,"kill"):
    with mock.patch.object(module.ctypes,"CDLL",return_value=Lib(1)): first=module.process_identity("123")
    with mock.patch.object(module.ctypes,"CDLL",return_value=Lib(2)): second=module.process_identity("123")
assert first["start_identity"] == "darwin:100:1"
assert first["start_fingerprint"] != second["start_fingerprint"]
PY
run_ok "pending attempt history survives Jira resync" "$CONTROLLER" sync --inventory inventory.json
"$CONTROLLER" reserve --sprint 42 --ticket PROJ-2 --run-ref codex-task-two > "$TMP/reserve2b.json" && ok "requeued ticket can reserve again" || bad "requeued ticket can reserve again"
json_check "relaunch accounting survives pending sync" "$TMP/reserve2b.json" 'data["attempt"] == 2'
TOKEN2B="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve2b.json")"
run_fail "superseded attempt cannot finish replacement" "$CONTROLLER" finish --sprint 42 --ticket PROJ-2 --outcome blocked --summary stale --attempt-token "$TOKEN2"
run_ok "recovered ticket completes" "$CONTROLLER" finish --sprint 42 --ticket PROJ-2 --outcome completed --summary merged --pr 102 --branch feature/two --attempt-token "$TOKEN2B"

"$CONTROLLER" summary --sprint 42 > "$TMP/summary.json"
json_check "summary separates completed, blocked, and user action" "$TMP/summary.json" '([x["key"] for x in data["completed"]] == ["PROJ-1", "PROJ-2"] and [x["key"] for x in data["user_action"]] == ["PROJ-8"] and set(x["key"] for x in data["blocked"]) == {"PROJ-3", "PROJ-4", "PROJ-5", "PROJ-6", "PROJ-7"})'
json_check "summary finishes after autonomous work is exhausted" "$TMP/summary.json" 'data["finished"] is True and data["running"] == []'

cat > "$TMP/repo/priority.json" <<'JSON'
{
  "project": "PROJ",
  "sprint": {"id": "43", "name": "Sprint 43"},
  "source_query": "project = PROJ AND sprint = 43",
  "subtask_source_query": "parent in sprint tickets",
  "subtask_keys": [],
  "tickets": [
    {"key": "PROJ-20", "summary": "medium", "status": "Ready", "priority": 3, "dependencies": [], "subtasks": []},
    {"key": "PROJ-21", "summary": "unranked", "status": "Ready", "dependencies": [], "subtasks": []},
    {"key": "PROJ-22", "summary": "urgent late key", "status": "Ready", "priority": 1, "dependencies": [], "subtasks": []},
    {"key": "PROJ-23", "summary": "urgent tie", "status": "Ready", "priority": "1", "dependencies": [], "subtasks": []},
    {"key": "PROJ-24", "summary": "urgent but dependent", "status": "Ready", "priority": 1, "dependencies": ["PROJ-20"], "subtasks": []},
    {"key": "PROJ-25", "summary": "low and dependent", "status": "Ready", "priority": 5, "dependencies": ["PROJ-20"], "subtasks": []}
  ]
}
JSON
jira_receipt "$TMP/repo/priority.json"

run_ok "sync accepts optional per-ticket priority" "$CONTROLLER" sync --inventory priority.json
"$CONTROLLER" plan --sprint 43 > "$TMP/priority-plan.json"
json_check "highest priority fills lanes first, ties broken by key" "$TMP/priority-plan.json" 'data["launch"] == ["PROJ-22", "PROJ-23"]'
json_check "unprioritized tickets sort after every ranked ticket" "$TMP/priority-plan.json" '[x["key"] for x in data["waiting"]] == ["PROJ-24", "PROJ-25"]'

"$CONTROLLER" reserve --sprint 43 --ticket PROJ-22 --run-ref p-one > "$TMP/reserve22.json" && ok "priority lane one reserves" || bad "priority lane one reserves"
run_ok "priority lane two reserves" "$CONTROLLER" reserve --sprint 43 --ticket PROJ-23 --run-ref p-two
TOKEN22="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/reserve22.json")"
run_fail "priority board still refuses a third lane" "$CONTROLLER" reserve --sprint 43 --ticket PROJ-21 --run-ref p-jump
run_ok "priority lane one finishes" "$CONTROLLER" finish --sprint 43 --ticket PROJ-22 --outcome completed --summary merged --pr 201 --branch feature/p-one --attempt-token "$TOKEN22"
"$CONTROLLER" plan --sprint 43 > "$TMP/priority-plan2.json"
json_check "next lane goes to the ranked ticket, not the unranked one" "$TMP/priority-plan2.json" 'data["launch"] == ["PROJ-20"]'

run_ok "priority survives inventory resync" "$CONTROLLER" sync --inventory priority.json
"$CONTROLLER" summary --sprint 43 > "$TMP/priority-summary.json"
json_check "summary reports each ticket priority" "$TMP/priority-summary.json" '{x["key"]: x["priority"] for x in data["completed"] + data["blocked"] + data["user_action"] + data["running"]} == {"PROJ-20": 3, "PROJ-21": None, "PROJ-22": 1, "PROJ-23": 1, "PROJ-24": 1, "PROJ-25": 5}'

# A checkpoint written before priority existed must keep planning, not crash.
python3 - "$TMP/repo/.orchestration/.sprint-state" <<'PY'
import json, sys
from pathlib import Path
for path in Path(sys.argv[1]).glob("*.json"):
    state = json.loads(path.read_text())
    if not isinstance(state.get("tickets"), dict):
        continue
    for ticket in state["tickets"].values():
        ticket.pop("priority", None)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
PY
run_ok "pre-priority checkpoints still plan" "$CONTROLLER" plan --sprint 43
run_ok "pre-priority checkpoints still summarize" "$CONTROLLER" summary --sprint 42

cat > "$TMP/repo/bad-priority.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"44","name":"bad"},"source_query":"q","tickets":[{"key":"PROJ-30","status":"Ready","priority":"urgent"}]}
JSON
run_fail "non-integer priority fails closed" "$CONTROLLER" sync --inventory bad-priority.json

cat > "$TMP/repo/duplicate.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"99","name":"bad"},"tickets":[{"key":"PROJ-1","status":"Ready"},{"key":"proj-1","status":"Ready"}]}
JSON
run_fail "duplicate normalized Jira keys fail closed" "$CONTROLLER" sync --inventory duplicate.json
cat > "$TMP/repo/missing-subtask.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"100","name":"bad child inventory"},"source_query":"q","subtask_source_query":"children","subtask_keys":["PROJ-2"],"tickets":[{"key":"PROJ-1","status":"Ready","subtasks":["PROJ-2"]}]}
JSON
run_fail "missing referenced Jira subtasks fail closed" "$CONTROLLER" sync --inventory missing-subtask.json
cat > "$TMP/repo/unproven-empty-subtasks.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"101","name":"unproven children"},"source_query":"q","tickets":[{"key":"PROJ-1","status":"Ready","subtasks":[]}]}
JSON
run_fail "empty subtasks without an independent child query fail closed" "$CONTROLLER" sync --inventory unproven-empty-subtasks.json
run_fail "checkpoint directory cannot escape the repository" "$CONTROLLER" --state-dir ../outside sync --inventory inventory.json

cat > "$TMP/repo/relations.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"102","name":"relations"},"source_query":"sprint = 102","subtask_source_query":"parent in (PROJ-80)","subtask_keys":["PROJ-81"],"tickets":[{"key":"PROJ-80","status":"Ready","dependencies":[],"subtasks":["PROJ-81"]},{"key":"PROJ-81","parent":"PROJ-80","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/relations.json"
run_ok "adapter evidence binds both directions of Jira child relations" "$CONTROLLER" sync --inventory relations.json
python3 - relations.json "$TMP/repo/bad-relations.json" "$TMP/repo/bad-relations.fetch.json" <<'PY'
import json,sys
value=json.load(open(sys.argv[1])); artifact=json.load(open(value["fetch_artifact"]["path"]))
artifact["child_parents"]["PROJ-81"]="PROJ-999"
json.dump(artifact,open(sys.argv[3],"w")); value["fetch_artifact"]["path"]=sys.argv[3]
json.dump(value,open(sys.argv[2],"w"))
PY
run_fail "one-way or contradictory Jira relations are rejected" "$CONTROLLER" sync --inventory "$TMP/repo/bad-relations.json"

cat > "$TMP/repo/legacy-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"47","name":"legacy running"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-60","status":"Ready","dependencies":[],"subtasks":[]},{"key":"PROJ-61","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/legacy-inventory.json"
run_ok "legacy migration fixture syncs" "$CONTROLLER" sync --inventory legacy-inventory.json
"$CONTROLLER" reserve --sprint 47 --ticket PROJ-60 --run-ref pid:999999 > /dev/null
python3 - "$TMP/repo/.orchestration/.sprint-state" <<'PY'
import json, sys
from pathlib import Path
path = next(Path(sys.argv[1]).glob('47-*.json'))
state = json.loads(path.read_text())
state['schema_version'] = 1
state['tickets']['PROJ-60'].pop('attempt_token', None)
path.write_text(json.dumps(state) + '\n')
PY
"$CONTROLLER" summary --sprint 47 > "$TMP/legacy-summary.json"
json_check "schema-v1 running lanes fence to explicit recovery" "$TMP/legacy-summary.json" 'data["user_action"][0]["key"] == "PROJ-60" and "legacy running lane" in data["user_action"][0]["reason"]'
printf 'recover-legacy-once' > "$ORCHESTRATION_TEST_RECOVERY_CAP"
run_ok "fenced legacy lane has an explicit recovery path" "$CONTROLLER" recover-legacy --sprint 47 --ticket PROJ-60 --reason 'operator verified old worker stopped' --operator-capability recover-legacy-once
"$CONTROLLER" plan --sprint 47 > "$TMP/legacy-plan.json"
json_check "recovered legacy lane becomes launchable without duplication" "$TMP/legacy-plan.json" '"PROJ-60" in data["launch"]'
run_fail "legacy recovery capability is one-shot" "$CONTROLLER" recover-legacy --sprint 47 --ticket PROJ-60 --reason replay

"$CONTROLLER" reserve --sprint 47 --ticket PROJ-61 --run-ref terminal > "$TMP/terminal-reserve.json"
TERMINAL_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/terminal-reserve.json")"
"$CONTROLLER" finish --sprint 47 --ticket PROJ-61 --outcome user_action --summary 'operator budget stop' --attempt-token "$TERMINAL_TOKEN"
python3 - "$TMP/repo/.orchestration/.sprint-state" "$TMP/repo/.orchestration/.llm-usage/usage.jsonl" <<'PY'
import json,sys
from pathlib import Path
state_path=next(Path(sys.argv[1]).glob('47-*.json'))
state=json.loads(state_path.read_text()); ticket=state['tickets']['PROJ-61']
ticket['attempt_token']=None; ticket['worker_identity']=None
state_path.write_text(json.dumps(state)+'\n')
usage=Path(sys.argv[2]); usage.parent.mkdir(parents=True,exist_ok=True)
with usage.open('a') as out:
  out.write(json.dumps({'kind':'usage','reservation_id':'spent-61','run_id':'spent-61','ticket':'PROJ-61','sprint':'47','role':'implementer','cost_usd':'20.10'})+'\n')
  out.write(json.dumps({'kind':'ticket_budget_pause','ticket':'PROJ-61','run_id':'spent-61','projected_total_usd':'20.10'})+'\n')
PY
printf 'budget-once' > "$ORCHESTRATION_TEST_BUDGET_CAP"
if printf 'budget-once\n' | "$CONTROLLER" grant-budget --sprint 47 --ticket PROJ-61 --operator-capability-stdin >/dev/null; then
  ok "root-issued budget capability activates an absolute ticket ceiling over stdin"
else
  fail_case "root-issued budget capability activates an absolute ticket ceiling over stdin"
fi
printf 'terminal-recovery-once' > "$ORCHESTRATION_TEST_RECOVERY_CAP"
if printf 'terminal-recovery-once\n' | "$CONTROLLER" recover-terminal --sprint 47 --ticket PROJ-61 --reason 'provider absence and stopped worker verified' --operator-capability-stdin >/dev/null; then
  ok "external authority recovers a terminal lane with no retained attempt token over stdin"
else
  fail_case "external authority recovers a terminal lane with no retained attempt token over stdin"
fi
"$CONTROLLER" plan --sprint 47 > "$TMP/terminal-plan.json"
json_check "budget grant does not erase a stalled-progress hold" "$TMP/terminal-plan.json" '"PROJ-61" not in data["launch"] and data["spend"]["PROJ-61"]["state"] != "operator_action" and any("max_usd_without_progress" in reason for item in data["waiting"] if item["key"] == "PROJ-61" for reason in item["reasons"])'
run_fail "terminal recovery capability is one-shot" "$CONTROLLER" recover-terminal --sprint 47 --ticket PROJ-61 --reason replay --operator-capability terminal-recovery-once

python3 - "$TMP/repo/.orchestration/.sprint-state" <<'PY'
import json,sys
from pathlib import Path
path=next(Path(sys.argv[1]).glob('47-*.json'))
state=json.loads(path.read_text()); state['tickets']['PROJ-61']['attempts']=3
# Fixture a previously verified milestone so this section isolates launch ceilings.
state['tickets']['PROJ-61']['progress']=[{'milestone':'implementation_commit','verified':True,'spent_usd':20.10}]
path.write_text(json.dumps(state)+'\n')
PY
"$CONTROLLER" plan --sprint 47 > "$TMP/relaunch-before.json"
json_check "an exhausted pending ticket is not launchable without root authority" "$TMP/relaunch-before.json" '"PROJ-61" not in data["launch"] and any(x["key"] == "PROJ-61" and "attempt ceiling" in "; ".join(x["reasons"]) for x in data["waiting"])'
"$CONTROLLER" summary --sprint 47 > "$TMP/relaunch-summary.json"
json_check "summary reports exhausted attempts as an operator action" "$TMP/relaunch-summary.json" 'any(x["key"] == "PROJ-61" and "root-issued ticket relaunch authority" in x["reason"] for x in data["user_action"])'
printf 'relaunch-once' > "$ORCHESTRATION_TEST_RELAUNCH_CAP"
if printf 'relaunch-once\n' | "$CONTROLLER" grant-relaunch --sprint 47 --ticket PROJ-61 --operator-capability-stdin > "$TMP/relaunch-grant.json"; then
  ok "root-issued relaunch capability activates an absolute ticket attempt ceiling"
else
  fail_case "root-issued relaunch capability activates an absolute ticket attempt ceiling"
fi
json_check "relaunch grant reports its exact absolute ceiling" "$TMP/relaunch-grant.json" 'data["attempt_ceiling"] == 4'
"$CONTROLLER" plan --sprint 47 > "$TMP/relaunch-after.json"
json_check "ticket-scoped relaunch authority restores only the exhausted ticket" "$TMP/relaunch-after.json" '"PROJ-61" in data["launch"]'
"$CONTROLLER" reserve --sprint 47 --ticket PROJ-61 --run-ref authorized-fourth > "$TMP/relaunch-reserve.json"
json_check "the authorized final attempt reserves normally" "$TMP/relaunch-reserve.json" 'data["attempt"] == 4'
run_fail "relaunch capability is one-shot" "$CONTROLLER" grant-relaunch --sprint 47 --ticket PROJ-61 --operator-capability relaunch-once

python3 - "$TMP/repo/.orchestration/.llm-usage/usage.jsonl" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
with p.open('a') as out:
  out.write(json.dumps({'kind':'usage','reservation_id':'legacy-pr','run_id':'legacy-pr','ticket':'1802','cost_usd':'0.01'})+'\n')
  out.write(json.dumps({'kind':'usage','reservation_id':'legacy-smoke','run_id':'legacy-smoke','ticket':'SMOKE-TEST','cost_usd':'0.01'})+'\n')
PY
"$CONTROLLER" summary --sprint 47 > "$TMP/legacy-label-summary.json"
json_check "legacy non-ticket accounting labels remain reportable without authority lookup" "$TMP/legacy-label-summary.json" 'data["spend"]["1802"]["spent_usd"] == 0.01 and data["spend"]["SMOKE-TEST"]["spent_usd"] == 0.01'

cat > "$TMP/repo/fast-exit.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"49","name":"fast exit"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-90","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/fast-exit.json"
run_ok "fast-exit inventory syncs" "$CONTROLLER" sync --inventory fast-exit.json
printf 'prompt-from-stdin\n' > "$TMP/repo/.orchestration/fast.prompt"
"$CONTROLLER" reserve --sprint 49 --ticket PROJ-90 --run-ref fast > "$TMP/fast-reserve.json"
FAST_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attempt_token"])' "$TMP/fast-reserve.json")"
FAST_ATTACH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["attach_capability"])' "$TMP/fast-reserve.json")"
"$CONTROLLER" launch-local --sprint 49 --ticket PROJ-90 --attach-capability "$FAST_ATTACH" --output .orchestration/fast.log --stdin-file .orchestration/fast.prompt -- /bin/sh -c 'IFS= read -r prompt; printf "%s\n" "$prompt"' > "$TMP/fast-launch.json"
FAST_EVIDENCE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["launch_evidence"])' "$TMP/fast-launch.json")"
if [ "$(cat "$TMP/repo/.orchestration/fast.log")" = prompt-from-stdin ]; then ok "launch-local sends prompt file contents to worker stdin"; else fail_case "launch-local sends prompt file contents to worker stdin"; fi
run_ok "fast worker terminal tombstone remains attachable" "$CONTROLLER" attach --sprint 49 --ticket PROJ-90 --launch-evidence "$FAST_EVIDENCE"
run_ok "fast worker tombstone permits confirmed recovery" "$CONTROLLER" requeue --sprint 49 --ticket PROJ-90 --reason 'fast worker exited' --attempt-token "$FAST_TOKEN"

cat > "$TMP/repo/limit-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"48","name":"run limit"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-70","status":"Ready","dependencies":[],"subtasks":[]},{"key":"PROJ-71","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/limit-inventory.json"
run_ok "run-limit inventory syncs" "$CONTROLLER" sync --inventory limit-inventory.json
python3 - "$TMP/repo/.orchestration/.llm-usage/usage.jsonl" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True)
with p.open('a') as f:
  for i in range(12):
    f.write(json.dumps({"kind":"reservation","reservation_id":f"limit-{i}","run_id":f"run-{i}","ticket":"PROJ-70","sprint":"48","role":"sprint-worker","projected_cost_usd":"0.001"})+'\n')
  for i in range(6):
    f.write(json.dumps({"kind":"reservation","reservation_id":f"review-limit-{i}","run_id":f"review-run-{i}","ticket":"PROJ-71","sprint":"48","role":"code-reviewer","projected_cost_usd":"0.001"})+'\n')
PY
"$CONTROLLER" plan --sprint 48 > "$TMP/limit-plan.json"
json_check "model and reviewer run breakers remove doomed replacements" "$TMP/limit-plan.json" 'data["launch"] == [] and all(data["spend"][key]["state"] == "operator_action" for key in ("PROJ-70","PROJ-71")) and data["spend"]["PROJ-71"]["reviewer_run_count"] == 6'
"$CONTROLLER" summary --sprint 48 > "$TMP/limit-summary.json"
json_check "run-limited-only sprint is terminal for captain" "$TMP/limit-summary.json" 'data["finished"] is True and [x["key"] for x in data["user_action"]] == ["PROJ-70","PROJ-71"]'

sed -i.bak 's/auto_decompose_large_tickets: false/auto_decompose_large_tickets: true/' "$TMP/repo/.orchestration/config.yaml"
rm "$TMP/repo/.orchestration/config.yaml.bak"
cat > "$TMP/repo/design-limit-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"50","name":"phase counts"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-72","description":"two independently releasable boundaries","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/design-limit-inventory.json"
run_ok "phase-count inventory syncs" "$CONTROLLER" sync --inventory design-limit-inventory.json
python3 - "$TMP/repo/.orchestration/.llm-usage/usage.jsonl" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
with p.open('a') as f:
  for i in range(6):
    f.write(json.dumps({"kind":"reservation","reservation_id":f"design-{i}","run_id":f"design-{i}","ticket":"PROJ-72","sprint":"50","role":"design-reviewer","projected_cost_usd":"0.001"})+'\n')
  f.write(json.dumps({"kind":"ticket_budget_pause","ticket":"PROJ-72","run_id":"legacy-review-stop","reason":"max_reviewer_runs_per_ticket"})+'\n')
PY
"$CONTROLLER" plan --sprint 50 > "$TMP/design-limit-plan.json"
json_check "design attempts preserve code and security review capacity" "$TMP/design-limit-plan.json" 'data["scope"] == ["PROJ-72"] and data["spend"]["PROJ-72"]["design_review_run_count"] == 6 and data["spend"]["PROJ-72"]["reviewer_run_count"] == 0 and data["spend"]["PROJ-72"]["state"] == "ok"'

"$CONTROLLER" plan --sprint 50 > "$TMP/scope-plan.json"
json_check "opted-in tickets enter autonomous scope before launch" "$TMP/scope-plan.json" 'data["scope"] == ["PROJ-72"] and data["launch"] == [] and data["autonomous_work_remaining"] is True'
"$CONTROLLER" scope-context --sprint 50 --ticket PROJ-72 > "$TMP/scope-context.json"
json_check "scope context exposes only the requested sanitized Jira body" "$TMP/scope-context.json" 'data["ticket"] == "PROJ-72" and data["description"] == "two independently releasable boundaries"'
cat > "$TMP/repo/.orchestration/proj-72-scope.json" <<'JSON'
{"schema_version":1,"ticket":"PROJ-72","verdict":"decompose","complexity_score":88,"reasons":["crosses two independently releasable boundaries"],"slices":[{"id":"foundation","summary":"Foundation","behavior":"add the independent foundation","acceptance_criteria":["foundation test passes"],"migration_owner":"none","test_plan":["foundation regression"],"depends_on":[]},{"id":"cutover","summary":"Cutover","behavior":"activate the new foundation","acceptance_criteria":["cutover test passes"],"migration_owner":"none","test_plan":["cutover regression"],"depends_on":["foundation"]}]}
JSON
run_ok "structured scope result enters decomposition queue" "$CONTROLLER" record-scope --sprint 50 --ticket PROJ-72 --assessment .orchestration/proj-72-scope.json
"$CONTROLLER" plan --sprint 50 > "$TMP/decomposition-plan.json"
json_check "decomposition remains autonomous work" "$TMP/decomposition-plan.json" 'data["decomposition"] == ["PROJ-72"] and data["autonomous_work_remaining"] is True'
cat > "$TMP/repo/design-limit-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"50","name":"phase counts"},"source_query":"q","subtask_source_query":"children","subtask_keys":["PROJ-73","PROJ-74"],"tickets":[{"key":"PROJ-72","status":"Ready","dependencies":[],"subtasks":["PROJ-73","PROJ-74"]},{"key":"PROJ-73","parent":"PROJ-72","summary":"Foundation","issue_type":"Sub-task","is_subtask":true,"labels":["orchestration-slice-proj-72-foundation","orka-slice-v1-3f0d17c45e5d705e1bc6639b2d2c12d61a1451193fdb0b8fdd76cffe200d71ef"],"status":"Ready","dependencies":[],"subtasks":[]},{"key":"PROJ-74","parent":"PROJ-72","summary":"Cutover","issue_type":"Sub-task","is_subtask":true,"labels":["orchestration-slice-proj-72-cutover","orka-slice-v1-8dd5ad4a5a6d04ed3e973eba104fa9f9c8027e1ca687695a0f67b62036d9df04"],"status":"Ready","dependencies":["PROJ-73"],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/design-limit-inventory.json"
run_ok "fresh Jira sync proves decomposition children" "$CONTROLLER" sync --inventory design-limit-inventory.json
run_ok "tracking parent binds exact synchronized children" "$CONTROLLER" record-decomposition --sprint 50 --ticket PROJ-72 --children PROJ-73,PROJ-74
"$CONTROLLER" plan --sprint 50 > "$TMP/child-scope-plan.json"
json_check "new children enter scope in dependency order" "$TMP/child-scope-plan.json" 'data["scope"] == ["PROJ-73"] and data["decomposition"] == []'
"$CONTROLLER" summary --sprint 50 > "$TMP/decomposed-summary.json"
json_check "summary separates decomposed tracking parents" "$TMP/decomposed-summary.json" '[x["key"] for x in data["decomposed"]] == ["PROJ-72"]'
sed -i.bak 's/auto_decompose_large_tickets: true/auto_decompose_large_tickets: false/' "$TMP/repo/.orchestration/config.yaml"
rm "$TMP/repo/.orchestration/config.yaml.bak"

cat > "$TMP/repo/batch-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"45","name":"batch"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-40","summary":"batch one","status":"Ready","dependencies":[],"subtasks":[]},{"key":"PROJ-41","summary":"batch two","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/batch-inventory.json"
cat > "$TMP/repo/batch-jobs.json" <<'JSON'
{"jobs":[{"ticket":"PROJ-40","background":true,"interactive":false,"params":{"model":"claude-sonnet-5","max_tokens":100,"system":[{"type":"text","text":"cached","cache_control":{"type":"ephemeral"}}],"messages":[{"role":"user","content":"ticket 40"}]}},{"ticket":"PROJ-41","background":true,"interactive":false,"params":{"model":"claude-sonnet-5","max_tokens":100,"messages":[{"role":"user","content":"ticket 41"}]}}]}
JSON
run_ok "batch sprint inventory syncs" "$CONTROLLER" sync --inventory batch-inventory.json
"$CONTROLLER" prepare-batch --sprint 45 --jobs batch-jobs.json > "$TMP/batch-result.json"
json_check "non-interactive background lanes serialize as one Message Batch" "$TMP/batch-result.json" 'data["status"] == "pending_submission" and data["tickets"] == ["PROJ-40", "PROJ-41"]'
python3 - "$TMP/batch-result.json" <<'PY'
import json, sys
result=json.load(open(sys.argv[1]))
request=json.load(open(result["request"]))
marker=json.load(open(result["marker"]))
assert len(request["requests"]) == 2
assert all(set(item) == {"custom_id", "params"} for item in request["requests"])
assert marker["endpoint"] == "/v1/messages/batches"
assert marker["status"] == "pending_submission"
PY
if [ "$?" -eq 0 ]; then ok "batch request and durable state marker match Anthropic shape"; else fail_case "batch request and durable state marker match Anthropic shape"; fi
"$CONTROLLER" plan --sprint 45 > "$TMP/batch-plan.json"
json_check "serialized batch jobs atomically reserve their sprint lanes" "$TMP/batch-plan.json" 'data["launch"] == [] and data["running"] == ["PROJ-40", "PROJ-41"]'
BATCH_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["batch_id"])' "$TMP/batch-result.json")"
run_fail "caller failure cannot release uncertain submitted work" "$CONTROLLER" reconcile-batch --batch "$BATCH_ID" --outcome failed
cat > "$TMP/batch-terminal.json" <<JSON
{"schema_version":1,"provider":"anthropic","batch_id":"$BATCH_ID","provider_batch_id":"msgbatch_test","status":"cancelled","job_ids":["ticket_PROJ_40_$BATCH_ID","ticket_PROJ_41_$BATCH_ID"]}
JSON
run_fail "hand-authored terminal JSON cannot transition an uncertain batch" "$CONTROLLER" reconcile-batch --batch "$BATCH_ID" --outcome failed --provider-evidence "$TMP/batch-terminal.json"
cat > "$TMP/batch-transport.json" <<'JSON'
{"submit":{"id":"msgbatch_test","type":"message_batch","processing_status":"in_progress"},"status":{"id":"msgbatch_test","processing_status":"cancelled"},"result_pages":[]}
JSON
run_fail "production CLI has no synthetic batch transport authority" "$CONTROLLER" submit-batch --batch "$BATCH_ID" --test-transport "$TMP/batch-transport.json"

cat > "$TMP/repo/interactive-job.json" <<'JSON'
{"jobs":[{"ticket":"PROJ-40","background":true,"interactive":true,"params":{"model":"claude-sonnet-5","max_tokens":10,"messages":[{"role":"user","content":"x"}]}}]}
JSON
run_fail "interactive work is rejected from asynchronous batching" "$CONTROLLER" prepare-batch --sprint 45 --jobs interactive-job.json

cat > "$TMP/repo/openai-inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"46","name":"openai batch"},"source_query":"q","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-50","summary":"openai lane","status":"Ready","dependencies":[],"subtasks":[]}]}
JSON
jira_receipt "$TMP/repo/openai-inventory.json"
cat > "$TMP/repo/openai-jobs.json" <<'JSON'
{"provider":"openai","jobs":[{"ticket":"PROJ-50","background":true,"interactive":false,"params":{"model":"gpt-5.6-sol","max_output_tokens":100,"input":[{"role":"developer","content":"stable"},{"role":"user","content":"ticket 50"}]}}]}
JSON
run_ok "OpenAI batch sprint inventory syncs" "$CONTROLLER" sync --inventory openai-inventory.json
"$CONTROLLER" prepare-batch --sprint 46 --jobs openai-jobs.json > "$TMP/openai-batch-result.json"
python3 - "$TMP/openai-batch-result.json" <<'PY'
import json, sys
result=json.load(open(sys.argv[1]))
assert result["provider"] == "openai" and result["status"] == "pending_upload"
line=json.loads(open(result["request"]).readline())
marker=json.load(open(result["marker"]))
assert set(line) == {"custom_id", "method", "url", "body"}
assert line["method"] == "POST" and line["url"] == "/v1/responses"
assert marker["endpoint"] == "/v1/batches" and marker["provider"] == "openai"
PY
if [ "$?" -eq 0 ]; then ok "OpenAI background lanes serialize to Batch JSONL"; else fail_case "OpenAI background lanes serialize to Batch JSONL"; fi
OPENAI_BATCH_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["batch_id"])' "$TMP/openai-batch-result.json")"
run_fail "OpenAI production CLI also rejects synthetic transport" "$CONTROLLER" submit-batch --batch "$OPENAI_BATCH_ID" --test-transport "$TMP/openai-nonterminal.json"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
