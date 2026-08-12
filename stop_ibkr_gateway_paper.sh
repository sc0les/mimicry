#!/bin/bash
# Stops the Raspberry Pi IB Gateway paper session.

set -euo pipefail

PATTERN='ibcalpha.ibc.IbcGateway|install4j.ibgateway.GWClient|/opt/Jts/ibgateway|ibcstart.sh 1045 -g|displaybannerandlaunch.sh|xvfb-run -a ./gatewaystart-paper.sh|Xvfb :99'

pkill -TERM -f "$PATTERN" || true
sleep 5
pkill -KILL -f "$PATTERN" || true
