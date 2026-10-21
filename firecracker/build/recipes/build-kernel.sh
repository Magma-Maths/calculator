#!/bin/bash
# Build the guest kernel from Firecracker's 6.1 CI config.
# Usage: build-kernel.sh <out-vmlinux> [work-dir] [extra-config]
set -euo pipefail

out=${1:?usage: build-kernel.sh <out-vmlinux> [work-dir] [extra-config]}
work=${2:-/var/tmp/magma-fc-kernel}
extra_config=${3:-}
if [ -n "$extra_config" ]; then
    extra_config=$(realpath -e "$extra_config")
fi
kver=${KERNEL_VERSION:-6.1.186}
fc_tag=${FIRECRACKER_TAG:-v1.17.0}
config_url="https://raw.githubusercontent.com/firecracker-microvm/firecracker/${fc_tag}/resources/guest_configs/microvm-kernel-ci-x86_64-6.1.config"
tarball="linux-${kver}.tar.xz"
src_url="https://cdn.kernel.org/pub/linux/kernel/v6.x/${tarball}"
build=
trap 'rm -f "$work/$tarball.part"; if [ -n "$build" ]; then rm -rf "$build"; fi' EXIT

mkdir -p "$work"
cd "$work"
if [ ! -f "$tarball" ]; then
    curl -fsSL -o "$tarball.part" "$src_url" && mv "$tarball.part" "$tarball"
fi
build=$(mktemp -d "$work/.magma-fc-kernel-source-XXXXXX")
tar -xf "$tarball" -C "$build"
cd "$build/linux-${kver}"
curl -fsSL "$config_url" -o .config
if [ -n "$extra_config" ]; then
    scripts/kconfig/merge_config.sh -m .config "$extra_config"
fi
scripts/config --disable CONFIG_IO_URING
scripts/config --set-str CONFIG_LOCALVERSION "-magma-fc"
make olddefconfig
if [ -n "$extra_config" ]; then
    while IFS= read -r option || [ -n "$option" ]; do
        case "$option" in
            CONFIG_*=*|'# CONFIG_'*' is not set')
                grep -Fxq -- "$option" .config || { echo "extra config not applied: $option" >&2; exit 1; }
                ;;
        esac
    done < "$extra_config"
fi
# The guest agent's seccomp filter, and its "log" mode, depend on these.
grep -q '^CONFIG_SECCOMP_FILTER=y' .config
grep -q '^CONFIG_AUDIT=y' .config
make -j"$(nproc)" vmlinux
install -m 0644 vmlinux "$out"
