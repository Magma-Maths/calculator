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

BOOT_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+=[A-Za-z0-9_./,:+@%=-]*")
MAX_EXTRA_BOOT_ARGS = 1024
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
    boot_args: str = ""


def validate_boot_args(boot_args: str) -> None:
    if not isinstance(boot_args, str) or len(boot_args) > MAX_EXTRA_BOOT_ARGS:
        raise ValueError("boot_args must be a string of at most 1024 characters")
    if boot_args and not all(BOOT_TOKEN_RE.fullmatch(token) for token in boot_args.split(" ")):
        raise ValueError("boot_args must contain unquoted key=value tokens separated by single spaces")


def render_config(boot_args: str, mem_mib: int, vcpus: int, seccomp_mode: str = "on") -> dict:
    validate_boot_args(boot_args)
    extra_boot_args = boot_args
    if seccomp_mode not in SECCOMP_MODES:
        raise ValueError(f"seccomp_mode must be one of {SECCOMP_MODES}")
    boot_args = f"{BOOT_ARGS} magma.seccomp={seccomp_mode}"
    if seccomp_mode == "log":
        # SCMP_ACT_LOG events reach the kernel log only through audit. Other
        # modes leave audit off so kauditd does not fill an ordinary console.
        boot_args += " audit=1"
    if extra_boot_args:
        boot_args += f" {extra_boot_args}"
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
        json.dump(render_config(slot.boot_args, mem_mib, vcpus, seccomp_mode), fh, indent=2)
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
