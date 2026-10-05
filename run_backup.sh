#!/bin/bash
# Consistent SQLite backup, age encryption, and rclone upload.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
BACKUP_STAGING_DIR="${BACKUP_STAGING_DIR:-$MIMICRY_HOME/.backup-staging}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is required}"
: "${BACKUP_RCLONE_REMOTE:?BACKUP_RCLONE_REMOTE is required}"

command -v age >/dev/null
command -v rclone >/dev/null
mkdir -p "$BACKUP_STAGING_DIR"

ARCHIVE="$("$MIMICRY_PYTHON" "$MIMICRY_HOME/backup_state.py" \
  --output-dir "$BACKUP_STAGING_DIR")"
ENCRYPTED="${ARCHIVE}.age"
age -r "$BACKUP_AGE_RECIPIENT" -o "$ENCRYPTED" "$ARCHIVE"
rclone copyto "$ENCRYPTED" \
  "${BACKUP_RCLONE_REMOTE%/}/$(basename "$ENCRYPTED")"
rm -f "$ARCHIVE" "$ENCRYPTED"
echo "Encrypted backup uploaded successfully"
