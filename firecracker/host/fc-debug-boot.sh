#!/bin/bash
# Boot one guest interactively with a serial console, no jailer, no vsock.
# Usage: fc-debug-boot.sh <vmlinux> <rootfs.ext4> <magma.ext4> [boot-args] [mem_mib]
# Set BOOT_ARGS=init=/bin/bash to enter a guest shell.
set -euo pipefail

kernel=${1:?}; rootfs=${2:?}; magma=${3:?}; extra=${4:-}; mem=${5:-1024}
fc=${FIRECRACKER:-/opt/magma-fc/v1.17.0/firecracker}
src=$(cd "$(dirname "$0")/../.." && pwd)
cfg=$(mktemp)
trap 'rm -f "$cfg"' EXIT
PYTHONPATH="$src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$kernel" "$rootfs" "$magma" "$extra" "$mem" > "$cfg" <<'PY'
import json
import os
import sys

from firecracker.host.jail import validate_boot_args

kernel, rootfs, magma, extra, mem = sys.argv[1:]
validate_boot_args(extra)
boot_args = "console=ttyS0 reboot=k panic=1 pci=off nomodule " + os.environ.get("BOOT_ARGS", "init=/opt/calculator-guest/init.sh")
if extra:
    boot_args += " " + extra
json.dump({
    "boot-source": {"kernel_image_path": kernel, "boot_args": boot_args},
    "drives": [
        {"drive_id": "rootfs", "path_on_host": rootfs, "is_root_device": True, "is_read_only": True},
        {"drive_id": "magma", "path_on_host": magma, "is_root_device": False, "is_read_only": True},
    ],
    "machine-config": {"vcpu_count": 1, "mem_size_mib": int(mem), "smt": False},
}, sys.stdout)
PY
"$fc" --no-api --config-file "$cfg"
