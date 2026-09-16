#!/bin/bash

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
exec "$MIMICRY_HOME/run_mirror_ibkr_execute.sh" live
