#!/usr/bin/env bash
set -u

echo 'AppArmor profile state:'
if ! sudo -n grep -F magma-calculator /sys/kernel/security/apparmor/profiles; then
  echo 'Calculator profile state is unavailable.'
fi
echo 'Kernel AppArmor denials:'
if kernel_log=$(sudo -n dmesg 2>&1); then
  printf '%s\n' "$kernel_log" | grep -E 'apparmor="DENIED"|apparmor=.*magma-calculator' || true
else
  printf 'dmesg unavailable: %s\n' "$kernel_log"
  sudo -n journalctl -k -b --no-pager --grep='apparmor=' || true
fi
