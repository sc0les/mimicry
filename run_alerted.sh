#!/bin/bash
# Run a command, append output to a log, alert on failure, and write a success marker.

set -uo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: run_alerted.sh NAME LOG_FILE COMMAND [ARGS...]" >&2
  exit 2
fi

NAME="$1"
LOG_FILE="$2"
shift 2

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${MIMICRY_PYTHON:-python3}"
TMP_OUTPUT="$(mktemp)"
SLUG="$(printf '%s' "$NAME" | tr '[:upper:] /' '[:lower:]__' | tr -cd '[:alnum:]_-')"
SUCCESS_FILE="$SCRIPT_DIR/last_success_${SLUG}.json"

STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
"$@" > "$TMP_OUTPUT" 2>&1
RC=$?
FINISHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

{
  echo
  echo "===== $NAME started=$STARTED_AT finished=$FINISHED_AT rc=$RC ====="
  cat "$TMP_OUTPUT"
} >> "$LOG_FILE"

if [ "$RC" -ne 0 ]; then
  {
    echo "$NAME failed with rc=$RC"
    echo "started=$STARTED_AT"
    echo "finished=$FINISHED_AT"
    echo
    tail -80 "$TMP_OUTPUT"
  } | "$PYTHON_BIN" "$SCRIPT_DIR/alerts.py" --title "$NAME failed" --priority 1
  rm -f "$TMP_OUTPUT"
  exit "$RC"
fi

printf '{"name": "%s", "started_at": "%s", "finished_at": "%s"}\n' "$NAME" "$STARTED_AT" "$FINISHED_AT" > "$SUCCESS_FILE"
rm -f "$TMP_OUTPUT"
