#!/bin/bash
# Read-only production-host checks to run before deploying or enabling timers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/secrets.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/secrets.env"
  set +a
fi

MODE="${IBKR_EXECUTION_MODE:-paper}"
PORT="${IBKR_GATEWAY_PORT:-}"
EXPECTED_TZ="${MIMICRY_TIMEZONE:-America/New_York}"
FAILED=0

check() {
  local label="$1"
  shift
  if "$@"; then
    printf 'OK   %s\n' "$label"
  else
    printf 'FAIL %s\n' "$label"
    FAILED=1
  fi
}

check "root filesystem is mounted read-write" sh -c \
  "findmnt -no OPTIONS / | tr ',' '\n' | grep -qx rw"
check "system clock synchronized" sh -c \
  "timedatectl show -p NTPSynchronized --value | grep -qx yes"
check "timezone is $EXPECTED_TZ" test "$(timedatectl show -p Timezone --value)" = "$EXPECTED_TZ"
check "at least 5 GB disk free" test "$(df -Pk / | awk 'NR==2 {print $4}')" -ge 5242880
check "IBKR_ACCOUNT_ID is configured" test -n "${IBKR_ACCOUNT_ID:-}"
check "execution mode is paper or live" sh -c '[ "$1" = paper ] || [ "$1" = live ]' _ "$MODE"
check "Python environment exists" test -x /opt/mimicry-env/bin/python3
check "IBC directory exists" test -d "/opt/ibc-$MODE"
check "SSH password login disabled" sh -c \
  "! sshd -T 2>/dev/null | grep -Eq '^passwordauthentication yes$'"

for tool in age rclone; do
  if command -v "$tool" >/dev/null; then
    echo "OK   $tool is installed"
  else
    echo "WARN $tool is not installed; install_systemd.sh will install it before backups start"
  fi
done

if ip -brief link | grep -Eq '^(eth|en)[^ ]*[[:space:]]+UP'; then
  echo "OK   wired Ethernet link is up"
else
  echo "WARN no wired Ethernet link detected; do not enable live execution over Wi-Fi alone"
fi

if [ -n "$PORT" ] && ss -ltn | awk '{print $4}' | grep -Eq "[:.]${PORT}$"; then
  echo "OK   configured Gateway API port $PORT is listening"
else
  echo "WARN configured Gateway API port is not currently listening"
fi

for path in \
  /opt/mimicry/ibkr_execution_ledger.json \
  /opt/mimicry/mimicry_state.sqlite3 \
  /opt/mimicry/browser_state.json \
  /opt/mimicry/figi_cache.json \
  /opt/mimicry/ibkr_contract_cache.json
do
  if [ -e "$path" ]; then
    stat --printf='FOUND %n (%s bytes)\n' "$path"
  fi
done

exit "$FAILED"
