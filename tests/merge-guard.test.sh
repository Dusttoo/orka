#!/usr/bin/env bash
# merge-guard.test.sh -- behavioural tests for the merge-guard hook.
#
# The guard is the enforcement centrepiece, so every gate path is exercised:
# pass-through, shlex precision, no-marker, valid marker, moved-sha, expired
# marker, the always-blocked production-branch / squash paths, and the
# fail-closed no-python3 fallback.
#
# Isolation: MERGE_GUARD_STATUS_DIR redirects the marker dir, and
# MERGE_GUARD_PR_HEAD_SHA stubs the `gh pr view` head-sha lookup so no network
# or real PR is needed. Each marker-sensitive case uses its own PR number so the
# tests do not couple through shared marker state. exit 0 = ALLOW, exit 2 = BLOCK.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fails=0
assert_exit() { # <desc> <expected-code> <actual-code>
  if [ "$2" = "$3" ]; then printf 'ok   %s (exit %s)\n' "$1" "$3"
  else printf 'FAIL %s: want exit %s, got %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}
assert_true() { # <desc> <cond-cmd...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then printf 'ok   %s\n' "$desc"
  else printf 'FAIL %s\n' "$desc"; fails=$((fails + 1)); fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export MERGE_GUARD_STATUS_DIR="$TMP/markers"
export MERGE_GUARD_PLUGIN_VERSION="test-version"
export MERGE_GUARD_PR_HEAD_BRANCH="feat/test"
export MERGE_GUARD_PR_BASE_BRANCH="develop"
export MERGE_GUARD_PR_BASE_SHA="base-sha"
mkdir -p "$MERGE_GUARD_STATUS_DIR"
mkdir -p "$TMP/repo/.orchestration"
cp "$HERE/../scripts/lib-config.sh" "$HERE/../scripts/merge-guard.sh" \
   "$HERE/../scripts/orchestration-engine.py" "$HERE/../scripts/version_policy.py" \
   "$TMP/repo/"
printf 'integration_branch: develop\nproduction_branch: main\n' > "$TMP/repo/.orchestration/config.yaml"
cd "$TMP/repo" && git init -q
GUARD="$TMP/repo/merge-guard.sh"

payload() {
  printf '{"tool_name":"%s","tool_input":{"command":%s}}' \
    "$1" "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
}
# run <tool> <command> ; echoes the payload into the guard and returns its exit
# code. Env stubs are read from the (exported) environment, so callers wanting a
# stub run inside a subshell that `export`s it -- see below.
run() { payload "$1" "$2" | bash "$GUARD" >/dev/null 2>&1; echo $?; }
record_green() { MERGE_GUARD_PR_HEAD_SHA="$2" bash "$GUARD" --record-green "$1" >/dev/null 2>&1; }

HEAD="abc1234def5678"

# 1. A non-merge Bash command passes straight through.
assert_exit "non-merge command allowed" 0 "$(run Bash 'git status')"

# 2. shlex precision: a commit whose TEXT contains 'gh pr merge' is NOT a merge.
assert_exit "commit body mentioning gh pr merge allowed" 0 \
  "$(run Bash 'git commit -m "will gh pr merge after review"')"

# 3. A real merge with no marker is blocked.
assert_exit "merge without marker blocked" 2 "$(run Bash 'gh pr merge 42 --merge')"
assert_exit "record-green refuses wrong configured base" 2 \
  "$(MERGE_GUARD_PR_HEAD_SHA="$HEAD" MERGE_GUARD_PR_BASE_BRANCH=main bash "$GUARD" --record-green 42 >/dev/null 2>&1; echo $?)"

# Repository minimum version applies both when evidence is recorded and when it
# is asserted for merge. A marker from an incompatible runtime cannot help.
printf 'integration_branch: develop\nproduction_branch: main\nminimum_orka_version: 1.6.0\n' \
  > "$TMP/repo/.orchestration/config.yaml"
assert_exit "record-green rejects runtime below repository minimum" 2 \
  "$(MERGE_GUARD_PLUGIN_VERSION=1.5.7 MERGE_GUARD_PR_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 41 >/dev/null 2>&1; echo $?)"
assert_exit "record-green accepts runtime meeting repository minimum" 0 \
  "$(MERGE_GUARD_PLUGIN_VERSION=1.6.0 MERGE_GUARD_PR_HEAD_SHA="$HEAD" bash "$GUARD" --record-green 41 >/dev/null 2>&1; echo $?)"
printf 'integration_branch: develop\nproduction_branch: main\nminimum_orka_version: 1.7.0\n' \
  > "$TMP/repo/.orchestration/config.yaml"
assert_exit "assert-green rejects runtime below repository minimum" 2 \
  "$(MERGE_GUARD_PLUGIN_VERSION=1.6.0 MERGE_GUARD_PR_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 41 feat/test >/dev/null 2>&1; echo $?)"
printf 'integration_branch: develop\nproduction_branch: main\n' > "$TMP/repo/.orchestration/config.yaml"

# 4. A merge with a valid, fresh marker whose sha matches HEAD is allowed.
record_green 43 "$HEAD"
assert_exit "host-neutral assertion accepts exact identity" 0 \
  "$(MERGE_GUARD_PR_HEAD_SHA="$HEAD" bash "$GUARD" --assert-green 43 feat/test >/dev/null 2>&1; echo $?)"
assert_exit "merge with valid fresh marker allowed" 0 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 43 --merge')"

# 5. Same marker, but the branch has moved (HEAD sha differs) -> blocked.
record_green 44 "$HEAD"
assert_exit "merge with moved sha blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA='999newsha000'; run Bash 'gh pr merge 44 --merge')"

# 6. Marker sha matches but it is older than MAX_AGE -> blocked.
printf 'all-green pr=45 plugin_version=test-version head_branch=feat/test head_sha=%s base_branch=develop base_sha=base-sha recorded_at=2020-01-01T00:00:00Z\n' "$HEAD" \
  > "$MERGE_GUARD_STATUS_DIR/pr-45.green"
assert_exit "merge with expired marker blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 45 --merge')"

# Exact identity and plugin binding: any reader change invalidates the marker.
record_green 47 "$HEAD"
assert_exit "changed head branch blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD" MERGE_GUARD_PR_HEAD_BRANCH='feat/other'; run Bash 'gh pr merge 47 --merge')"
assert_exit "changed base sha blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD" MERGE_GUARD_PR_BASE_SHA='new-base'; run Bash 'gh pr merge 47 --merge')"
assert_exit "changed plugin version blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD" MERGE_GUARD_PLUGIN_VERSION='next-version'; run Bash 'gh pr merge 47 --merge')"

# 7. A direct merge to the production branch is always blocked, marker or not.
record_green 46 "$HEAD"
assert_exit "merge --base main blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --base main --merge')"
assert_exit "merge --squash blocked" 2 \
  "$(export MERGE_GUARD_PR_HEAD_SHA="$HEAD"; run Bash 'gh pr merge 46 --squash')"

# 8. A non-Bash tool is ignored.
assert_exit "non-Bash tool ignored" 0 "$(run Read 'gh pr merge 42')"

# 9. Fail-closed fallback (no python3): still blocks a no-marker merge (PR 99,
#    which has no marker), still allows a plainly non-merge command.
assert_exit "fallback blocks no-marker merge" 2 \
  "$(export MERGE_GUARD_FORCE_FALLBACK=1; run Bash 'gh pr merge 99 --merge')"
assert_exit "fallback allows non-merge" 0 \
  "$(export MERGE_GUARD_FORCE_FALLBACK=1; run Bash 'git status')"

# 10. Plugin hook mode is inert until a repo opts in with orchestration config.
EMPTY="$TMP/empty-repo"
mkdir -p "$EMPTY"
git -C "$EMPTY" init -q
unset MERGE_GUARD_STATUS_DIR
payload Bash 'git status' | (cd "$EMPTY" && bash "$HERE/../scripts/merge-guard.sh") >/dev/null 2>&1
assert_exit "uninitialized repo non-merge hook no-ops" 0 "$?"
payload Bash 'gh pr merge 123 --merge' | (cd "$EMPTY" && bash "$HERE/../scripts/merge-guard.sh") >/dev/null 2>&1
assert_exit "uninitialized repo merge hook no-ops" 0 "$?"
assert_true "uninitialized repo hook creates no .orchestration dir" test ! -e "$EMPTY/.orchestration"
export MERGE_GUARD_STATUS_DIR="$TMP/markers"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
