#!/bin/bash
# Run the exact same mirror and reconciliation profile for paper or live.

set -euo pipefail

MODE="${1:-${IBKR_EXECUTION_MODE:-}}"
case "$MODE" in
  paper|live) ;;
  *)
    echo "usage: run_ibkr_execute_cycle.sh paper|live" >&2
    exit 2
    ;;
esac

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi

cd "$MIMICRY_HOME"
"$MIMICRY_PYTHON" mirror_ibkr.py \
  --execute \
  "--confirm-$MODE" \
  --order-type limit
"$MIMICRY_PYTHON" reconcile.py --mode "$MODE"
