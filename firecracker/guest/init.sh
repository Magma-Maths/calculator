#!/bin/bash
# Guest PID 1. Boots straight into one Magma job and powers off.
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

log() { echo "init: $*" > /dev/kmsg 2>/dev/null || echo "init: $*"; }

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs -o size=64m,nosuid,nodev,noexec,mode=1777 tmpfs /tmp
mount -t tmpfs -o size=8m,nosuid,nodev,noexec tmpfs /run
mount -o ro /dev/vdb /opt/magma/current || log "failed to mount /dev/vdb"

if [ -x /etc/magma-fc/site-init ]; then
    if ! /etc/magma-fc/site-init; then
        log "site hook failed"
        sync
        echo b > /proc/sysrq-trigger
        exit 1
    fi
fi

hostname magma-worker
export PYTHONPATH=/opt/calculator-guest
export AGENT_RUN_AS_UID=1000
python3 -m firecracker.guest.agent
log "agent exited with $?"

sync
echo b > /proc/sysrq-trigger
