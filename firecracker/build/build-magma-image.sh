#!/bin/bash
# Pack one Magma version tree into a read-only ext4 image for /dev/vdb.
# Usage: build-magma-image.sh <magma-version-dir> <out-image>
set -euo pipefail

tree=${1:?usage: build-magma-image.sh <magma-version-dir> <out-image>}
out=${2:?usage: build-magma-image.sh <magma-version-dir> <out-image>}
trap 'rm -f "$out.tmp"' EXIT
tree=$(readlink -f "$tree")
[ -x "$tree/magma.exe" ] || { echo "no magma.exe under $tree" >&2; exit 1; }
[ -f "$tree/magmapassfile" ] || { echo "no magmapassfile under $tree" >&2; exit 1; }

size_kb=$(( $(du -sk "$tree" | cut -f1) * 11 / 10 + 32768 ))
rm -f "$out.tmp"
mke2fs -q -t ext4 -d "$tree" -L magma -F "$out.tmp" "${size_kb}K"
chmod 0640 "$out.tmp"
mv -f "$out.tmp" "$out"
trap - EXIT
echo "magma image written to $out"
