import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from app.config import Settings


ROOT = Path("/sys/fs/cgroup")
REQUIRED_CONTROLLERS = {"memory", "pids", "cpu"}
OUTER_MEMORY_BYTES = 3 * 1024**3
OUTER_PIDS = 320
APPARMOR_PROFILE = "magma-calculator"


class CgroupFiles:
    def read(self, path: Path) -> str:
        return path.read_text().strip()

    def write(self, path: Path, value: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
        try:
            data = value.encode()
            if os.write(fd, data) != len(data):
                raise OSError(f"short cgroup write: {path.name}")
        finally:
            os.close(fd)

    def mkdir(self, path: Path) -> None:
        path.mkdir(exist_ok=True)

    def rmdir(self, path: Path) -> None:
        path.rmdir()

    def remount_cgroup(self, path: Path) -> None:
        # A bind remount changes this mount's flags, not the shared superblock.
        try:
            result = subprocess.run(
                [
                    "/bin/mount", "--no-mtab", "-o",
                    "remount,bind,rw,nosuid,nodev,noexec", "--", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("cgroup remount timed out") from exc
        _require(result.returncode == 0, f"cgroup remount failed: {result.stderr.strip()}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _check_mount(mountinfo: str, cgroup_path: str, root: Path) -> bool:
    _require(cgroup_path.strip() == "0::/", "process is outside the cgroup namespace root")
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        fs_fields = right.split()
        if len(fields) < 6 or len(fs_fields) < 1 or fields[4] != str(root):
            continue
        _require(fields[3] == "/", "cgroup mount does not expose the namespace root")
        _require(fs_fields[0] == "cgroup2", "cgroup mount is not cgroup2")
        options = set(fields[5].split(","))
        _require(bool(options & {"ro", "rw"}), "cgroup2 mount has no access mode")
        return "rw" in options
    raise RuntimeError("cgroup2 mount is missing")


def _check_budget(settings: Settings) -> None:
    _require(settings.max_concurrent > 0, "MAX_CONCURRENT must be positive")
    _require(settings.magma_memory_mb > 0, "MAGMA_MEMORY_MB must be positive")
    _require(settings.magma_pids_max > 0, "MAGMA_PIDS_MAX must be positive")
    _require(
        settings.max_concurrent * settings.magma_memory_mb <= 2048,
        "child memory budget exceeds 2048 MiB",
    )
    _require(
        settings.max_concurrent * settings.magma_pids_max + 64 <= OUTER_PIDS,
        "child task budget exceeds 320 tasks with API reserve",
    )


def _check_outer_limits(root: Path, fs: CgroupFiles) -> None:
    _require(
        fs.read(root / "memory.max") == str(OUTER_MEMORY_BYTES),
        "outer memory.max is not 3 GiB",
    )
    _require(fs.read(root / "pids.max") == str(OUTER_PIDS), "outer pids.max is not 320")


def _check_controls(root: Path, fs: CgroupFiles, pid: int) -> None:
    _check_outer_limits(root, fs)
    available = set(fs.read(root / "cgroup.controllers").split())
    _require(REQUIRED_CONTROLLERS <= available, "memory, pids, or cpu controller is missing")

    api = root / "api"
    fs.mkdir(api)
    fs.write(api / "cgroup.procs", str(pid))
    _require(str(pid) in fs.read(api / "cgroup.procs").split(), "API cgroup migration failed")
    _require(not fs.read(root / "cgroup.procs"), "cgroup root still has internal processes")

    fs.write(root / "cgroup.subtree_control", "+memory +pids +cpu")
    enabled = set(fs.read(root / "cgroup.subtree_control").split())
    _require(REQUIRED_CONTROLLERS <= enabled, "cgroup controllers were not enabled")

    probe = root / f"bootstrap-check-{pid}"
    fs.mkdir(probe)
    try:
        limits = {
            "memory.max": str(400 * 1024**2),
            "memory.swap.max": "0",
            "pids.max": "64",
            "cpu.max": "1000000 1000000",
        }
        for name, expected in limits.items():
            fs.write(probe / name, expected)
            _require(fs.read(probe / name) == expected, f"{name} write did not stick")
    finally:
        fs.rmdir(probe)


def main(
    *,
    root: Path = ROOT,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    cgroup_path: Path = Path("/proc/self/cgroup"),
    fs: CgroupFiles | None = None,
    settings: Settings | None = None,
    exec_fn: Callable[[str, list[str]], object] = os.execv,
) -> int:
    fs = fs or CgroupFiles()
    try:
        configured = settings if settings is not None else Settings()
    except ValidationError as exc:
        issues = set()
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = error["loc"]
            field = location[0] if location and location[0] in Settings.model_fields else "settings"
            issues.add(f"{field}: {error['type']}")
        summary = ", ".join(sorted(issues))
        print(f"cgroup bootstrap: invalid configuration ({summary})", file=sys.stderr)
        return 1
    try:
        _check_budget(configured)
        writable = _check_mount(fs.read(mountinfo_path), fs.read(cgroup_path), root)
        if not writable:
            _check_outer_limits(root, fs)
            _require(
                fs.read(Path("/proc/self/attr/current")) == f"{APPARMOR_PROFILE} (enforce)",
                "cgroup remount requires the enforced magma-calculator AppArmor profile",
            )
            fs.remount_cgroup(root)
            _require(
                _check_mount(fs.read(mountinfo_path), fs.read(cgroup_path), root),
                "cgroup2 mount is still read-only after remount",
            )
        _check_controls(root, fs, os.getpid())
    except (OSError, RuntimeError) as exc:
        print(f"cgroup bootstrap: {exc}", file=sys.stderr)
        return 1
    exec_fn(sys.executable, [sys.executable, "-m", "app.main"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
