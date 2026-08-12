#!/bin/bash
# Starts a single IB Gateway paper session under IBC/Xvfb on the Raspberry Pi.

set -euo pipefail

LOCK_FILE=/tmp/mimicry-ibgateway-paper.lock
LOG_DIR=/opt/ibc-paper/logs
LOG_FILE="$LOG_DIR/gateway.out"
PID_FILE="$LOG_DIR/gateway.wrapper.pid"
READY_TIMEOUT_SECONDS=120
API_PORT=4002

mkdir -p "$LOG_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IB Gateway paper start already running" >> "$LOG_FILE"
  exit 0
fi

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Restarting IB Gateway paper session" >> "$LOG_FILE"

# IBC/Gateway auto-restarts can leave old Java children behind on the Pi.
# Clean them before each managed start so API clients see one Gateway.
pkill -TERM -f 'ibcalpha.ibc.IbcGateway|install4j.ibgateway.GWClient|/opt/Jts/ibgateway|ibcstart.sh 1045 -g|displaybannerandlaunch.sh|xvfb-run -a ./gatewaystart-paper.sh|Xvfb :99' || true
sleep 5
pkill -KILL -f 'ibcalpha.ibc.IbcGateway|install4j.ibgateway.GWClient|/opt/Jts/ibgateway|ibcstart.sh 1045 -g|displaybannerandlaunch.sh|xvfb-run -a ./gatewaystart-paper.sh|Xvfb :99' || true

cd /opt/ibc-paper
# Close the flock fd in the long-lived child; otherwise future cron restarts
# see the stale lock and skip the cleanup/start sequence forever.
(
  exec 9>&-
  exec setsid xvfb-run -a ./gatewaystart-paper.sh -inline
) >> "$LOG_FILE" 2>&1 < /dev/null &
echo "$!" > "$PID_FILE"

for _ in $(seq 1 "$READY_TIMEOUT_SECONDS"); do
  if ss -ltn | awk '{print $4}' | grep -Eq "[:.]${API_PORT}$"; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IB Gateway paper start complete; API port ${API_PORT} is listening" >> "$LOG_FILE"
    exit 0
  fi
  sleep 1
done

if ! pgrep -f 'ibcalpha.ibc.IbcGateway.*config.ini|install4j.ibgateway.GWClient' > /dev/null; then
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IB Gateway paper failed to start" >> "$LOG_FILE"
  tail -120 "$LOG_FILE"
  exit 1
fi

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') IB Gateway paper process is running but API port ${API_PORT} is not listening" >> "$LOG_FILE"
tail -120 "$LOG_FILE"
exit 1
