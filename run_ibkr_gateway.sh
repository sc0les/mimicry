#!/bin/bash
# Foreground IB Gateway/IBC launcher supervised by systemd.

set -euo pipefail

MODE="${IBKR_EXECUTION_MODE:-paper}"
case "$MODE" in
  paper|live) ;;
  *)
    echo "IBKR_EXECUTION_MODE must be paper or live" >&2
    exit 2
    ;;
esac

IBC_HOME="${IBC_HOME:-/opt/ibc-$MODE}"
START_SCRIPT="${IBC_START_SCRIPT:-$IBC_HOME/gatewaystart-$MODE.sh}"
if [ ! -x "$START_SCRIPT" ]; then
  echo "IBC Gateway start script is not executable: $START_SCRIPT" >&2
  exit 1
fi

cd "$IBC_HOME"
exec xvfb-run -a "$START_SCRIPT" -inline
