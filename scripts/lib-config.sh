#!/usr/bin/env bash
# lib-config.sh -- resolve per-repo orchestration config for the shell scripts.
#
# The config lives at <repo>/.orchestration/config.yaml. This library reads the
# two shapes the harness relies on, with no external YAML dependency:
#
#   1. flat scalars           key: value
#   2. block lists            key:\n  - item\n  - item
#   3. self_check list-of-map self_check:\n  - name: x\n    run: y
#
# Anything deeper is read semantically by the agents from the YAML, not here.
#
# Usage:
#   . "$(dirname "$0")/lib-config.sh"
#   root="$(orch_project_root)"
#   prod="$(orch_get production_branch)"
#   orch_list ci_checks_integration            # one check name per line
#   orch_selfchecks                            # "name<TAB>run" per line

orch_script_dir() {
  cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

orch_engine() {
  printf '%s/orchestration-engine.py' "$(orch_script_dir)"
}

orch_branch_name() {
  local role="${1:?branch role required}"
  shift || true
  python3 "$(orch_engine)" branch-name "$role" "$@"
}

orch_validate_config() {
  python3 "$(orch_engine)" validate-config >/dev/null
}

orch_assert_minimum_version() {
  local active_version="${1:-}"
  local args=(--plugin-root "$(orch_script_dir)/.." --config "$(orch_config_file)")
  if [ -n "$active_version" ]; then args+=(--active-version "$active_version"); fi
  python3 "$(orch_script_dir)/version_policy.py" "${args[@]}" >/dev/null
}

# --- location -----------------------------------------------------------------

orch_project_root() {
  git rev-parse --show-toplevel 2>/dev/null || printf '%s' "${CLAUDE_PROJECT_DIR:-$PWD}"
}

orch_config_file() {
  printf '%s/.orchestration/config.yaml' "$(orch_project_root)"
}

# --- readers ------------------------------------------------------------------

# orch_get <key> [default] -- a flat top-level scalar. Strips a trailing inline
# comment and one pair of surrounding quotes. Returns the default if absent.
orch_get() {
  local key="$1" def="${2:-}" f v
  f="$(orch_config_file)"
  if [ ! -f "$f" ]; then printf '%s' "$def"; return 0; fi
  v="$(grep -E "^${key}:[[:space:]]" "$f" 2>/dev/null | head -1 \
        | sed -E "s/^${key}:[[:space:]]*//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//; s/^[\"']//; s/[\"']$//")"
  if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "$def"; fi
}

# orch_list <key> -- items of a top-level block list, one per line. Strips a
# trailing inline comment and surrounding quotes on each item. Emits nothing if
# the key is absent or is not a block list.
orch_list() {
  local key="$1" f
  f="$(orch_config_file)"
  [ -f "$f" ] || return 0
  awk -v key="$key" '
    $0 ~ "^" key ":[[:space:]]*(#.*)?$" { inblk=1; next }
    inblk {
      if ($0 ~ /^[[:space:]]*$/)  { next }                 # blank: stay in block
      if ($0 ~ /^[[:space:]]*#/)  { next }                 # comment: stay
      if ($0 ~ /^[[:space:]]+-[[:space:]]+/) {             # a list item
        line = $0
        sub(/^[[:space:]]+-[[:space:]]+/, "", line)
        sub(/[[:space:]]+#.*$/, "", line)                  # trailing comment
        sub(/[[:space:]]+$/, "", line)
        gsub(/^["'\'']|["'\'']$/, "", line)                # surrounding quotes
        print line
        next
      }
      inblk = 0                                            # anything else ends it
    }
  ' "$f"
}

# orch_named <block> <name> <field> -- within a top-level list-of-maps `block:`,
# find the entry whose `name:` equals <name> and print its `<field>:` value.
# Emits nothing if the block, entry, or field is absent. Strips one pair of outer
# quotes (no YAML escape processing), matching orch_selfchecks' `run` handling.
orch_named() {
  local blk="$1" want="$2" fld="$3" f
  f="$(orch_config_file)"
  [ -f "$f" ] || return 0
  awk -v blk="$blk" -v want="$want" -v fld="$fld" '
    function trim(s)   { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    function unquote(s,  n, a, b) {
      n = length(s); if (n < 2) return s
      a = substr(s, 1, 1); b = substr(s, n, 1)
      if ((a == "\"" && b == "\"") || (a == "\x27" && b == "\x27")) return substr(s, 2, n - 2)
      return s
    }
    $0 ~ "^" blk ":[[:space:]]*(#.*)?$" { inblk = 1; cur = ""; next }
    inblk {
      if ($0 ~ /^[^[:space:]]/) { inblk = 0; next }        # dedent to col 0 ends block
      if ($0 ~ /^[[:space:]]+-[[:space:]]+name:/) {
        line = $0; sub(/^[[:space:]]+-[[:space:]]+name:[[:space:]]*/, "", line)
        cur = unquote(trim(line)); next
      }
      if (cur == want && $0 ~ ("^[[:space:]]+" fld ":")) {
        line = $0; sub("^[[:space:]]+" fld ":[[:space:]]*", "", line)
        print unquote(trim(line)); exit
      }
    }
  ' "$f"
}

# orch_selfchecks -- emit one line per self_check entry as: name<TAB>run
# The run value is passed through verbatim except that a single pair of matching
# outer quotes is stripped (no YAML escape processing -- see the template docs).
orch_selfchecks() {
  local f
  f="$(orch_config_file)"
  [ -f "$f" ] || return 0
  awk '
    function trim(s)   { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    function unquote(s,  n, a, b) {
      n = length(s); if (n < 2) return s
      a = substr(s, 1, 1); b = substr(s, n, 1)
      if ((a == "\"" && b == "\"") || (a == "\x27" && b == "\x27")) return substr(s, 2, n - 2)
      return s
    }
    $0 ~ /^self_check:[[:space:]]*(#.*)?$/ { inblk = 1; next }
    inblk {
      if ($0 ~ /^[^[:space:]]/) { inblk = 0; next }        # dedent to col 0 ends block
      if ($0 ~ /^[[:space:]]+-[[:space:]]+name:/) {        # new entry
        if (have) print name "\t" run
        line = $0; sub(/^[[:space:]]+-[[:space:]]+name:[[:space:]]*/, "", line)
        name = trim(line); run = ""; have = 1; next
      }
      if ($0 ~ /^[[:space:]]+run:/) {
        line = $0; sub(/^[[:space:]]+run:[[:space:]]*/, "", line)
        run = unquote(trim(line)); next
      }
    }
    END { if (have) print name "\t" run }
  ' "$f"
}
