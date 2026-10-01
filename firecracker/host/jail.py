"""Per-job jail directory for the Firecracker jailer.

The jailer expects <base>/<exec_file_basename>/<id>/root and chroots into
it. We pre-populate that root with the kernel, the two disk images and the
config.json, then remove the whole <id> tree after the job. Images are
hard-linked, so base and the image store must share one filesystem. The
images keep the ownership they were provisioned with (root:firecracker,
0640); only the slot directory, its root, run/ and config.json are chowned
to the job's uid/gid, so a compromised guest cannot rewrite a shared master
image.
"""
import json
import os
import re
import shutil
from dataclasses import dataclass

from firecracker.guest.seccomp_policy import MODES as SECCOMP_MODES

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
GUEST_CID = 3
UDS_PATH = "/run/vsock.sock"
BOOT_ARGS = "reboot=k panic=1 pci=off nomodule 8250.nr_uarts=0 i8042.noaux i8042.nomux i8042.dumbkbd init=/opt/calculator-guest/init.sh"


@dataclass(frozen=True)
class Images:
    kernel: str
    rootfs: str
    magma: str


@dataclass(frozen=True)
class Slot:
    name: str
    mac: str


def render_config(mac: str, mem_mib: int, vcpus: int, seccomp_mode: str = "on") -> dict:
    if not MAC_RE.match(mac):
        raise ValueError("mac must be six lowercase hex pairs separated by colons")
    if seccomp_mode not in SECCOMP_MODES:
        raise ValueError(f"seccomp_mode must be one of {SECCOMP_MODES}")
    boot_args = f"{BOOT_ARGS} magma.mac={mac} magma.seccomp={seccomp_mode}"
    if seccomp_mode == "log":
        # SCMP_ACT_LOG events reach the kernel log only through audit. Other
        # modes leave audit off so kauditd does not fill an ordinary console.
        boot_args += " audit=1"
    return {
        "boot-source": {
            "kernel_image_path": "/kernel.bin",
            "boot_args": boot_args,
        },
        "drives": [
            {"drive_id": "rootfs", "path_on_host": "/rootfs.ext4", "is_root_device": True, "is_read_only": True, "io_engine": "Sync"},
            {"drive_id": "magma", "path_on_host": "/magma.ext4", "is_root_device": False, "is_read_only": True, "io_engine": "Sync"},
        ],
        "machine-config": {"vcpu_count": vcpus, "mem_size_mib": mem_mib, "smt": False},
        "network-interfaces": [],
        "vsock": {"guest_cid": GUEST_CID, "uds_path": UDS_PATH},
    }


def jail_root(base: str, slot: str) -> str:
    return os.path.join(base, "firecracker", slot, "root")


def _slot_dir(base: str, slot: str) -> str:
    return os.path.join(base, "firecracker", slot)


def stage(
    base: str, slot: Slot, images: Images, mem_mib: int, vcpus: int, uid: int, gid: int, seccomp_mode: str = "on"
) -> str:
    cleanup(base, slot.name)
    root = jail_root(base, slot.name)
    run_dir = os.path.join(root, "run")
    os.makedirs(run_dir, mode=0o750)
    for src, name in ((images.kernel, "kernel.bin"), (images.rootfs, "rootfs.ext4"), (images.magma, "magma.ext4")):
        os.link(src, os.path.join(root, name))
    config_path = os.path.join(root, "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(render_config(slot.mac, mem_mib, vcpus, seccomp_mode), fh, indent=2)
    # The hard-linked images are left root:firecracker so the jailed uid can
    # read but never rewrite a shared master image.
    os.chown(_slot_dir(base, slot.name), uid, gid)
    os.chown(root, uid, gid)
    os.chown(run_dir, uid, gid)
    os.chown(config_path, uid, gid)
    return root


def cleanup(base: str, slot_name: str) -> None:
    path = _slot_dir(base, slot_name)
    if os.path.lexists(path):
        shutil.rmtree(path)
