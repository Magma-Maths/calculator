#!/bin/bash
# Build the guest root filesystem as an ext4 image from Debian bookworm.
# Usage: build-rootfs.sh <calculator-src-dir> <out-image>
# Needs root (mmdebstrap chroot steps) and: mmdebstrap, e2fsprogs.
set -euo pipefail

src=${1:?usage: build-rootfs.sh <calculator-src-dir> <out-image>}
out=${2:?usage: build-rootfs.sh <calculator-src-dir> <out-image>}
work=$(mktemp -d /var/tmp/magma-fc-rootfs.XXXXXX)
trap 'rm -rf "$work" "$out.tmp"' EXIT
root="$work/root"

mmdebstrap --variant=minbase \
    --include=python3,python3-seccomp,passwd,iproute2,mount,coreutils,util-linux \
    --aptopt='Acquire::Retries "3"' \
    bookworm "$root"

install -d -m 0755 "$root/opt/calculator-guest/firecracker/guest" "$root/opt/magma" "$root/opt/magma/current"
install -m 0755 "$src/firecracker/guest/init.sh" "$root/opt/calculator-guest/init.sh"
install -m 0644 "$src/firecracker/__init__.py" "$root/opt/calculator-guest/firecracker/__init__.py"
install -m 0644 "$src/firecracker/protocol.py" "$root/opt/calculator-guest/firecracker/protocol.py"
install -m 0644 "$src/firecracker/guest/__init__.py" "$root/opt/calculator-guest/firecracker/guest/__init__.py"
install -m 0644 "$src/firecracker/guest/agent.py" "$root/opt/calculator-guest/firecracker/guest/agent.py"
install -m 0644 "$src/firecracker/guest/seccomp_policy.py" "$root/opt/calculator-guest/firecracker/guest/seccomp_policy.py"

chroot "$root" useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin magma
rm -rf "$root/var/cache/apt" "$root/var/lib/apt/lists"

size_kb=$(( $(du -sk "$root" | cut -f1) + 65536 ))
rm -f "$out.tmp"
mke2fs -q -t ext4 -d "$root" -L rootfs -F "$out.tmp" "${size_kb}K"
chmod 0640 "$out.tmp"
mv -f "$out.tmp" "$out"
echo "rootfs written to $out"
