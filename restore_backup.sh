#!/bin/bash
# Decrypt a backup into a new directory for a restore drill.

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: restore_backup.sh BACKUP.tar.gz.age EMPTY_DESTINATION" >&2
  exit 2
fi

BACKUP="$1"
DESTINATION="$2"
if [ -e "$DESTINATION" ] && [ -n "$(find "$DESTINATION" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "destination must be empty: $DESTINATION" >&2
  exit 1
fi

mkdir -p "$DESTINATION"
PLAINTEXT="$(mktemp --suffix=.tar.gz)"
trap 'rm -f "$PLAINTEXT"' EXIT
age --decrypt -o "$PLAINTEXT" "$BACKUP"
tar -xzf "$PLAINTEXT" -C "$DESTINATION"
echo "Backup restored to $DESTINATION"
