#!/bin/bash
# Send daily Mimicry heartbeat.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi
export MIMICRY_PYTHON

LOG_FILE="$MIMICRY_HOME/daily_summary.log"

cd "$MIMICRY_HOME"
"$MIMICRY_HOME/run_alerted.sh" "Daily heartbeat ${IBKR_EXECUTION_MODE:-paper}" "$LOG_FILE" \
  timeout 15m "$MIMICRY_PYTHON" daily_summary.py --mode "${IBKR_EXECUTION_MODE:-paper}"
