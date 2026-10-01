#!/usr/bin/env bash
# preflight.sh -- one-time environment check before dispatching a wave of agents.
# Run from the repo root. Exits non-zero (and prints why) if the environment is
# not ready. Universal checks (git, gh, configured base branch, stale lock) always
# run; language-specific checks run only when the repo shows evidence of that
# toolchain, so the harness is not Node-specific.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-config.sh
. "$HERE/lib-config.sh"

fail() { echo "PREFLIGHT FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

BASE_ROLE="${PREFLIGHT_BRANCH_ROLE:-integration}"
BASE_BRANCH="$(orch_branch_name "$BASE_ROLE")"
CONCURRENCY="$(orch_get concurrency_max 2)"
MAX_HEAVY="$(orch_get max_heavy_processes 2)"
case "$CONCURRENCY:$MAX_HEAVY" in
  *[!0-9:]*|0:*|*:0) fail "concurrency_max and max_heavy_processes must be positive integers" ;;
esac

echo "== orchestration preflight =="

# Repo + config sanity
[ -e .git ] || fail "run this from the repository root (no .git entry here)"
[ -f .orchestration/config.yaml ] || fail "missing .orchestration/config.yaml (copy templates/config.yaml)"
ok "in repo root with orchestration config"

# Reconcile every pre-0.11 runtime copy across every registered worktree before
# reporting readiness. Any differing dormant copy is an enforcement conflict,
# not a warning that a captain may ignore.
python3 - "$HERE" <<'PY' || fail "legacy runtime state conflicts across git worktrees"
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from runtime_state import migrate_legacy_runtime_dir
for relative in (
    ".orchestration/.llm-usage", ".orchestration/.llm-runs",
    ".orchestration/.review-ledger", ".orchestration/.sprint-state",
):
    migrate_legacy_runtime_dir(Path.cwd(), relative)
PY
ok "runtime state reconciled across all git worktrees"

# Universal tooling
command -v git >/dev/null || fail "git not found"
command -v gh  >/dev/null || fail "gh CLI not found"
ok "git, gh present"
gh auth status >/dev/null 2>&1 || fail "gh is not authenticated -- run 'gh auth login'"
ok "gh authenticated"

# Configured base branch reachable
git fetch origin "$BASE_BRANCH" --quiet || fail "cannot fetch origin/${BASE_BRANCH}"
HEAD_SHA="$(git rev-parse "origin/${BASE_BRANCH}")"
ok "origin/${BASE_BRANCH} @ ${HEAD_SHA:0:12}"

# Stale merge lock from a crashed run
if [ -f .git/orchestrator-merge.lock ]; then
  echo "WARNING: stale merge lock exists (.git/orchestrator-merge.lock):"
  cat .git/orchestrator-merge.lock
  echo "If no merge is in progress, remove it: rm .git/orchestrator-merge.lock"
fi

# Node toolchain -- only if the repo is a Node project
if [ -f package.json ]; then
  command -v node >/dev/null || fail "package.json present but node not found"
  command -v npm  >/dev/null || fail "package.json present but npm not found"
  [ -d node_modules ] || fail "node_modules missing -- run 'npm ci'"
  ok "node, npm, node_modules present"
fi

# Playwright -- only if the repo has a Playwright config
if [ -f playwright.config.ts ] || [ -f playwright.config.js ]; then
  if npx playwright --version >/dev/null 2>&1; then
    ok "playwright present (run 'npx playwright install' if browsers are missing)"
  else
    echo "WARNING: playwright config present but not resolvable -- any e2e gate will fail until 'npx playwright install' runs."
  fi
fi

echo "== preflight PASSED =="
echo "Next: dispatch up to ${CONCURRENCY} Ready ticket(s), with at most ${MAX_HEAVY} simultaneous heavy local checks."
