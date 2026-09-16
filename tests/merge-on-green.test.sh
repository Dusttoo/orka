#!/usr/bin/env bash
# merge-on-green.test.sh -- tests for the safe-merge guard rails that run before
# any network call: the refuse-unless-green check and the merge lock. The actual
# `gh pr merge` + fetch/verify path needs a live remote and is covered by the
# integration run, not here.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fails=0
assert_exit() { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then printf 'ok   %s (exit %s)\n' "$1" "$3"
  else printf 'FAIL %s: want exit %s, got %s\n' "$1" "$2" "$3"; fails=$((fails + 1)); fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/.orchestration"
cp "$HERE"/../scripts/lib-config.sh "$HERE"/../scripts/merge-guard.sh \
   "$HERE"/../scripts/merge-on-green.sh "$HERE"/../scripts/orchestration-engine.py \
   "$HERE"/../scripts/version_policy.py \
   "$TMP/repo/"
printf 'integration_branch: develop\nproduction_branch: main\nmerge_to_integration: merge\n' > "$TMP/repo/.orchestration/config.yaml"
cd "$TMP/repo" && git init -q
git config user.email t@t.t
git config user.name t
git add -A
git commit -qm init
MOG="$TMP/repo/merge-on-green.sh"
export MERGE_GUARD_STATUS_DIR="$TMP/repo/.orchestration/.gate-status"
export MERGE_GUARD_PLUGIN_VERSION="test-version"
export MERGE_GUARD_PR_HEAD_BRANCH="feat/x"
export MERGE_GUARD_PR_HEAD_SHA="test-head-sha"
export MERGE_GUARD_PR_BASE_BRANCH="develop"
export MERGE_GUARD_PR_BASE_SHA="test-base-sha"

# 1. A non-"all-green" gate status is refused before anything else happens.
bash "$MOG" 42 feat/x not-green >/dev/null 2>&1
assert_exit "refuses when gate is not all-green" 2 "$?"

# Remaining pre-network cases need valid host-neutral evidence; no hook is
# involved. This proves merge-on-green performs the assertion directly.
bash "$TMP/repo/merge-guard.sh" --record-green 42 >/dev/null 2>&1

# 2. When the merge lock is already held, a second merge is refused with 75
#    (EX_TEMPFAIL) and does not disturb the existing lock.
echo "pid=1 pr=1 held" > "$TMP/repo/.git/orchestrator-merge.lock"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "refuses when merge lock is held" 75 "$?"
grep -q "pid=1 pr=1 held" "$TMP/repo/.git/orchestrator-merge.lock" \
  && printf 'ok   existing lock left intact\n' \
  || { printf 'FAIL existing lock was disturbed\n'; fails=$((fails + 1)); }
rm "$TMP/repo/.git/orchestrator-merge.lock"

# 3. A linked worktree has a .git file, not a directory. Its merge wrapper must
#    use the shared Git common directory and observe a lock created elsewhere.
git worktree add -q --detach "$TMP/worktree"
echo "pid=1 pr=1 held-from-primary" > "$TMP/repo/.git/orchestrator-merge.lock"
cd "$TMP/worktree"
bash "$MOG" 42 feat/x all-green >/dev/null 2>&1
assert_exit "linked worktree observes shared merge lock" 75 "$?"
rm "$TMP/repo/.git/orchestrator-merge.lock"
cd "$TMP/repo"

# 4. Regression: the merge must not use --delete-branch (that couples branch
#    cleanup to the merge and, under set -e, aborts a verified merge when a
#    worktree still holds the branch), and branch deletion must be best-effort.
SRC="$HERE/../scripts/merge-on-green.sh"
grep -Eq 'gh pr merge "\$PR" "\$MERGE_FLAG"[[:space:]]*$' "$SRC" \
  && printf 'ok   merge invocation has no --delete-branch\n' \
  || { printf 'FAIL merge invocation still couples --delete-branch\n'; fails=$((fails + 1)); }
grep -Eq 'git push origin --delete "\$BRANCH".*\|\| true' "$SRC" \
  && grep -Eq 'git branch -D "\$BRANCH".*\|\| true' "$SRC" \
  && printf 'ok   branch deletion is best-effort (remote + local, tolerant)\n' \
  || { printf 'FAIL branch deletion is not best-effort\n'; fails=$((fails + 1)); }
grep -Eq 'merge-guard\.sh" --assert-green "\$PR" "\$BRANCH"' "$SRC" \
  && printf 'ok   sanctioned merge validates evidence without hooks\n' \
  || { printf 'FAIL sanctioned merge still depends on host hooks\n'; fails=$((fails + 1)); }

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
