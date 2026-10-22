#!/bin/bash
# Build only when inputs change; --force refreshes build dependencies.
# Usage: build-rootfs.sh [--force] <calculator-src-dir> <out-image> [site-hook]
# Needs root for mmdebstrap chroot steps.
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/freshness.py" rootfs "$@"
