#!/usr/bin/env bash
# security-gate.test.sh -- security review requirements are decided mechanically
# from diff content and authoritative PR branch metadata.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE/.."
ENGINE="$ROOT/scripts/orchestration-engine.py"

fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/config.yaml" <<'YAML'
integration_branch: develop
production_branch: main
security_required_when:
  - migrations/
  - auth
security_required_source_branches:
  - hotfix/**
security_required_target_branches:
  - main
YAML

cat > "$TMP/harmless.diff" <<'DIFF'
diff --git a/src/components/join-form.tsx b/src/components/join-form.tsx
--- a/src/components/join-form.tsx
+++ b/src/components/join-form.tsx
@@ -1 +1 @@
-const label = "Join";
+const label = "Join now";
DIFF

cat > "$TMP/security.diff" <<'DIFF'
diff --git a/src/auth/session.ts b/src/auth/session.ts
--- a/src/auth/session.ts
+++ b/src/auth/session.ts
@@ -1 +1 @@
-const ttl = 60;
+const ttl = 30;
DIFF

decision() {
  "$ENGINE" --config "$TMP/config.yaml" security-gate \
    --source-branch "$1" --target-branch "$2" --diff-file "$3"
}

assert_json() {
  local desc="$1" expression="$2" json="$3"
  if python3 - "$expression" "$json" <<'PY'
import json
import sys

value = json.loads(sys.argv[2])
if not eval(sys.argv[1], {"__builtins__": {}}, {"value": value}):
    raise SystemExit(1)
PY
  then ok "$desc"; else fail_case "$desc"; fi
}

plain="$(decision feature/join develop "$TMP/harmless.diff")"
assert_json "harmless feature diff does not require security" \
  'value["required"] is False and value["reasons"] == []' "$plain"

hotfix="$(decision refs/heads/hotfix/join-form develop "$TMP/harmless.diff")"
assert_json "hotfix source branch forces security review" \
  'value["required"] is True and value["reasons"][0]["kind"] == "source_branch"' "$hotfix"

main_target="$(decision feature/join main "$TMP/harmless.diff")"
assert_json "main target branch forces security review" \
  'value["required"] is True and value["reasons"][0]["kind"] == "target_branch"' "$main_target"

diff_match="$(decision feature/session develop "$TMP/security.diff")"
assert_json "configured diff path still forces security review" \
  'value["required"] is True and value["reasons"][0]["kind"] == "diff_path"' "$diff_match"

if "$ENGINE" --config "$TMP/config.yaml" security-gate \
  --source-branch feature/x --target-branch develop --diff-file "$TMP/missing.diff" \
  >/dev/null 2>&1; then
  fail_case "missing diff evidence is rejected"
else
  ok "missing diff evidence is rejected"
fi

: > "$TMP/empty.diff"
if "$ENGINE" --config "$TMP/config.yaml" security-gate \
  --source-branch hotfix/x --target-branch main --diff-file "$TMP/empty.diff" \
  >/dev/null 2>&1; then
  fail_case "empty diff evidence is rejected even when branches match"
else
  ok "empty diff evidence is rejected even when branches match"
fi

cat > "$TMP/invalid.yaml" <<'YAML'
integration_branch: develop
production_branch: main
security_required_source_branches:
  nested: invalid
YAML
if "$ENGINE" --config "$TMP/invalid.yaml" validate-config >/dev/null 2>&1; then
  fail_case "invalid branch trigger shape is rejected"
else
  ok "invalid branch trigger shape is rejected"
fi

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
