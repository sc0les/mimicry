#!/bin/bash
# Alert if expected Mimicry cron jobs have not completed recently.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi
export MIMICRY_PYTHON

LOG_FILE="$MIMICRY_HOME/deadman.log"

cd "$MIMICRY_HOME"
"$MIMICRY_HOME/run_alerted.sh" "Deadman check ${IBKR_EXECUTION_MODE:-paper}" "$LOG_FILE" \
  timeout 5m "$MIMICRY_PYTHON" deadman_check.py \
  --mode "${IBKR_EXECUTION_MODE:-paper}" --max-age-minutes 45 --force --no-alert
