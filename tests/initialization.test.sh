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
JIRA_REPO="$(mktemp -d)"
trap 'rm -rf "$PREFLIGHT_REPO" "$INCOMPLETE_PLUGIN" "$UNSUPPORTED_ROUTE_REPO" "$FAKE_BIN" "$JIRA_REPO"' EXIT
mkdir -p "$PREFLIGHT_REPO/.orchestration"
cp "$ROOT/templates/config.yaml" "$PREFLIGHT_REPO/.orchestration/config.yaml"
mkdir -p "$JIRA_REPO/.orchestration"
python3 - "$ROOT/templates/config.yaml" "$JIRA_REPO/.orchestration/config.yaml" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
for old, new in (
    ("  kind: none ", "  kind: jira "),
    ('  project: ""', "  project: PROJ"),
    ('jira_base_url: ""', "jira_base_url: https://jira.example"),
    ("    max_usd_per_run: 1.00", "    max_usd_per_run: 200"),
):
    assert old in source, old
    source = source.replace(old, new, 1)
Path(sys.argv[2]).write_text(source)
PY
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
check "template exposes an optional minimum Orka release" \
  grep -Eq '^minimum_orka_version:[[:space:]]*""([[:space:]]|$)' "$ROOT/templates/config.yaml"
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
check "template exposes opt-in source-branch security triggers" \
  grep -Eq '^security_required_source_branches:[[:space:]]*\[\]([[:space:]]|$)' "$ROOT/templates/config.yaml"
check "template exposes opt-in target-branch security triggers" \
  grep -Eq '^security_required_target_branches:[[:space:]]*\[\]([[:space:]]|$)' "$ROOT/templates/config.yaml"
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
check_minimum_version_rejected() {
  cp "$PREFLIGHT_REPO/.orchestration/config.yaml" "$PREFLIGHT_REPO/minimum.yaml"
  python3 - "$PREFLIGHT_REPO/.orchestration/config.yaml" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
path.write_text(path.read_text().replace('minimum_orka_version: ""', 'minimum_orka_version: 99.0.0'))
PY
  ! python3 "$ROOT/scripts/captain-preflight.py" --plugin-root "$ROOT" \
    --repo "$PREFLIGHT_REPO" --host codex > "$PREFLIGHT_REPO/minimum-preflight.json"
  python3 - "$PREFLIGHT_REPO/minimum-preflight.json" <<'CHECK'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['status']=='blocked' and v['installation_status']=='incompatible'
assert v['minimum_orka_version']=='99.0.0'
CHECK
  mv "$PREFLIGHT_REPO/minimum.yaml" "$PREFLIGHT_REPO/.orchestration/config.yaml"
}
check "captain rejects a runtime below the repository minimum" check_minimum_version_rejected
check_minimum_version_parser() {
  python3 - "$ROOT/scripts/captain-preflight.py" <<'PY'
import importlib.util,sys
spec=importlib.util.spec_from_file_location("captain_preflight", sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
assert module.release_version("1.5.2") == (1, 5, 2)
assert module.release_version("1.5.2+codex.20260914") == (1, 5, 2)
assert module.release_version("1.6.0") > module.release_version("1.5.2")
try:
    module.release_version("1.5")
except ValueError:
    pass
else:
    raise AssertionError("malformed release was accepted")
PY
}
check "minimum version comparison accepts equal, newer, and cachebuster releases" \
  check_minimum_version_parser
check_runtime_verified_subscription() {
  env -u JIRA_EMAIL JIRA_API_TOKEN=test-only-token PATH="$FAKE_BIN:$PATH" \
    python3 "$ROOT/scripts/captain-preflight.py" \
    --plugin-root "$ROOT" --repo "$JIRA_REPO" --host claude \
    --verify-runtime --skip-jira-auth-check > "$JIRA_REPO/subscription-preflight.json"
  python3 - "$JIRA_REPO/subscription-preflight.json" <<'CHECK'
import json,sys
raw=open(sys.argv[1]).read()
v=json.loads(raw)
assert v['status']=='ready' and v['execution_ready']
assert all(x['state']=='healthy' and x['mode']=='subscription' for x in v['routes'])
assert v['jira']['state']=='ready' and v['jira']['live_check']=='skipped'
assert v['skipped_checks']==['jira-auth']
assert 'test-only-token' not in raw
CHECK
}
check "verified preflight accepts model-less desktop subscription routes" \
  check_runtime_verified_subscription
check_preflight_blocks_missing_jira_credentials() {
  ! env -u JIRA_API_TOKEN -u JIRA_EMAIL PATH="$FAKE_BIN:$PATH" \
    python3 "$ROOT/scripts/captain-preflight.py" \
    --plugin-root "$ROOT" --repo "$JIRA_REPO" --host claude \
    --verify-runtime > "$JIRA_REPO/missing-jira-preflight.json"
  python3 - "$JIRA_REPO/missing-jira-preflight.json" <<'CHECK'
import json,sys
v=json.load(open(sys.argv[1]))
assert v['status']=='blocked' and not v['execution_ready']
assert all(x['state']=='healthy' for x in v['routes'])
assert v['jira']['state']=='blocked'
assert 'JIRA_API_TOKEN is required' in v['jira']['reason']
assert '.orchestration/.env' in v['jira']['reason']
CHECK
}
check "captain preflight blocks execution when Jira credentials are missing" \
  check_preflight_blocks_missing_jira_credentials
check_preflight_verifies_jira_credentials() {
  python3 - "$ROOT/scripts/captain-preflight.py" "$JIRA_REPO/.orchestration/config.yaml" <<'PY'
import importlib.util, json, shutil, sys, tempfile
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
spec = importlib.util.spec_from_file_location("captain_preflight", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
repo = Path(tempfile.mkdtemp())
try:
    (repo / ".orchestration").mkdir()
    config = repo / ".orchestration/config.yaml"
    shutil.copy(sys.argv[2], config)
    env = repo / ".orchestration/.env"
    env.write_text("JIRA_API_TOKEN=file-secret-token\nJIRA_EMAIL=captain@example.com\n")
    env.chmod(0o600)

    class Response:
        def __init__(self, url): self.url = url
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def geturl(self): return self.url
        def read(self): return b'{"accountId":"acct-1"}'

    class Opener:
        def __init__(self, error=None): self.error, self.urls = error, []
        def open(self, request, timeout):
            self.urls.append(request.full_url)
            assert request.headers["Authorization"].startswith("Basic ")
            if self.error: raise self.error
            return Response(request.full_url)

    with mock.patch.dict(module.os.environ, {}, clear=True):
        opener = Opener()
        ok = module.jira_readiness(repo, config, skip_live=False, opener=opener)
        assert ok["state"] == "ready" and ok["live_check"] == "passed", ok
        assert opener.urls == ["https://jira.example/rest/api/3/myself"]
        assert ok["credential_sources"] == {"JIRA_API_TOKEN": "file", "JIRA_EMAIL": "file"}
        assert "file-secret-token" not in json.dumps(ok)
        for error, fragment in (
            (HTTPError("https://jira.example", 401, "Unauthorized", {}, None), "HTTP 401"),
            (HTTPError("https://jira.example", 403, "Forbidden", {}, None), "HTTP 403"),
            (URLError("offline"), "could not reach Jira"),
        ):
            bad = module.jira_readiness(repo, config, skip_live=False, opener=Opener(error))
            assert bad["state"] == "blocked" and bad["live_check"] == "failed", bad
            assert fragment in bad["reason"], bad
            assert "file-secret-token" not in json.dumps(bad)
        skipped_opener = Opener()
        skipped = module.jira_readiness(repo, config, skip_live=True, opener=skipped_opener)
        assert skipped["state"] == "ready" and skipped["live_check"] == "skipped"
        assert skipped_opener.urls == []
        env.chmod(0o644)
        loose = module.jira_readiness(repo, config, skip_live=False, opener=Opener())
        assert loose["state"] == "blocked" and "chmod 600" in loose["reason"], loose
    with mock.patch.dict(module.os.environ, {"JIRA_API_TOKEN": "env-token"}, clear=True):
        env.chmod(0o600)
        config.write_text(config.read_text().replace("  kind: jira ", "  kind: none ", 1))
        wrong_kind = module.jira_readiness(repo, config, skip_live=True)
        assert wrong_kind["state"] == "blocked" and "ticket.kind: jira" in wrong_kind["reason"]
finally:
    shutil.rmtree(repo)
PY
}
check "captain preflight authenticates Jira through an injectable transport" \
  check_preflight_verifies_jira_credentials
check_preflight_reports_effective_budget_limits() {
  python3 - "$ROOT/scripts/captain-preflight.py" "$JIRA_REPO/.orchestration/config.yaml" "$JIRA_REPO/subscription-preflight.json" <<'PY'
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("captain_preflight", sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
cli = json.load(open(sys.argv[3]))
assert cli["budget_limits"]["max_usd_per_run"] == "10.00", cli["budget_limits"]
report = module.budget_report(Path(sys.argv[2]))
assert report["budget_limits"]["max_usd_per_run"] == "10.00"
assert {"key": "max_usd_per_run", "configured": "200", "effective": "10.00", "cap": "10.00"} in report["budget_cap_warnings"], report
import api_agent
helper = api_agent.budget_cap_violations
del api_agent.budget_cap_violations
try:
    report = module.budget_report(Path(sys.argv[2]))
finally:
    api_agent.budget_cap_violations = helper
assert "budget_cap_warnings" not in report
json.dumps(report)
PY
}
check "captain preflight reports effective budget limits and optional cap warnings" \
  check_preflight_reports_effective_budget_limits
check "Claude sprint preflight documents the Jira credential file" \
  rg -q '\.orchestration/\.env' "$ROOT/commands/orchestrate-sprint.md"
check "Codex sprint preflight documents the Jira credential file" \
  rg -q '\.orchestration/\.env' "$ROOT/skills/orchestrate-sprint/SKILL.md"
check "sprint controller docs define Jira credential loading" \
  rg -q 'JIRA_API_TOKEN.*\.orchestration/\.env|\.orchestration/\.env.*JIRA_API_TOKEN' "$ROOT/docs/sprint-controller.md"
check "template points Jira credentials at the gitignored env file" \
  rg -q 'JIRA_API_TOKEN.*\.orchestration/\.env' "$ROOT/templates/config.yaml"
check "Claude init names Jira credentials in the gitignored env file" \
  rg -q 'JIRA_API_TOKEN' "$ROOT/commands/orchestration-init.md"
check "Codex init names Jira credentials in the gitignored env file" \
  rg -q 'JIRA_API_TOKEN' "$ROOT/skills/orchestration-init/SKILL.md"
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
