#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$ROOT/scripts/durable_liveness.py" \
  "$ROOT/contracts/durable-liveness-v1.json" \
  --fixtures "$ROOT/tests/fixtures/durable-liveness-v1.json" \
  --root "$ROOT"
python3 "$ROOT/tests/durable_liveness_test.py"
