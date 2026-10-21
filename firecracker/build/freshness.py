"""Freshness and publication for the standalone image builders."""

import argparse
from contextlib import ExitStack, contextmanager, suppress
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

BUILD = Path(__file__).resolve().parent
FORMAT = 2


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def payload():
    with (BUILD / "rootfs-files.tsv").open(newline="") as stream:
        return list(csv.reader(stream, delimiter="\t"))


def tree_manifest(tree):
    entries = []
    hardlinks = {}

    def visit(path):
        info = path.lstat()
        entry = [path.relative_to(tree).as_posix(), info.st_mode, info.st_uid,
                 info.st_gid, info.st_mtime_ns]
        if stat.S_ISLNK(info.st_mode):
            entry.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb"):
                pass
            entry.append(info.st_size)
            identity = (info.st_dev, info.st_ino)
            entry.append(hardlinks.setdefault(identity, entry[0]))
        elif not stat.S_ISDIR(info.st_mode):
            entry.append(info.st_rdev)
        entries.append(entry)
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child)

    visit(tree)
    return entries


def fingerprint(kind, paths):
    recipe = BUILD / "recipes" / f"build-{kind if kind != 'magma' else 'magma-image'}.sh"
    wrapper = BUILD / recipe.name
    inputs = [digest(Path(__file__)), digest(recipe), digest(wrapper)]
    if kind == "kernel":
        inputs.extend([os.environ.get("KERNEL_VERSION", "6.1.186"),
                       os.environ.get("FIRECRACKER_TAG", "v1.17.0"),
                       digest(paths[2]) if len(paths) == 3 else None])
    elif kind == "rootfs":
        inputs.extend([digest(BUILD / "rootfs-files.tsv"),
                       [(source, target, mode, digest(paths[0] / source))
                        for source, target, mode in payload()],
                       digest(paths[2]) if len(paths) == 3 else None])
    else:
        if not os.access(paths[0] / "magma.exe", os.X_OK):
            raise ValueError(f"no executable magma.exe under {paths[0]}")
        inputs.append(tree_manifest(paths[0]))
    encoded = json.dumps(inputs, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), recipe


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"build already in progress: {path}") from None
        yield


def output_identity(info):
    return {"device": info.st_dev, "inode": info.st_ino,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def remove_stale_builds(directory, prefix):
    for stale in directory.iterdir():
        if stale.name.startswith(prefix) and stale.is_dir():
            shutil.rmtree(stale)


def current(output, stamp, kind, inputs):
    try:
        record = json.loads(stamp.read_text())
        info = output.stat()
        return (isinstance(record, dict) and record.get("format") == FORMAT
                and record.get("builder") == kind and record.get("inputs") == inputs
                and stat.S_ISREG(info.st_mode) and info.st_size > 0
                and record.get("output") == output_identity(info))
    except (OSError, ValueError):
        return False


def publish(output, stamp, staged, record):
    staged_stamp = staged.parent / "stamp.json"
    staged_stamp.write_text(json.dumps(record) + "\n")
    staged_stamp.chmod(0o600)
    previous = staged.parent / "previous"
    had_output = output.exists()
    if had_output:
        os.link(output, previous)
    os.replace(staged, output)
    try:
        os.replace(staged_stamp, stamp)
    except BaseException:
        if had_output:
            os.replace(previous, output)
        else:
            output.unlink()
        raise


def run_recipe(recipe, paths):
    with subprocess.Popen(["bash", str(recipe), *map(str, paths)],
                          stdout=sys.stderr, start_new_session=True) as process:
        try:
            status = process.wait()
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        if status:
            raise subprocess.CalledProcessError(status, process.args)


def build(args):
    supplied = args.paths.copy()
    if len(supplied) == 3 and not supplied[2]:
        supplied.pop()
    if args.kind == "kernel" and len(supplied) >= 2 and not supplied[1]:
        supplied[1] = "/var/tmp/magma-fc-kernel"
    paths = [Path.cwd() / path for path in supplied]
    if args.kind == "kernel" and len(paths) == 1:
        paths.append(Path("/var/tmp/magma-fc-kernel"))
    if args.kind != "kernel":
        paths[0] = paths[0].resolve(strict=True)
    output_index = 0 if args.kind == "kernel" else 1
    output = paths[output_index]
    stamp = Path(str(output) + ".build-stamp.json")
    with ExitStack() as stack:
        stack.enter_context(locked(Path(str(output) + ".build.lock")))
        remove_stale_builds(output.parent, f".{output.name}.build-")
        if args.kind == "kernel":
            stack.enter_context(locked(paths[1] / ".build.lock"))
            remove_stale_builds(paths[1], ".magma-fc-kernel-source-")
        inputs, recipe = fingerprint(args.kind, paths)
        if not args.force and current(output, stamp, args.kind, inputs):
            return False
        with tempfile.TemporaryDirectory(prefix=f".{output.name}.build-",
                                         dir=output.parent.resolve(strict=True)) as directory:
            staged = Path(directory) / "artifact"
            build_paths = paths.copy()
            build_paths[output_index] = staged
            run_recipe(recipe, build_paths)
            if not staged.is_file() or staged.stat().st_size == 0:
                raise ValueError("builder produced no complete artifact")
            record = {"format": FORMAT, "builder": args.kind, "inputs": inputs,
                      "output": output_identity(staged.stat())}
            publish(output, stamp, staged, record)
        label = "magma image" if args.kind == "magma" else args.kind
        print(f"{label} written to {output}", file=sys.stderr)
        return True


def terminate(signum, frame):
    raise SystemExit(128 + signum)


def main():
    kind = sys.argv[1]
    name = f"build-{kind if kind != 'magma' else 'magma-image'}.sh"
    paths_usage = {
        "kernel": "<out-vmlinux> [work-dir] [extra-config]",
        "rootfs": "<calculator-src-dir> <out-image> [site-hook]",
        "magma": "<magma-version-dir> <out-image>",
    }[kind]
    parser = argparse.ArgumentParser(prog=name, usage=f"%(prog)s [--force] {paths_usage}",
                                     description=__doc__)
    parser.add_argument("--force", action="store_true", help="rebuild with refreshed dependencies")
    parser.add_argument("paths", nargs="+", help=paths_usage)
    args = parser.parse_args(sys.argv[2:])
    args.kind = kind
    limits = {"kernel": (1, 3), "rootfs": (2, 3), "magma": (2, 2)}
    low, high = limits[args.kind]
    if not low <= len(args.paths) <= high:
        parser.error(f"expected {paths_usage}")
    signal.signal(signal.SIGTERM, terminate)
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:
        signal.signal(signal.SIGHUP, terminate)
    try:
        changed = build(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"build-{args.kind}: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"changed": changed}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
