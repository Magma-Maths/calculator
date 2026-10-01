#!/bin/bash
# Boot one guest interactively with a serial console, no jailer, no vsock.
# Usage: fc-debug-boot.sh <vmlinux> <rootfs.ext4> <magma.ext4> <mac> [mem_mib]
# Inside the guest, init runs the agent which waits on vsock; press Ctrl-A x
# to quit. To get a shell instead, add init=/bin/bash to BOOT_ARGS below.
set -euo pipefail

kernel=${1:?}; rootfs=${2:?}; magma=${3:?}; mac=${4:?}; mem=${5:-1024}
fc=${FIRECRACKER:-/opt/magma-fc/v1.17.0/firecracker}
cfg=$(mktemp)
trap 'rm -f "$cfg"' EXIT
cat > "$cfg" <<EOF
{
  "boot-source": {
    "kernel_image_path": "$kernel",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off nomodule ${BOOT_ARGS:-init=/opt/calculator-guest/init.sh} magma.mac=$mac"
  },
  "drives": [
    {"drive_id": "rootfs", "path_on_host": "$rootfs", "is_root_device": true, "is_read_only": true},
    {"drive_id": "magma", "path_on_host": "$magma", "is_root_device": false, "is_read_only": true}
  ],
  "machine-config": {"vcpu_count": 1, "mem_size_mib": $mem, "smt": false},
  "network-interfaces": []
}
EOF
exec "$fc" --no-api --config-file "$cfg"
