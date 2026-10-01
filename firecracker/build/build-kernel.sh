#!/bin/bash
# Build the guest kernel: Firecracker's 6.1 CI config plus the dummy driver.
# Usage: build-kernel.sh <out-vmlinux> [work-dir]
set -euo pipefail

out=${1:?usage: build-kernel.sh <out-vmlinux> [work-dir]}
work=${2:-/var/tmp/magma-fc-kernel}
kver=${KERNEL_VERSION:-6.1.186}
fc_tag=${FIRECRACKER_TAG:-v1.17.0}
config_url="https://raw.githubusercontent.com/firecracker-microvm/firecracker/${fc_tag}/resources/guest_configs/microvm-kernel-ci-x86_64-6.1.config"
tarball="linux-${kver}.tar.xz"
src_url="https://cdn.kernel.org/pub/linux/kernel/v6.x/${tarball}"
trap 'rm -f "$tarball.part"' EXIT

mkdir -p "$work"
cd "$work"
if [ ! -f "$tarball" ]; then
    curl -fsSL -o "$tarball.part" "$src_url" && mv "$tarball.part" "$tarball"
fi
[ -d "linux-${kver}" ] || tar -xf "$tarball"
cd "linux-${kver}"
curl -fsSL "$config_url" -o .config
scripts/config --enable CONFIG_DUMMY
scripts/config --disable CONFIG_IO_URING
scripts/config --set-str CONFIG_LOCALVERSION "-magma-fc"
make olddefconfig
grep -q '^CONFIG_DUMMY=y' .config
# The guest agent's seccomp filter, and its "log" mode, depend on these.
grep -q '^CONFIG_SECCOMP_FILTER=y' .config
grep -q '^CONFIG_AUDIT=y' .config
make -j"$(nproc)" vmlinux
install -m 0644 vmlinux "$out"
echo "kernel written to $out"
