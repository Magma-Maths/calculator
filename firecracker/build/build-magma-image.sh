#!/bin/bash
# Build only when inputs change; --force refreshes build dependencies.
# Usage: build-magma-image.sh [--force] <magma-version-dir> <out-image>
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/freshness.py" magma "$@"
