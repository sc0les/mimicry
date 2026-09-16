#!/bin/bash
# Compatibility wrapper for the unified live-like paper cycle.

set -euo pipefail

MIMICRY_HOME="${MIMICRY_HOME:-$(cd "$(dirname "$0")" && pwd)}"
exec "$MIMICRY_HOME/run_ibkr_execute_cycle.sh" paper
