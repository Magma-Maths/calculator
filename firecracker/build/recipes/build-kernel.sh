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
source "$(dirname "$(readlink -f "$0")")/../kernel-inputs.sh"
if [ "${KERNEL_VERSION:-$kver}" != "$kver" ] || [ "${FIRECRACKER_TAG:-$fc_tag}" != "$fc_tag" ]; then
    echo "KERNEL_VERSION and FIRECRACKER_TAG must match reviewed kernel-inputs.sh versions" >&2
    exit 1
fi
config_url="https://raw.githubusercontent.com/firecracker-microvm/firecracker/${fc_tag}/resources/guest_configs/microvm-kernel-ci-x86_64-6.1.config"
config="microvm-kernel-ci-x86_64-6.1-${fc_tag}.config"
tarball="linux-${kver}.tar.xz"
src_url="https://cdn.kernel.org/pub/linux/kernel/v6.x/${tarball}"
build=
trap 'rm -f "$work/$tarball.part" "$work/$config.part"; if [ -n "$build" ]; then rm -rf "$build"; fi' EXIT

verified_input() {
    local file=$2
    if [ ! -f "$file" ]; then
        file=$2.part
        curl -fsSL "$1" -o "$file"
    fi
    printf '%s  %s\n' "$3" "$file" | sha256sum --check >&2
    if [ "$file" != "$2" ]; then
        mv "$file" "$2"
    fi
}

mkdir -p "$work"
cd "$work"
verified_input "$src_url" "$tarball" "$kernel_sha256"
verified_input "$config_url" "$config" "$config_sha256"
build=$(mktemp -d "$work/.magma-fc-kernel-source-XXXXXX")
tar -xf "$tarball" -C "$build"
cp "$config" "$build/linux-${kver}/.config"
cd "$build/linux-${kver}"
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
