import re
import shutil
import subprocess
from pathlib import Path

import pytest

# mkdir, mount and chmod handle these themselves inside any Debian minbase tree.
STANDARD_MINBASE_DIRS = {"/proc", "/sys", "/tmp", "/run"}

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    ROOT / "firecracker/guest/init.sh",
    ROOT / "firecracker/build/build-kernel.sh",
    ROOT / "firecracker/build/build-rootfs.sh",
    ROOT / "firecracker/build/build-magma-image.sh",
    ROOT / "firecracker/host/magma-fc-launch",
    ROOT / "firecracker/host/fc-debug-boot.sh",
]


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_parses(script):
    assert script.exists(), script
    assert script.stat().st_mode & 0o111, f"{script} is not executable"
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_shellcheck(script):
    subprocess.run(["shellcheck", "-S", "warning", str(script)], check=True)


def test_init_uses_magma_mac_and_dummy():
    text = (ROOT / "firecracker/guest/init.sh").read_text()
    assert "magma.mac=" in text
    assert "type dummy" in text
    assert "sysrq-trigger" in text


def test_init_mount_targets_exist_in_rootfs_or_are_standard():
    """Every directory init.sh mounts onto must exist in the built rootfs.

    The root disk is read-only under Firecracker, so init.sh cannot mkdir a
    mount point at boot; build-rootfs.sh must create it ahead of time, unless
    mmdebstrap's minbase variant already provides it.
    """
    init_text = (ROOT / "firecracker/guest/init.sh").read_text()
    mount_targets = []
    for line in init_text.splitlines():
        line = line.strip()
        if not line.startswith("mount "):
            continue
        mount_targets.append(line.split("||", 1)[0].split()[-1])
    assert mount_targets == ["/proc", "/sys", "/tmp", "/run", "/opt/magma/current"]

    rootfs_text = (ROOT / "firecracker/build/build-rootfs.sh").read_text()
    install_line = next(line for line in rootfs_text.splitlines() if line.startswith("install -d"))
    created_dirs = set(re.findall(r'"\$root([^"]*)"', install_line))

    for target in mount_targets:
        assert target in STANDARD_MINBASE_DIRS or target in created_dirs, target
