import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from .test_fc_hooks import executable, tools

# mkdir, mount and chmod handle these themselves inside any Debian minbase tree.
STANDARD_MINBASE_DIRS = {"/proc", "/sys", "/tmp", "/run"}

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    ROOT / "firecracker/guest/init.sh",
    ROOT / "firecracker/build/build-kernel.sh",
    ROOT / "firecracker/build/build-rootfs.sh",
    ROOT / "firecracker/build/build-magma-image.sh",
    ROOT / "firecracker/build/recipes/build-kernel.sh",
    ROOT / "firecracker/build/recipes/build-rootfs.sh",
    ROOT / "firecracker/build/recipes/build-magma-image.sh",
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

    rootfs_text = (ROOT / "firecracker/build/recipes/build-rootfs.sh").read_text()
    install_line = next(line for line in rootfs_text.splitlines() if line.startswith("install -d"))
    created_dirs = set(re.findall(r'"\$root([^"]*)"', install_line))

    for target in mount_targets:
        assert target in STANDARD_MINBASE_DIRS or target in created_dirs, target


@pytest.fixture(params=["kernel", "rootfs", "magma"])
def builder(request, tmp_path, tools):
    kind = request.param
    directory, env = tools
    source = tmp_path / "source tree"
    source.mkdir()
    output = tmp_path / "output image"
    count = tmp_path / "build-count"
    env["BUILD_COUNT"] = str(count)
    executable(directory / "mmdebstrap", 'mkdir -p "${@: -1}"\n')
    executable(directory / "chroot", 'exit 0\n')
    executable(directory / "mke2fs", 'echo build >> "$BUILD_COUNT"\n[ "${FAIL_BUILD:-0}" != 1 ]\ntar -cf "${@: -2:1}" -C "$5" .\n')
    if kind == "kernel":
        work = tmp_path / "kernel work"
        work.mkdir()
        tree = source / "linux-6.1.186"
        executable(tree / "scripts/config", ':\n')
        executable(tree / "scripts/kconfig/merge_config.sh", 'cat "$3" >> "$2"\n')
        with tarfile.open(work / "linux-6.1.186.tar.xz", "w:xz") as archive:
            archive.add(tree, arcname=tree.name)
        executable(directory / "curl", 'echo CONFIG_SECCOMP_FILTER=y > "${@: -1}"\necho CONFIG_AUDIT=y >> "${@: -1}"\n')
        executable(directory / "make", 'if [ "${@: -1}" = vmlinux ]; then\n[ ! -e old-object ]\necho build >> "$BUILD_COUNT"\n[ "${FAIL_BUILD:-0}" != 1 ]\ncp .config vmlinux\nfi\n')
        change = tmp_path / "extra.config"
        change.write_text("CONFIG_FEATURE=y\n")
        args = [str(output), str(work), str(change)]
        script = "build-kernel.sh"
    elif kind == "rootfs":
        shutil.copytree(ROOT / "firecracker", source / "firecracker")
        change = source / "firecracker/protocol.py"
        args = [str(source), str(output)]
        script = "build-rootfs.sh"
    else:
        change = executable(source / "magma.exe", 'echo one\n')
        args = [str(source), str(output)]
        script = "build-magma-image.sh"
    return kind, [str(ROOT / "firecracker/build" / script), *args], env, output, change, count


def invoke(builder, *flags, ok=True):
    _, args, env, _, _, _ = builder
    result = subprocess.run([args[0], *flags, *args[1:]], env=env, capture_output=True, text=True)
    if ok:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert result.returncode != 0
    assert not result.stdout
    return result


def test_builder_freshness_lifecycle(builder):
    kind, _, _, output, change, count = builder
    assert invoke(builder) == {"changed": True}
    assert output.stat().st_mode & 0o777 == (0o644 if kind == "kernel" else 0o640)
    stamp = Path(str(output) + ".build-stamp.json")
    before = output.stat().st_mtime_ns, stamp.stat().st_mtime_ns
    output.chmod(0o600)
    assert invoke(builder) == {"changed": False}
    assert before == (output.stat().st_mtime_ns, stamp.stat().st_mtime_ns)
    assert len(count.read_text().splitlines()) == 1
    change.write_bytes(change.read_bytes() + b"\n")
    assert invoke(builder) == {"changed": True}
    assert len(count.read_text().splitlines()) == 2
    output.unlink()
    assert invoke(builder) == {"changed": True}
    assert len(count.read_text().splitlines()) == 3
    stamp.unlink()
    assert invoke(builder) == {"changed": True}
    stamp.write_text("invalid")
    assert invoke(builder) == {"changed": True}
    record = json.loads(stamp.read_text())
    record["format"] = -1
    stamp.write_text(json.dumps(record))
    assert invoke(builder) == {"changed": True}
    output.write_bytes(b"")
    assert invoke(builder) == {"changed": True}
    output.write_bytes(b"damaged")
    assert invoke(builder) == {"changed": True}
    assert invoke(builder, "--force") == {"changed": True}
    assert len(count.read_text().splitlines()) == 9


def test_unchanged_output_needs_no_reads(builder, tmp_path):
    _, _, env, output, _, _ = builder
    invoke(builder)
    guard = tmp_path / "python guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import os, sys\n"
        "def guard(event, args):\n"
        "    if event == 'open' and args[0] == os.environ['GUARDED_IMAGE']:\n"
        "        raise RuntimeError('published image was opened')\n"
        "sys.addaudithook(guard)\n"
    )
    env["PYTHONPATH"] = str(guard)
    env["GUARDED_IMAGE"] = str(output)
    assert invoke(builder) == {"changed": False}
    info = output.stat()
    record = json.loads(Path(str(output) + ".build-stamp.json").read_text())
    assert record["output"] == {
        "device": info.st_dev, "inode": info.st_ino,
        "size": info.st_size, "mtime_ns": info.st_mtime_ns,
    }
    env.pop("PYTHONPATH")
    replacement = output.with_name("replacement")
    replacement.write_bytes(output.read_bytes())
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    replacement.replace(output)
    assert invoke(builder) == {"changed": True}


def test_stale_staging_is_removed_on_unchanged_call(builder):
    _, _, _, output, _, _ = builder
    invoke(builder)
    stale = output.with_name(f".{output.name}.build-abandoned")
    stale.mkdir()
    (stale / "artifact").write_bytes(b"partial image")
    unrelated = output.with_name(".another-image.build-abandoned")
    unrelated.mkdir()
    assert invoke(builder) == {"changed": False}
    assert not stale.exists()
    assert unrelated.is_dir()


@pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGHUP], ids=["sigterm", "sighup"])
def test_signal_stops_recipe_and_cleans_staging(builder, tmp_path, interrupt):
    kind, args, env, output, _, _ = builder
    invoke(builder)
    stamp = Path(str(output) + ".build-stamp.json")
    old_output, old_stamp = output.read_bytes(), stamp.read_bytes()
    copied = tmp_path / "build tools"
    shutil.copytree(ROOT / "firecracker/build", copied)
    args[0] = str(copied / Path(args[0]).name)
    ready = tmp_path / "ready"
    env["BUILD_READY"] = str(ready)
    recipe = copied / "recipes" / Path(args[0]).name
    original_recipe = recipe.read_bytes()
    recipe.write_text(
        '#!/bin/bash\nset -eu\n'
        f'echo staged > "${1 if kind == "kernel" else 2}"\n'
        'sleep 60 &\nchild=$!\n'
        'trap \'kill "$child" 2>/dev/null || :; wait "$child" || :; exit 143\' TERM\n'
        'echo "$$ $child" > "$BUILD_READY"\nwait "$child"\n'
    )
    recipe_pid = None
    with subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.01)
            recipe_pid, child_pid = map(int, ready.read_text().split())
            os.killpg(process.pid, interrupt)
            assert process.wait(timeout=10) != 0
            for pid in (recipe_pid, child_pid):
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            assert not list(output.parent.glob(f".{output.name}.build-*"))
            assert output.read_bytes() == old_output
            assert stamp.read_bytes() == old_stamp
            recipe.write_bytes(original_recipe)
            assert invoke(builder) == {"changed": False}
            assert invoke(builder, "--force") == {"changed": True}
        finally:
            for group in (process.pid, recipe_pid):
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.wait(timeout=5)


def test_nohup_build_survives_sighup(builder, tmp_path):
    kind, args, env, output, _, _ = builder
    command = Path(env["PATH"].split(os.pathsep)[0]) / (
        "make" if kind == "kernel" else "mke2fs")
    original = command.read_text()
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    env["BUILD_READY"] = str(ready)
    env["BUILD_RELEASE"] = str(release)
    executable(command, 'echo "$PPID" > "$BUILD_READY"\n'
               'while [ ! -e "$BUILD_RELEASE" ]; do sleep 0.01; done\n' + original)
    recipe_pid = None
    with subprocess.Popen(["nohup", *args], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.01)
            recipe_pid = int(ready.read_text())
            process.send_signal(signal.SIGHUP)
            release.touch()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert json.loads(stdout) == {"changed": True}
            assert output.is_file()
            assert invoke(builder) == {"changed": False}
        finally:
            for group in (process.pid, recipe_pid):
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.wait(timeout=5)


def test_progress_names_published_output(builder):
    _, args, env, output, _, _ = builder
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"changed": True}
    assert output.is_file()
    assert any(line.endswith(f"written to {output}") for line in result.stderr.splitlines())
    assert f".{output.name}.build-" not in result.stderr


def test_builder_usage_names_public_arguments(builder):
    kind, args, env, _, _, _ = builder
    paths = {
        "kernel": "<out-vmlinux> [work-dir] [extra-config]",
        "rootfs": "<calculator-src-dir> <out-image> [site-hook]",
        "magma": "<magma-version-dir> <out-image>",
    }[kind]
    usage = f"usage: {Path(args[0]).name} [--force] {paths}"
    for flags, status in ((["--help"], 0), ([], 2), (["one"] * 4, 2)):
        result = subprocess.run([args[0], *flags], env=env, capture_output=True, text=True)
        assert result.returncode == status
        assert usage in result.stdout + result.stderr
        assert "freshness.py" not in result.stdout + result.stderr


def test_failed_build_preserves_artifact(builder):
    _, _, env, output, change, _ = builder
    invoke(builder)
    stamp = Path(str(output) + ".build-stamp.json")
    old_output, old_stamp = output.read_bytes(), stamp.read_bytes()
    link = output.with_name("old image")
    os.link(output, link)
    change.write_bytes(change.read_bytes() + b"\n")
    env["FAIL_BUILD"] = "1"
    invoke(builder, ok=False)
    assert output.read_bytes() == link.read_bytes() == old_output
    assert stamp.read_bytes() == old_stamp
    env.pop("FAIL_BUILD")
    assert invoke(builder) == {"changed": True}
    assert link.read_bytes() == old_output


def test_failed_first_build_publishes_nothing(builder):
    _, _, env, output, _, _ = builder
    env["FAIL_BUILD"] = "1"
    invoke(builder, ok=False)
    assert not output.exists()
    assert not Path(str(output) + ".build-stamp.json").exists()


def test_missing_input_is_error_even_with_output(builder):
    _, _, _, output, change, _ = builder
    invoke(builder)
    old = output.read_bytes()
    change.unlink()
    invoke(builder, ok=False)
    assert output.read_bytes() == old


def test_stamp_publication_failure_restores_artifact(builder):
    _, _, _, output, change, _ = builder
    invoke(builder)
    old = output.read_bytes()
    stamp = Path(str(output) + ".build-stamp.json")
    stamp.unlink()
    stamp.mkdir()
    change.write_bytes(change.read_bytes() + b"\n")
    invoke(builder, ok=False)
    assert output.read_bytes() == old
    assert stamp.is_dir()
    stamp.rmdir()
    assert invoke(builder) == {"changed": True}
    output.unlink()
    stamp.unlink()
    stamp.mkdir()
    invoke(builder, ok=False)
    assert not output.exists()


def test_relative_input_and_output_paths(builder, tmp_path):
    _, args, env, output, _, _ = builder
    relative = [args[0], *(os.path.relpath(arg, tmp_path) for arg in args[1:])]
    result = subprocess.run(relative, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.is_file()
    if builder[0] == "kernel":
        output = Path(args[2]) / "linux-6.1.186/vmlinux"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b"previous kernel")
        args[1] = str(output)
        env["FAIL_BUILD"] = "1"
        invoke(builder, "--force", ok=False)
        assert output.read_bytes() == b"previous kernel"
        env.pop("FAIL_BUILD")
        assert invoke(builder, "--force") == {"changed": True}


def test_output_path_follows_symlink_before_parent(builder, tmp_path):
    kind, args, env, output, _, _ = builder
    physical = tmp_path / "physical"
    (physical / "current").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(physical / "current", target_is_directory=True)
    output.write_bytes(b"unrelated artifact")
    args[1 if kind == "kernel" else 2] = f"alias/../{output.name}"
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"changed": True}
    assert output.read_bytes() == b"unrelated artifact"
    requested = physical / output.name
    assert requested.is_file()
    assert Path(str(requested) + ".build-stamp.json").is_file()
    assert not Path(str(output) + ".build-stamp.json").exists()
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"changed": False}


def test_input_paths_follow_symlink_before_parent(builder, tmp_path):
    kind, args, _, output, _, _ = builder
    physical = tmp_path / "physical"
    (physical / "current").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(physical / "current", target_is_directory=True)
    if kind == "rootfs":
        args.append(str(executable(tmp_path / "site hook", 'echo setup\n')))
    output_index = 1 if kind == "kernel" else 2
    for index in range(1, len(args)):
        if index == output_index:
            continue
        original = Path(args[index])
        original.rename(physical / original.name)
        args[index] = str(alias / ".." / original.name)
    assert invoke(builder) == {"changed": True}
    if kind == "kernel":
        assert b"CONFIG_FEATURE=y" in output.read_bytes()
    else:
        member = ("./etc/magma-fc/site-init" if kind == "rootfs" else "./magma.exe")
        with tarfile.open(output) as archive:
            assert archive.extractfile(member).read() == b"#!/bin/bash\nset -e\necho " + (
                b"setup\n" if kind == "rootfs" else b"one\n")
    assert invoke(builder) == {"changed": False}


@pytest.mark.parametrize("builder", ["rootfs"], indirect=True)
def test_rootfs_manifest_without_final_newline(builder, tmp_path):
    _, args, _, output, _, _ = builder
    copied = tmp_path / "build tools"
    shutil.copytree(ROOT / "firecracker/build", copied)
    args[0] = str(copied / "build-rootfs.sh")
    manifest = copied / "rootfs-files.tsv"
    manifest.write_bytes(manifest.read_bytes().rstrip(b"\n"))
    assert invoke(builder) == {"changed": True}
    with tarfile.open(output) as archive:
        assert archive.extractfile("./opt/calculator-guest/firecracker/guest/seccomp_policy.py").read() == (
            ROOT / "firecracker/guest/seccomp_policy.py").read_bytes()
    assert invoke(builder) == {"changed": False}


@pytest.mark.parametrize("builder", ["kernel"], indirect=True)
def test_kernel_builds_in_work_directory(builder, tmp_path):
    _, args, env, output, _, _ = builder
    make = Path(env["PATH"].split(os.pathsep)[0]) / "make"
    location = tmp_path / "build-directory"
    env["KERNEL_BUILD_DIRECTORY"] = str(location)
    executable(make, 'pwd > "$KERNEL_BUILD_DIRECTORY"\n' + make.read_text())
    assert invoke(builder) == {"changed": True}
    source = Path(location.read_text().strip())
    assert source.is_relative_to(Path(args[2]).resolve())
    assert not source.exists()
    assert output.is_file()


@pytest.mark.parametrize("builder", ["kernel"], indirect=True)
def test_kernel_recovers_source_tree_after_sigkill(builder, tmp_path):
    _, args, env, output, _, _ = builder
    invoke(builder)
    stamp = Path(str(output) + ".build-stamp.json")
    old_output, old_stamp = output.read_bytes(), stamp.read_bytes()
    make = Path(env["PATH"].split(os.pathsep)[0]) / "make"
    original_make = make.read_bytes()
    ready = tmp_path / "ready"
    env["BUILD_READY"] = str(ready)
    executable(make, 'echo "$PPID $PWD" > "$BUILD_READY"\nsleep 60\n')
    recipe_pid = None
    with subprocess.Popen([args[0], "--force", *args[1:]], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                assert process.poll() is None
                time.sleep(0.01)
            pid, source = ready.read_text().strip().split(" ", 1)
            recipe_pid = int(pid)
            process.kill()
            process.wait(timeout=5)
        finally:
            if recipe_pid is not None:
                try:
                    os.killpg(recipe_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            make.write_bytes(original_make)
    abandoned = Path(source).parent
    assert abandoned.is_dir()
    assert invoke(builder) == {"changed": False}
    assert not abandoned.exists()
    assert output.read_bytes() == old_output
    assert stamp.read_bytes() == old_stamp
    assert invoke(builder, "--force") == {"changed": True}
    assert not list(Path(args[2]).glob("source.*"))


def test_shared_output_is_locked(builder, tmp_path):
    kind, args, _, output, _, count = builder
    lock = Path(str(output) + ".build.lock")
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        invoke(builder, ok=False)
    assert not output.exists()
    assert not count.exists()
    if kind == "kernel":
        with (Path(args[2]) / ".build.lock").open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            args[1] = str(tmp_path / "another kernel")
            invoke(builder, ok=False)
        assert not count.exists()


def test_builder_specific_inputs(builder):
    kind, args, env, output, change, count = builder
    invoke(builder)
    if kind == "kernel":
        args.pop()
        assert invoke(builder) == {"changed": True}
        assert b"CONFIG_FEATURE=y" not in output.read_bytes()
        args.append("")
        assert invoke(builder) == {"changed": False}
        args.pop()
        env["FIRECRACKER_TAG"] = "v-other"
        assert invoke(builder) == {"changed": True}
        source = Path(args[2]) / "linux-6.1.186"
        source.mkdir(exist_ok=True)
        (source / "old-object").touch()
        invoke(builder, "--force")
        assert (source / "old-object").exists()
        args[2] = str(source.parent / "new work directory")
        assert invoke(builder) == {"changed": False}
    elif kind == "rootfs":
        source = Path(args[1])
        change.chmod(0o600)
        args.append("")
        assert invoke(builder) == {"changed": False}
        args.pop()
        (source / "app").mkdir()
        (source / "app/main.py").write_text("unrelated")
        assert invoke(builder) == {"changed": False}
        hook = executable(source / "site hook", 'echo setup\n')
        args.append(str(hook))
        assert invoke(builder) == {"changed": True}
        with tarfile.open(output) as archive:
            assert archive.getmember("./etc/magma-fc/site-init").mode == 0o755
            assert archive.getmember("./opt/calculator-guest/init.sh").mode == 0o755
            assert archive.getmember("./opt/calculator-guest/firecracker/protocol.py").mode == 0o644
            assert not any("app/main.py" in name for name in archive.getnames())
        args.pop()
        assert invoke(builder) == {"changed": True}
        with tarfile.open(output) as archive:
            assert "./etc/magma-fc/site-init" not in archive.getnames()
    else:
        alias = change.parent.parent / "tree alias"
        alias.symlink_to(change.parent, target_is_directory=True)
        args[1] = str(alias)
        assert invoke(builder) == {"changed": False}
        before = change.stat()
        change.write_bytes(change.read_bytes().replace(b"one", b"two"))
        os.utime(change, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert invoke(builder) == {"changed": False}
        assert invoke(builder, "--force") == {"changed": True}
        change.chmod(0o700)
        assert invoke(builder) == {"changed": True}
        hidden = change.parent / ".hidden"
        hidden.write_text("data")
        assert invoke(builder) == {"changed": True}
        hidden.unlink()
        assert invoke(builder) == {"changed": True}
        link = change.parent / "outside"
        link.symlink_to(change.parent.parent, target_is_directory=True)
        assert invoke(builder) == {"changed": True}
        assert invoke(builder) == {"changed": False}
        link.unlink()
        link.symlink_to(change.name)
        assert invoke(builder) == {"changed": True}


def test_recipe_and_manifest_changes(builder, tmp_path):
    kind, args, _, output, _, _ = builder
    copied = tmp_path / "build tools"
    shutil.copytree(ROOT / "firecracker/build", copied)
    args[0] = str(copied / Path(args[0]).name)
    invoke(builder)
    recipe = copied / "recipes" / Path(args[0]).name
    recipe.write_text(recipe.read_text() + '\necho recipe >&2\n')
    assert invoke(builder) == {"changed": True}
    helper = copied / "freshness.py"
    helper.write_text(helper.read_text() + "\n")
    assert invoke(builder) == {"changed": True}
    if kind == "rootfs":
        manifest = copied / "rootfs-files.tsv"
        with manifest.open("a") as stream:
            stream.write("firecracker/protocol.py\t/extra/protocol.py\t0600\n")
        assert invoke(builder) == {"changed": True}
        with tarfile.open(output) as archive:
            assert archive.getmember("./extra/protocol.py").mode == 0o600
