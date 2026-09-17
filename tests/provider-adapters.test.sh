#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok() { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

if python3 -m unittest \
  "$ROOT/tests/github_progress_test.py" \
  "$ROOT/tests/test_progress_test.py" \
  "$ROOT/tests/sprint_metrics_test.py" \
  "$ROOT/tests/sprint_completion_scenario_test.py" \
  "$ROOT/tests/phase_budget_test.py" \
  "$ROOT/tests/native_gateway_test.py" \
  "$ROOT/tests/codex_gateway_test.py" \
  "$ROOT/tests/sprint_controller_resilience_test.py" \
  "$ROOT/tests/jira_decomposition_test.py" \
  "$ROOT/tests/provider_batch_adapter_test.py" \
  "$ROOT/tests/sprint_controller_batch_test.py"; then
  ok "provider-native batch normalization unit matrix"
else
  bad "provider-native batch normalization unit matrix"
fi
if python3 "$ROOT/tests/jira_inventory_fetch_test.py" >/dev/null 2>&1; then
  ok "Jira adapter credential, identity, and pagination unit matrix"
else
  bad "Jira adapter credential, identity, and pagination unit matrix"
fi

cat > "$TMP/inventory.json" <<'JSON'
{"project":"PROJ","sprint":{"id":"1","name":"one"},"source_query":"parents","subtask_source_query":"children","subtask_keys":[],"tickets":[{"key":"PROJ-1","status":"Ready","subtasks":[]},{"key":"PROJ-2","status":"Ready","subtasks":[]}]}
JSON
cat > "$TMP/config.yaml" <<'YAML'
ticket:
  kind: jira
  project: PROJ
sprint_id: 1
jira_base_url: https://jira.example
jira_sprint_field: sprint
YAML
cat > "$TMP/jira-transport.json" <<'JSON'
{"parents":[{"isLast":false,"nextPageToken":"page-2","issues":[{"key":"PROJ-1","fields":{"summary":"one","status":{"name":"Ready"},"priority":null,"sprint":{"id":"1","name":"one"},"subtasks":[],"issuelinks":[]}}]},{"isLast":true,"issues":[{"key":"PROJ-2","fields":{"summary":"two","status":{"name":"Ready"},"priority":null,"sprint":{"id":"1","name":"one"},"subtasks":[],"issuelinks":[]}}]}],"children":[{"isLast":true,"issues":[]}]}
JSON
if python3 "$ROOT/tests/jira_fixture_driver.py" "$TMP/jira-transport.json" "$TMP/config.yaml" --inventory-template "$TMP/inventory.json" --artifact "$TMP/jira.json" --output "$TMP/output.json" \
  && python3 - "$TMP/jira.json" <<'PY'
import json, pathlib, sys
value=json.load(open(sys.argv[1]))
assert value["authority"] == "test-only"
assert [page["start_at"] for page in value["queries"][0]["pages"]] == [0, 1]
assert value["queries"][0]["pages"][1]["cursor_in"] == "page-2"
assert all(pathlib.Path(page["raw_path"]).name == "sha256-" + page["raw_sha256"] + ".json" for query in value["queries"] for page in query["pages"])
PY
then ok "Jira adapter exhausts pagination into content-addressed raw evidence"; else bad "Jira adapter exhausts pagination into content-addressed raw evidence"; fi

sed 's#https://jira.example#http://jira.example#' "$TMP/config.yaml" > "$TMP/http-config.yaml"
mkdir -p "$TMP/.orchestration"
cp "$TMP/http-config.yaml" "$TMP/.orchestration/config.yaml"
if (cd "$TMP" && JIRA_API_TOKEN=test python3 "$ROOT/scripts/jira_inventory_fetch.py" --inventory-template "$TMP/inventory.json" --artifact "$TMP/no.json" --output "$TMP/no-out.json") >/dev/null 2>&1; then
  bad "Jira adapter rejects a non-HTTPS origin"
else ok "Jira adapter rejects a non-HTTPS origin"; fi

exit "$fails"
