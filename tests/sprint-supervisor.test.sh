#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$HERE/sprint_supervisor_test.py"
python3 "$HERE/supervisor_dispatch_test.py"
