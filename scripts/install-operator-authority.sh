#!/usr/bin/env bash
# Install the root-owned capability authority and a narrow runtime sudo policy.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo: sudo $0 [runtime-user]" >&2
  exit 2
fi

RUNTIME_USER="${1:-orchestrator}"
case "$RUNTIME_USER" in
  *[!A-Za-z0-9_.-]*|'')
    echo "runtime user contains unsupported characters" >&2
    exit 2
    ;;
esac
id "$RUNTIME_USER" >/dev/null 2>&1 || {
  echo "unknown runtime user: $RUNTIME_USER" >&2
  exit 2
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$HERE/../host-tools/orchestration-recovery-authority.py"
TARGET="/usr/local/libexec/orchestration-recovery-authority"
STATE="/var/lib/orka-authority"
SUDOERS="/etc/sudoers.d/orka-authority"

for path in "$STATE" "$STATE/pending" "$STATE/active" "$STATE/consumed" "$STATE/policies"; do
  if [ -L "$path" ]; then
    echo "refusing symlinked authority path: $path" >&2
    exit 2
  fi
done

install -d -o root -g root -m 0755 /usr/local/libexec
install -d -o root -g root -m 0700 "$STATE" "$STATE/pending" "$STATE/active" "$STATE/consumed" "$STATE/policies"
install -o root -g root -m 0755 "$SOURCE" "$TARGET"

temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT
{
  printf 'Cmnd_Alias ORCHESTRATION_AUTHORITY_RUNTIME = '
  printf '%s consume-recovery --scope *, ' "$TARGET"
  printf '%s activate-review-repair --scope *, ' "$TARGET"
  printf '%s review-repair-grant --scope *, ' "$TARGET"
  printf '%s activate-budget --scope *, ' "$TARGET"
  printf '%s budget-ceiling --scope *, ' "$TARGET"
  printf '%s budget-policy --scope *, ' "$TARGET"
  printf '%s activate-relaunch --scope *, ' "$TARGET"
  printf '%s activate-restart --scope *, ' "$TARGET"
  printf '%s restart-grant --scope *, ' "$TARGET"
  printf '%s relaunch-ceiling --scope *\n' "$TARGET"
  printf '%s ALL=(root) NOPASSWD: ORCHESTRATION_AUTHORITY_RUNTIME\n' "$RUNTIME_USER"
} > "$temporary"
chmod 0440 "$temporary"
visudo -cf "$temporary" >/dev/null
install -o root -g root -m 0440 "$temporary" "$SUDOERS"

echo "installed $TARGET for runtime user $RUNTIME_USER"
