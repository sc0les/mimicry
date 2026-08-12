#!/bin/bash
# Dry-run IBKR wrapper for cron. Connects to IBKR for preview, but does not submit orders.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi
export MIMICRY_PYTHON

LOCK_FILE=/tmp/mimicry-ibkr-dryrun.lock
LOG_FILE="$MIMICRY_HOME/mirror_ibkr.log"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IBKR dry-run already running; skipping this cron slot" >> "$LOG_FILE"
  exit 0
fi

cd "$MIMICRY_HOME"
IBKR_CLIENT_ID=12 "$MIMICRY_HOME/run_alerted.sh" "IBKR dry run" "$LOG_FILE" timeout 25m "$MIMICRY_PYTHON" mirror_ibkr.py --mode all_equities --preview-orders --portfolio-value-source ibkr --save-state
