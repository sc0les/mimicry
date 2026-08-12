#!/bin/bash
# Executes IBKR paper orders from cron. Paper uses full target deltas by default.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi
export MIMICRY_PYTHON

LOCK_FILE=/tmp/mimicry-ibkr-execute.lock
LOG_FILE="$MIMICRY_HOME/mirror_ibkr_execute.log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IBKR execute already running; skipping this cron slot" >> "$LOG_FILE"
  exit 0
fi

cd "$MIMICRY_HOME"
IBKR_CLIENT_ID=13 "$MIMICRY_HOME/run_alerted.sh" "IBKR execute paper" "$LOG_FILE" timeout 25m "$MIMICRY_HOME/run_ibkr_execute_paper_cycle.sh"
