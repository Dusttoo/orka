#!/usr/bin/env bash
# merge-guard.sh -- Claude Code / Codex PreToolUse hook (matcher: Bash). Turns
# "never merge on red" from orchestrator discipline into a MECHANISM: a raw
# `gh pr merge` is blocked unless the gate pipeline recorded an all-green marker
# whose sha matches the PR's current head AND is recent. Additional blocked
# merge targets and squash policy are read from the configured workflow.
#
# Hook contract:
#   stdin  : the PreToolUse JSON (.tool_name, .tool_input.command)
#   exit 0 : ALLOW (not a merge, or a valid green marker exists)
#   exit 2 : BLOCK; stderr is fed back to the model as the reason.
#
# Controller modes (host-neutral; called by both Claude Code and Codex):
#   merge-guard.sh --record-green <pr> [result_file]
#       Stamp a marker with the plugin version and exact PR head/base identity.
#       If a result_file from
#       run-verification.sh is given, its embedded sha MUST match the PR head, so
#       a marker cannot be recorded without the verification actually having run
#       on the current commit.
#   merge-guard.sh --assert-green <pr> [expected_head_branch]
#       Re-read GitHub and require the marker's plugin version, head name/sha,
#       base name/sha, and freshness to match exactly. The sanctioned merge script
#       calls this directly, so hook registration is never a correctness dependency.
#   merge-guard.sh --clear <pr>          # drop the marker (e.g. after a rebase)
#
# Fail-closed by design: if the payload cannot be parsed (e.g. python3 absent),
# the guard treats anything that looks like `gh pr merge` as a merge and blocks
# it, rather than silently disabling itself.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

CONFIG_FILE="$(orch_config_file)"
STATUS_DIR="${MERGE_GUARD_STATUS_DIR:-$(orch_project_root)/.orchestration/.gate-status}"
MAX_AGE="${MERGE_GUARD_MAX_AGE_SECONDS:-3600}"

resolve_plugin_version() {
  local codex_version claude_version
  if [ -n "${MERGE_GUARD_PLUGIN_VERSION:-}" ]; then
    printf '%s' "$MERGE_GUARD_PLUGIN_VERSION"
    return 0
  fi
  codex_version="$(sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$HERE/../.codex-plugin/plugin.json" 2>/dev/null | head -1)"
  claude_version="$(sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$HERE/../.claude-plugin/plugin.json" 2>/dev/null | head -1)"
  [ -n "$codex_version" ] && [ "$codex_version" = "$claude_version" ] || return 1
  printf '%s' "$codex_version"
}

# Exact tab-separated reader contract:
# head branch, head sha, base branch, base sha. Tests can supply all four via
# environment variables; production re-reads the PR directly from GitHub.
resolve_pr_identity() {
  local pr="$1" target
  if [ -n "${MERGE_GUARD_PR_HEAD_SHA:-}" ]; then
    target="$(orch_branch_name "${MERGE_TARGET_ROLE:-integration}" 2>/dev/null)"
    printf '%s\t%s\t%s\t%s\n' \
      "${MERGE_GUARD_PR_HEAD_BRANCH:-test-head}" \
      "$MERGE_GUARD_PR_HEAD_SHA" \
      "${MERGE_GUARD_PR_BASE_BRANCH:-$target}" \
      "${MERGE_GUARD_PR_BASE_SHA:-test-base-sha}"
    return 0
  fi
  gh pr view "$pr" --json headRefName,headRefOid,baseRefName,baseRefOid \
    --jq '[.headRefName,.headRefOid,.baseRefName,.baseRefOid] | @tsv' 2>/dev/null
}

marker_value() {
  local marker="$1" key="$2"
  grep -Eo "(^|[[:space:]])${key}=[^[:space:]]+" "$marker" 2>/dev/null \
    | head -1 | sed -E "s/^[[:space:]]*${key}=//"
}

assert_green() {
  local pr="$1" expected_head="${2:-}" marker identity
  local head_branch head_sha base_branch base_sha target_branch plugin_version
  local mark_version mark_head_branch mark_head_sha mark_base_branch mark_base_sha mark_at mark_epoch age

  marker="${STATUS_DIR}/pr-${pr}.green"
  if [ ! -f "$marker" ] || ! grep -q '^all-green' "$marker"; then
    echo "merge-guard: REFUSED: no all-green marker for PR #$pr." >&2
    return 2
  fi
  identity="$(resolve_pr_identity "$pr")"
  IFS=$'\t' read -r head_branch head_sha base_branch base_sha <<<"$identity"
  if [ -z "$head_branch" ] || [ -z "$head_sha" ] || [ -z "$base_branch" ] || [ -z "$base_sha" ]; then
    echo "merge-guard: REFUSED: could not resolve exact head/base identity for PR #$pr." >&2
    return 2
  fi
  target_branch="$(orch_branch_name "${MERGE_TARGET_ROLE:-integration}" 2>/dev/null)"
  plugin_version="$(resolve_plugin_version)"
  if [ -z "$plugin_version" ] || [ -z "$target_branch" ]; then
    echo "merge-guard: REFUSED: could not resolve plugin version or configured target branch." >&2
    return 2
  fi
  if ! orch_assert_minimum_version "$plugin_version"; then
    echo "merge-guard: REFUSED: active Orka runtime does not satisfy the repository minimum version." >&2
    return 2
  fi

  mark_version="$(marker_value "$marker" plugin_version)"
  mark_head_branch="$(marker_value "$marker" head_branch)"
  mark_head_sha="$(marker_value "$marker" head_sha)"
  mark_base_branch="$(marker_value "$marker" base_branch)"
  mark_base_sha="$(marker_value "$marker" base_sha)"
  if [ "$mark_version" != "$plugin_version" ]; then
    echo "merge-guard: REFUSED: marker plugin version '${mark_version:-missing}' != active '$plugin_version'. Re-gate." >&2
    return 2
  fi
  if [ -n "$expected_head" ] && [ "$head_branch" != "$expected_head" ]; then
    echo "merge-guard: REFUSED: PR #$pr head '$head_branch' != expected '$expected_head'." >&2
    return 2
  fi
  if [ "$base_branch" != "$target_branch" ]; then
    echo "merge-guard: REFUSED: PR #$pr base '$base_branch' != configured '$target_branch'." >&2
    return 2
  fi
  if [ "$mark_head_branch" != "$head_branch" ] || [ "$mark_head_sha" != "$head_sha" ] \
     || [ "$mark_base_branch" != "$base_branch" ] || [ "$mark_base_sha" != "$base_sha" ]; then
    echo "merge-guard: REFUSED: PR #$pr head/base identity changed after gating. Re-gate." >&2
    return 2
  fi

  mark_at="$(marker_value "$marker" recorded_at)"
  mark_epoch="$(iso_to_epoch "$mark_at")"
  if [ -z "$mark_epoch" ]; then
    echo "merge-guard: REFUSED: marker timestamp is missing or invalid." >&2
    return 2
  fi
  age=$(( $(date -u +%s) - mark_epoch ))
  if [ "$age" -gt "$MAX_AGE" ] || [ "$age" -lt "-60" ]; then
    echo "merge-guard: REFUSED: marker for PR #$pr is ${age}s old (max ${MAX_AGE}s). Re-gate." >&2
    return 2
  fi
  echo "merge-guard: all-green identity verified for PR #$pr (plugin=$plugin_version head=$head_branch@$head_sha base=$base_branch@$base_sha)."
  return 0
}

# Parse an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ) to epoch seconds.
# GNU date first, BSD/macOS date as fallback. Empty on failure.
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null \
    || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null
}

regex_escape() {
  printf '%s' "$1" | sed 's/[][\\.^$*+?{}|()]/\\&/g'
}

case "${1:-}" in
  --record-green)
    if [ ! -f "$CONFIG_FILE" ]; then
      echo "merge-guard: REFUSED: .orchestration/config.yaml not found; run orchestration-init first." >&2
      exit 2
    fi
    if ! orch_validate_config; then
      echo "merge-guard: REFUSED: orchestration config is invalid; validate it before recording a green marker." >&2
      exit 2
    fi
    PR="${2:?usage: merge-guard.sh --record-green <pr> [result_file]}"
    RESULT_FILE="${3:-}"
    IDENTITY="$(resolve_pr_identity "$PR")"
    IFS=$'\t' read -r HEAD_BRANCH SHA BASE_BRANCH BASE_SHA <<<"$IDENTITY"
    TARGET_BRANCH="$(orch_branch_name "${MERGE_TARGET_ROLE:-integration}" 2>/dev/null)"
    PLUGIN_VERSION="$(resolve_plugin_version)"
    if [ -z "$HEAD_BRANCH" ] || [ -z "$SHA" ] || [ -z "$BASE_BRANCH" ] || [ -z "$BASE_SHA" ]; then
      echo "merge-guard: REFUSED: could not resolve exact PR #$PR head/base identity." >&2
      exit 2
    fi
    if [ -z "$PLUGIN_VERSION" ]; then
      echo "merge-guard: REFUSED: could not resolve active plugin version." >&2
      exit 2
    fi
    if ! orch_assert_minimum_version "$PLUGIN_VERSION"; then
      echo "merge-guard: REFUSED: active Orka runtime does not satisfy the repository minimum version." >&2
      exit 2
    fi
    if [ "$BASE_BRANCH" != "$TARGET_BRANCH" ]; then
      echo "merge-guard: REFUSED: PR #$PR base '$BASE_BRANCH' != configured '$TARGET_BRANCH'." >&2
      exit 2
    fi
    # If a verification result file is supplied, its sha must match the PR head,
    # so a marker cannot be stamped without the verification having run on this
    # exact commit.
    VERIFIED_BY=""
    if [ -n "$RESULT_FILE" ]; then
      if [ ! -f "$RESULT_FILE" ]; then
        echo "merge-guard: REFUSED: result file '$RESULT_FILE' not found." >&2
        exit 2
      fi
      if ! grep -q '^result=GREEN' "$RESULT_FILE"; then
        echo "merge-guard: REFUSED: result file '$RESULT_FILE' is not GREEN." >&2
        exit 2
      fi
      FILE_SHA="$(grep -Eo '^sha=[^[:space:]]+' "$RESULT_FILE" | head -1 | cut -d= -f2)"
      if [ "$FILE_SHA" != "$SHA" ]; then
        echo "merge-guard: REFUSED: result file sha ($FILE_SHA) != PR #$PR head ($SHA)." >&2
        echo "The branch moved after the verification ran. Rebase, re-run it, and retry." >&2
        exit 2
      fi
      VERIFIED_BY=" verified_by=${RESULT_FILE##*/}"
    fi
    mkdir -p "$STATUS_DIR" 2>/dev/null || true
    printf 'all-green pr=%s plugin_version=%s head_branch=%s head_sha=%s base_branch=%s base_sha=%s recorded_at=%s%s\n' \
      "$PR" "$PLUGIN_VERSION" "$HEAD_BRANCH" "$SHA" "$BASE_BRANCH" "$BASE_SHA" \
      "$(date -u +%FT%TZ)" "$VERIFIED_BY" > "${STATUS_DIR}/pr-${PR}.green"
    echo "merge-guard: recorded all-green for PR #$PR (plugin=$PLUGIN_VERSION head=$HEAD_BRANCH@$SHA base=$BASE_BRANCH@$BASE_SHA)${VERIFIED_BY:+, $VERIFIED_BY}."
    exit 0
    ;;
  --assert-green)
    PR="${2:?usage: merge-guard.sh --assert-green <pr> [expected_head_branch]}"
    if [ ! -f "$CONFIG_FILE" ] || ! orch_validate_config; then
      echo "merge-guard: REFUSED: orchestration config is missing or invalid." >&2
      exit 2
    fi
    assert_green "$PR" "${3:-}"
    exit $?
    ;;
  --clear)
    PR="${2:?usage: merge-guard.sh --clear <pr>}"
    rm -f "${STATUS_DIR}/pr-${PR}.green"
    echo "merge-guard: cleared green marker for PR #$PR."
    exit 0
    ;;
esac

# ---- hook mode ----------------------------------------------------------------
# Plugin hooks may be enabled globally by Claude Code or Codex. Outside a repo
# that has opted into this harness, the guard must be invisible and must not
# create runtime directories.
if [ ! -f "$CONFIG_FILE" ]; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

PAYLOAD="$(cat)"

# argv-shape check (shlex) so we fire ONLY on a literal `gh pr merge`, never on a
# command whose TEXT merely contains those words (a commit body, a --body string,
# a shell comment). python3 is the precise path; a bash fallback keeps the guard
# fail-closed if python3 is unavailable.
TOOL=""; IS_MERGE="0"; CMD=""
# MERGE_GUARD_FORCE_FALLBACK=1 exercises the no-python3 path in tests.
if [ -z "${MERGE_GUARD_FORCE_FALLBACK:-}" ] && command -v python3 >/dev/null 2>&1; then
  PARSED="$(printf '%s' "$PAYLOAD" | python3 -c '
import sys, json, base64, shlex
try:
    d = json.load(sys.stdin)
except Exception:
    print("\t\t"); sys.exit(0)
tn = d.get("tool_name", "") or ""
cmd = (d.get("tool_input", {}) or {}).get("command", "") or ""
is_merge = "0"
try:
    argv = shlex.split(cmd, posix=True, comments=True)
    if len(argv) >= 3 and argv[0] == "gh" and argv[1] == "pr" and argv[2] == "merge":
        is_merge = "1"
except Exception:
    pass
print(tn + "\t" + is_merge + "\t" + base64.b64encode(cmd.encode()).decode())
' 2>/dev/null)"
  TOOL="${PARSED%%$'\t'*}"
  REST="${PARSED#*$'\t'}"
  IS_MERGE="${REST%%$'\t'*}"
  CMD_B64="${REST#*$'\t'}"
  CMD="$(printf '%s' "$CMD_B64" | base64 -d 2>/dev/null || true)"
else
  # Fail-closed fallback: no precise parse available. Pull tool_name and command
  # with a best-effort grep and flag anything resembling `gh pr merge`.
  TOOL="$(printf '%s' "$PAYLOAD" | grep -Eo '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"([^"]*)"$/\1/')"
  CMD="$(printf '%s' "$PAYLOAD" | grep -Eo '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/^"command"[[:space:]]*:[[:space:]]*"//; s/"$//')"
  if printf '%s' "$CMD" | grep -Eq 'gh[[:space:]]+pr[[:space:]]+merge'; then IS_MERGE="1"; fi
  [ -n "$TOOL" ] || TOOL="Bash"   # assume Bash if we cannot read it (fail-closed)
fi

[ "$TOOL" = "Bash" ] || exit 0
[ "$IS_MERGE" = "1" ] || exit 0

# Configured policy can block selected merge targets and squash merges. If the
# policy cannot be read, block the merge rather than under-enforcing.
POLICY="$(python3 "$HERE/orchestration-engine.py" guard-policy 2>/dev/null)"
if [ "$?" -ne 0 ]; then
  echo "BLOCKED by merge-guard: orchestration config is invalid or guard policy cannot be resolved." >&2
  exit 2
fi
BLOCK_SQUASH="$(printf '%s\n' "$POLICY" | awk -F= '$1=="block_squash"{print $2; exit}')"
if [ "$BLOCK_SQUASH" = "true" ] && printf '%s' "$CMD" | grep -Eq '\-\-squash'; then
  echo "BLOCKED by merge-guard: configured policy blocks direct squash merges." >&2
  exit 2
fi
while IFS= read -r BLOCKED_BRANCH; do
  [ -n "$BLOCKED_BRANCH" ] || continue
  BR_RE="$(regex_escape "$BLOCKED_BRANCH")"
  if printf '%s' "$CMD" | grep -Eq -- "--base[[:space:]=]+${BR_RE}([[:space:]]|$)" \
     || printf '%s' "$CMD" | grep -Eq "[[:space:]]${BR_RE}([[:space:]]|\$)"; then
    echo "BLOCKED by merge-guard: configured policy blocks direct merges to '${BLOCKED_BRANCH}'." >&2
    exit 2
  fi
done < <(printf '%s\n' "$POLICY" | awk -F= '$1=="blocked_branch"{print $2}')

# PR id = first token after 'merge', strip any URL prefix.
PR="$(printf '%s' "$CMD" | grep -Eo 'gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+[^[:space:]]+' | head -1 | awk '{print $4}')"
PR="${PR##*/}"

if [ -n "$PR" ] && assert_green "$PR" >/dev/null; then
  echo "merge-guard: all-green plugin/head/base identity matches and is fresh -- allowing direct merge." >&2
  exit 0
fi

{
  echo "BLOCKED by merge-guard: refusing direct 'gh pr merge' for PR #${PR:-?} -- no all-green marker."
  echo "Run the gate pipeline first; it records the marker after all gates PASS and CI is green."
  echo "To merge by hand after that, the marker's sha must match the PR's current head and be recent"
  echo "(a rebase or a long delay invalidates it -- re-gate)."
} >&2
exit 2
