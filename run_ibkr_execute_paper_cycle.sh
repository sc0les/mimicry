#!/bin/bash
# Execute IBKR paper mirror, then reconcile the result.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MIMICRY_PYTHON="${MIMICRY_PYTHON:-$MIMICRY_HOME/../mimicry-env/bin/python3}"
if [ ! -x "$MIMICRY_PYTHON" ]; then
  MIMICRY_PYTHON="$(command -v python3)"
fi

cd "$MIMICRY_HOME"
"$MIMICRY_PYTHON" mirror_ibkr.py --mode all_equities --execute --confirm-paper --cancel-before-execute --order-type market --portfolio-value-source ibkr --save-state
"$MIMICRY_PYTHON" reconcile.py
