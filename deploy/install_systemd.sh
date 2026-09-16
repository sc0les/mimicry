#!/bin/bash
# Install Pi services after a successful paper-mode preflight and deployment.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-/opt/mimicry}"
SYSTEMD_SOURCE="$MIMICRY_HOME/deploy/systemd"
JOURNALD_SOURCE="$MIMICRY_HOME/deploy/journald/mimicry.conf"

set -a
# shellcheck disable=SC1091
. "$MIMICRY_HOME/secrets.env"
set +a
: "${IBKR_EXECUTION_MODE:?IBKR_EXECUTION_MODE must be paper for validation or live after cutover}"
: "${IBKR_ACCOUNT_ID:?IBKR_ACCOUNT_ID is required}"
: "${PUSHOVER_APP_TOKEN:?PUSHOVER_APP_TOKEN is required}"
: "${PUSHOVER_USER_KEY:?PUSHOVER_USER_KEY is required}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is required}"
: "${BACKUP_RCLONE_REMOTE:?BACKUP_RCLONE_REMOTE is required}"

sudo apt-get update
sudo apt-get install -y age rclone fail2ban unattended-upgrades
sudo install -m 0644 "$SYSTEMD_SOURCE"/*.service "$SYSTEMD_SOURCE"/*.timer \
  /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/journald.conf.d
sudo install -m 0644 "$JOURNALD_SOURCE" \
  /etc/systemd/journald.conf.d/mimicry.conf

crontab -l > "$HOME/crontab.pre-systemd" 2>/dev/null || true
if crontab -l >/tmp/mimicry-cron-current 2>/dev/null; then
  grep -Ev \
    'run_(mirror_ibkr|ibkr_gateway|reconcile|deadman|daily_summary|backup)' \
    /tmp/mimicry-cron-current >/tmp/mimicry-cron-kept || true
  crontab /tmp/mimicry-cron-kept
fi

sudo systemctl daemon-reload
sudo systemctl restart systemd-journald
sudo systemctl enable --now mimicry-gateway.service
sudo systemctl enable --now \
  mimicry-mirror.timer \
  mimicry-reconcile.timer \
  mimicry-heartbeat.timer \
  mimicry-deadman.timer \
  mimicry-alert-self-test.timer \
  mimicry-backup.timer
systemctl --no-pager --full status mimicry-gateway.service
systemctl --no-pager list-timers 'mimicry-*'
