#!/usr/bin/env bash
set -euo pipefail

blocked() {
  echo "BLOCKED: $*" >&2
  exit 1
}

(( EUID == 0 )) || blocked 'loading AppArmor requires host root'
command -v apparmor_parser >/dev/null || blocked 'install the host apparmor package first'
[[ -r /sys/kernel/security/apparmor/profiles ]] || blocked 'host AppArmor policy is unavailable'
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
profile="$repo_root/security/apparmor/magma-calculator"

apparmor_parser -QK "$profile" || blocked 'calculator AppArmor profile does not compile'
apparmor_parser -r -K "$profile" || blocked 'calculator AppArmor profile could not be loaded'
grep -Fx 'magma-calculator (enforce)' /sys/kernel/security/apparmor/profiles \
  || blocked 'calculator AppArmor profile is not enforced'

docker info --format '{{json .SecurityOptions}}' | python3 -c '
import json, sys
if not any(option == "name=apparmor" or option.startswith("name=apparmor,") for option in json.load(sys.stdin)):
    raise SystemExit("BLOCKED: Docker does not report AppArmor support")
'
