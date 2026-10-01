import json
import os

import pytest

from firecracker.host import jail


@pytest.fixture
def images(tmp_path):
    k = tmp_path / "vmlinux"; k.write_bytes(b"K")
    r = tmp_path / "rootfs.ext4"; r.write_bytes(b"R")
    m = tmp_path / "magma.ext4"; m.write_bytes(b"M")
    return jail.Images(kernel=str(k), rootfs=str(r), magma=str(m))


def test_render_config_shape():
    cfg = jail.render_config(mac="02:00:00:00:00:01", mem_mib=1024, vcpus=1)
    assert cfg["network-interfaces"] == []
    assert "cpu_template" not in cfg["machine-config"]
    assert cfg["machine-config"] == {"vcpu_count": 1, "mem_size_mib": 1024, "smt": False}
    assert cfg["vsock"] == {"guest_cid": 3, "uds_path": "/run/vsock.sock"}
    drives = {d["drive_id"]: d for d in cfg["drives"]}
    assert drives["rootfs"]["is_root_device"] and drives["rootfs"]["is_read_only"]
    assert drives["magma"]["path_on_host"] == "/magma.ext4" and drives["magma"]["is_read_only"]
    args = cfg["boot-source"]["boot_args"]
    assert "magma.mac=02:00:00:00:00:01" in args
    assert "init=/opt/calculator-guest/init.sh" in args
    assert "console=" not in args


def test_render_config_rejects_bad_mac():
    with pytest.raises(ValueError):
        jail.render_config(mac="not-a-mac", mem_mib=512, vcpus=1)


def test_stage_creates_jail(tmp_path, images):
    base = str(tmp_path / "jails")
    root = jail.stage(base, jail.Slot("slot1", "02:00:00:00:00:01"), images, 1024, 1, os.getuid(), os.getgid())
    assert root == f"{base}/firecracker/slot1/root"
    assert (tmp_path / "jails/firecracker/slot1/root/kernel.bin").read_bytes() == b"K"
    assert (tmp_path / "jails/firecracker/slot1/root/rootfs.ext4").read_bytes() == b"R"
    assert (tmp_path / "jails/firecracker/slot1/root/magma.ext4").read_bytes() == b"M"
    assert (tmp_path / "jails/firecracker/slot1/root/run").is_dir()
    cfg = json.loads((tmp_path / "jails/firecracker/slot1/root/config.json").read_text())
    assert cfg["boot-source"]["kernel_image_path"] == "/kernel.bin"


def test_stage_does_not_chown_the_images(tmp_path, images, monkeypatch):
    base = str(tmp_path / "jails")
    chowned = []
    monkeypatch.setattr(jail.os, "chown", lambda path, uid, gid, **kw: chowned.append(path))

    root = jail.stage(base, jail.Slot("slot1", "02:00:00:00:00:01"), images, 1024, 1, os.getuid(), os.getgid())

    for name in ("kernel.bin", "rootfs.ext4", "magma.ext4"):
        assert os.path.join(root, name) not in chowned
    assert os.path.join(root, "config.json") in chowned
    assert os.path.join(root, "run") in chowned
    assert root in chowned
    assert os.path.join(base, "firecracker", "slot1") in chowned


def test_stage_replaces_leftover(tmp_path, images):
    base = str(tmp_path / "jails")
    stale = tmp_path / "jails/firecracker/slot1/root"
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("old")
    jail.stage(base, jail.Slot("slot1", "02:00:00:00:00:01"), images, 1024, 1, os.getuid(), os.getgid())
    assert not (stale / "leftover").exists()


def test_cleanup_does_not_follow_symlinks(tmp_path, images):
    base = str(tmp_path / "jails")
    jail.stage(base, jail.Slot("slot1", "02:00:00:00:00:01"), images, 1024, 1, os.getuid(), os.getgid())
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "keep").write_text("k")
    os.symlink(str(outside), str(tmp_path / "jails/firecracker/slot1/root/escape"))
    jail.cleanup(base, "slot1")
    assert not (tmp_path / "jails/firecracker/slot1").exists()
    assert (outside / "keep").exists()


def test_cleanup_missing_is_noop(tmp_path):
    jail.cleanup(str(tmp_path / "nothing"), "slot9")
