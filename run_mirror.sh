#!/bin/bash
# Wrapper for cron — credentials loaded from secrets.env by Python, not here

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi
export MIMICRY_PYTHON

LOCK_FILE=/tmp/mimicry-alpaca.lock
LOG_FILE="$MIMICRY_HOME/mirror.log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Alpaca mirror already running; skipping this cron slot" >> "$LOG_FILE"
  exit 0
fi

cd "$MIMICRY_HOME"
"$MIMICRY_HOME/run_alerted.sh" "Alpaca mirror" "$LOG_FILE" timeout 25m "$MIMICRY_PYTHON" mirror.py
