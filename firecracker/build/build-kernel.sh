#!/bin/bash
# Build only when inputs change; --force refreshes build dependencies.
# Usage: build-kernel.sh [--force] <out-vmlinux> [work-dir] [extra-config]
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/freshness.py" kernel "$@"
