#!/bin/bash
# Locked, alerted IBKR runner used by systemd for either account mode.

set -euo pipefail

MODE="${1:-${IBKR_EXECUTION_MODE:-}}"
case "$MODE" in
  paper|live) ;;
  *)
    echo "usage: run_mirror_ibkr_execute.sh paper|live" >&2
    exit 2
    ;;
esac

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi
export MIMICRY_HOME MIMICRY_PYTHON IBKR_EXECUTION_MODE="$MODE"

exec 9>"/tmp/mimicry-ibkr-execute.lock"
if ! flock -n 9; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IBKR execute already running; skipping slot"
  exit 0
fi

cd "$MIMICRY_HOME"
"$MIMICRY_HOME/run_alerted.sh" \
  "IBKR execute $MODE" \
  - \
  timeout 25m \
  "$MIMICRY_HOME/run_ibkr_execute_cycle.sh" "$MODE"
