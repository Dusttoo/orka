#!/usr/bin/env bash
# run-verification.test.sh -- tests the generic verification runner and its
# handshake with merge-guard --record-green: a GREEN run writes a sha-stamped
# result file, a RED run writes none, and --record-green accepts the file only
# when its sha matches the PR head.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fails=0
assert_exit() { [ "$2" = "$3" ] && printf 'ok   %s (exit %s)\n' "$1" "$3" \
  || { printf 'FAIL %s: want %s got %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); }; }
assert_true() { local d="$1"; shift; if "$@" >/dev/null 2>&1; then printf 'ok   %s\n' "$d"; \
  else printf 'FAIL %s\n' "$d"; fails=$((fails + 1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/.orchestration"
cp "$HERE"/../scripts/lib-config.sh "$HERE"/../scripts/run-verification.sh \
   "$HERE"/../scripts/merge-guard.sh "$HERE"/../scripts/orchestration-engine.py \
   "$HERE"/../scripts/version_policy.py \
   "$TMP/repo/"
cat > "$TMP/repo/.orchestration/config.yaml" <<'YAML'
integration_branch: develop
production_branch: main
verification:
  - name: green-check
    run: 'true'
  - name: red-check
    run: 'false'
YAML
cd "$TMP/repo" && git init -q >/dev/null
echo x > f; git add -A; git -c user.email=t@t.t -c user.name=t commit -qm init
export GATE_STATUS_DIR="$TMP/markers"
export MERGE_GUARD_STATUS_DIR="$TMP/markers"
export MERGE_GUARD_PLUGIN_VERSION="test-version"
export MERGE_GUARD_PR_HEAD_BRANCH="feat/verify"
export MERGE_GUARD_PR_BASE_BRANCH="develop"
export MERGE_GUARD_PR_BASE_SHA="base-sha"
SHA="$(git rev-parse HEAD)"

# 1. A GREEN verification exits 0 and writes a sha-stamped result file.
bash run-verification.sh green-check >/dev/null 2>&1
assert_exit "green verification exits 0" 0 "$?"
RESULT="$TMP/markers/verify-green-check-${SHA}.green"
assert_true "green verification wrote its result file" test -f "$RESULT"

# 2. A RED verification exits 1 and writes NO result file.
bash run-verification.sh red-check >/dev/null 2>&1
assert_exit "red verification exits 1" 1 "$?"
assert_true "red verification wrote no result file" test ! -e "$TMP/markers/verify-red-check-${SHA}.green"

# 3. An unknown verification name is refused.
bash run-verification.sh nope >/dev/null 2>&1
assert_exit "unknown verification refused" 2 "$?"

# 4. --record-green accepts the matching result file (sha == head).
MERGE_GUARD_PR_HEAD_SHA="$SHA" bash merge-guard.sh --record-green 7 "$RESULT" >/dev/null 2>&1
assert_exit "record-green accepts matching result file" 0 "$?"
assert_true "marker notes it was verified" grep -q "verified_by=" "$TMP/markers/pr-7.green"

# 5. --record-green refuses a result file whose sha != PR head.
MERGE_GUARD_PR_HEAD_SHA="differentsha999" bash merge-guard.sh --record-green 8 "$RESULT" >/dev/null 2>&1
assert_exit "record-green refuses mismatched result file" 2 "$?"
assert_true "no marker written on mismatch" test ! -e "$TMP/markers/pr-8.green"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
