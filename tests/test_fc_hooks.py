import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parent.parent


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\nset -e\n" + text)
    path.chmod(0o755)
    return path


@pytest.fixture
def tools(tmp_path):
    directory = tmp_path / "bin"
    directory.mkdir()
    return directory, {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"}


def test_rootfs_installs_executable_site_hook(tmp_path, tools):
    directory, env = tools
    executable(directory / "mktemp", 'mkdir -p "$BUILD_ROOT"; echo "$BUILD_ROOT"\n')
    executable(directory / "mmdebstrap", 'mkdir -p "${@: -1}/var/cache/apt" "${@: -1}/var/lib/apt/lists"\n')
    executable(directory / "chroot", 'exit 0\n')
    executable(directory / "mke2fs", 'tar -cf "${@: -2:1}" -C "$5" .\n')
    env["BUILD_ROOT"] = str(tmp_path / "build")
    hook = executable(tmp_path / "site-init", 'echo setup\n')
    out = tmp_path / "rootfs.ext4"
    subprocess.run([str(ROOT / "firecracker/build/build-rootfs.sh"), str(ROOT), str(out), str(hook)], env=env, check=True)
    with tarfile.open(out) as archive:
        member = archive.getmember("./etc/magma-fc/site-init")
        assert member.mode == 0o755
        assert archive.extractfile(member).read() == hook.read_bytes()


@pytest.mark.parametrize("hook_status", [None, 0, 7])
def test_init_runs_site_hook_before_agent(tmp_path, hook_status):
    # A chroot keeps PID 1's absolute paths and redirections away from the host.
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    if prefix and (not shutil.which("sudo") or subprocess.run(prefix + ["true"], capture_output=True).returncode):
        pytest.skip("chroot requires root or passwordless sudo")
    root = tmp_path / "guest"
    for name in ("bin", "dev", "proc", "etc/magma-fc", "usr/bin"):
        (root / name).mkdir(parents=True)
    shell = Path(shutil.which("bash")).resolve()
    shutil.copy2(shell, root / "bin/bash")
    libraries = subprocess.check_output(["ldd", str(shell)], text=True)
    for word in libraries.split():
        if word.startswith("/"):
            dest = root / word.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(word, dest)
    (root / "dev/null").touch()
    (root / "proc/cmdline").write_text("site.slot=one\n")
    (root / "proc/sysrq-trigger").touch()
    for command in ("mount", "hostname", "sync", "cat"):
        executable(root / "bin" / command, ':\n')
    executable(root / "usr/bin/python3", 'echo agent >> /events\n')
    if hook_status is not None:
        executable(root / "etc/magma-fc/site-init", f'echo hook >> /events\nexit {hook_status}\n')
    shutil.copy2(ROOT / "firecracker/guest/init.sh", root / "init.sh")
    try:
        result = subprocess.run(prefix + ["chroot", str(root), "/bin/bash", "/init.sh"], capture_output=True, text=True)
        events = (root / "events").read_text().splitlines()
        assert events == (["agent"] if hook_status is None else ["hook", "agent"] if hook_status == 0 else ["hook"])
        if hook_status:
            assert result.returncode != 0
    finally:
        subprocess.run(prefix + ["chown", "-R", f"{os.getuid()}:{os.getgid()}", str(root)], check=True)


@pytest.mark.parametrize("extra, applied", [(False, True), (True, True), (True, False)])
def test_kernel_applies_optional_fragment(tmp_path, tools, extra, applied):
    directory, env = tools
    work = tmp_path / "build"
    source = work / "linux-6.1.186"
    source.mkdir(parents=True)
    (work / "linux-6.1.186.tar.xz").touch()
    executable(directory / "curl", 'echo "CONFIG_SECCOMP_FILTER=y" > .config\necho "CONFIG_AUDIT=y" >> .config\n')
    executable(source / "scripts/config", 'if [ "$1" = --enable ]; then echo "$2=y" >> .config; fi\n')
    executable(source / "scripts/kconfig/merge_config.sh", 'test "$1" = -m\ncat "$3" >> "$2"\n')
    executable(directory / "make", 'if [ "${DROP_FEATURE:-0}" = 1 ]; then sed -i "/CONFIG_SITE_FEATURE/d" .config; fi\nif [ "$1" != olddefconfig ]; then cp .config vmlinux; fi\n')
    env["DROP_FEATURE"] = "0" if applied else "1"
    fragment = tmp_path / "extra.config"
    fragment.write_text("CONFIG_SITE_FEATURE=y\n")
    out = tmp_path / "vmlinux"
    args = [str(ROOT / "firecracker/build/build-kernel.sh"), str(out), str(work)]
    if extra:
        args.append("extra.config")
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True)
    if not applied:
        assert result.returncode != 0
        assert not out.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert ("CONFIG_SITE_FEATURE=y" in out.read_text()) == extra


def test_debug_boot_accepts_generic_args_and_escapes_paths(tmp_path, tools):
    directory, env = tools
    executable(directory / "firecracker", 'cp "$3" "$CAPTURE_CONFIG"\n')
    capture = tmp_path / "captured.json"
    env.update(FIRECRACKER=str(directory / "firecracker"), CAPTURE_CONFIG=str(capture), TMPDIR=str(tmp_path))
    result = subprocess.run([
        str(ROOT / "firecracker/host/fc-debug-boot.sh"), '/images/kernel"one',
        "/images/rootfs", "/images/magma", "site.slot=one site.mode=fast", "512",
    ], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    config = json.loads(capture.read_text())
    assert config["boot-source"]["kernel_image_path"] == '/images/kernel"one'
    assert config["boot-source"]["boot_args"].endswith("site.slot=one site.mode=fast")
    assert config["machine-config"]["mem_size_mib"] == 512
    capture.unlink()
    result = subprocess.run([
        str(ROOT / "firecracker/host/fc-debug-boot.sh"), "kernel", "rootfs", "magma", "site.slot=one\ninit=/bin/sh",
    ], env=env, capture_output=True)
    assert result.returncode != 0
    assert not capture.exists()
