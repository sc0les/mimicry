#!/bin/bash
# Deploy the current local commit directly to the Pi without using a remote.

set -euo pipefail

HOST="${1:?Usage: deploy_pi.sh user@host (existing /opt/mimicry installation)}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
ARCHIVE="$(mktemp -t mimicry-deploy)"
trap 'rm -f "$ARCHIVE"' EXIT

git -C "$ROOT" archive --format=tar.gz -o "$ARCHIVE" "$COMMIT"
scp "$ROOT/pi_preflight.sh" "$HOST:/tmp/mimicry-preflight.sh"
ssh "$HOST" "test -d /opt/mimicry && \
  cp /tmp/mimicry-preflight.sh /opt/mimicry/pi_preflight.sh && \
  chmod +x /opt/mimicry/pi_preflight.sh && \
  /opt/mimicry/pi_preflight.sh"
scp "$ARCHIVE" "$HOST:/tmp/mimicry-$COMMIT.tar.gz"
ssh "$HOST" "set -e; \
  stamp=\$(date -u +%Y%m%dT%H%M%SZ); \
  mkdir -p /opt/mimicry-deploy-backups; \
  tar -czf /opt/mimicry-deploy-backups/predeploy-\$stamp.tar.gz \
    -C /opt mimicry; \
  tar -xzf /tmp/mimicry-$COMMIT.tar.gz -C /opt/mimicry; \
  rm -f /tmp/mimicry-$COMMIT.tar.gz; \
  /opt/mimicry-env/bin/pip install \
    -r /opt/mimicry/requirements.txt; \
  /opt/mimicry-env/bin/python3 -m py_compile \
    /opt/mimicry/*.py; \
  chmod +x /opt/mimicry/*.sh"
echo "Deployed local commit $COMMIT to $HOST; systemd activation remains a separate step"
