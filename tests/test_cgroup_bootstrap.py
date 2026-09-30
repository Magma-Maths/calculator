from pathlib import Path
import subprocess

import pytest

from app.cgroup_bootstrap import CgroupFiles, main
from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_resource_configuration_contract():
    jail = (ROOT / "nsjail.cfg").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    env = (ROOT / "calculator.env.example").read_text()

    assert 'cgroupv2_mount: "/sys/fs/cgroup"' in jail
    assert "use_cgroupv2: true" in jail
    assert "detect_cgroupv2: false" in jail
    assert "cgroup_mem_swap_max: 0" in jail
    assert "cgroup_mem_parent" not in jail
    assert "cgroup_pids_parent" not in jail
    assert "cgroup_cpu_parent" not in jail
    assert "mem_limit: 3g" in compose
    assert "pids_limit: 320" in compose
    assert "MAGMA_PIDS_MAX=64" in env
    assert "MAGMA_CPU_MS_PER_SEC=1000" in env


class FakeCgroupFiles(CgroupFiles):
    def __init__(
        self,
        root: Path,
        *,
        controllers: str = "memory pids cpu",
        probe_writable=True,
        swap_write_sticks=True,
    ):
        self.root = root
        self.probe_writable = probe_writable
        self.swap_write_sticks = swap_write_sticks
        self.directories = {root}
        self.writes = []
        self.files = {
            root / "memory.max": str(3 * 1024**3),
            root / "pids.max": "320",
            root / "cgroup.controllers": controllers,
            root / "cgroup.procs": "1",
            root / "cgroup.subtree_control": "",
            Path("/proc/self/mountinfo"): (
                f"29 23 0:26 / {root} rw,nosuid - cgroup2 cgroup rw"
            ),
            Path("/proc/self/cgroup"): "0::/",
            Path("/proc/self/attr/current"): "magma-calculator (enforce)",
        }

    def read(self, path: Path) -> str:
        return self.files[path]

    def write(self, path: Path, value: str) -> None:
        if path.parent.name.startswith("bootstrap-check-") and not self.probe_writable:
            raise PermissionError("controller is not delegated")
        self.writes.append((path, value))
        if path.name == "memory.swap.max" and not self.swap_write_sticks:
            return
        if path == self.root / "cgroup.subtree_control":
            value = value.replace("+", "")
        if path == self.root / "api" / "cgroup.procs":
            self.files[self.root / "cgroup.procs"] = ""
        self.files[path] = value

    def mkdir(self, path: Path) -> None:
        self.directories.add(path)
        if path.name.startswith("bootstrap-check-"):
            for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
                self.files[path / name] = "max"
        else:
            self.files[path / "cgroup.procs"] = ""

    def rmdir(self, path: Path) -> None:
        for file in list(self.files):
            if file.parent == path:
                del self.files[file]
        self.directories.discard(path)


def assert_no_bootstrap_probe(fs: FakeCgroupFiles) -> None:
    leaked = sorted(
        path.name for path in fs.directories if path.name.startswith("bootstrap-check-")
    )
    assert not leaked, f"bootstrap probe directories remain: {leaked}"


def run_fake_bootstrap(root: Path, fs: FakeCgroupFiles, **settings_overrides):
    calls = []
    status = main(
        root=root,
        fs=fs,
        settings=Settings(**settings_overrides),
        exec_fn=lambda executable, argv: calls.append((executable, argv)),
    )
    return status, calls


def test_bootstrap_stops_without_required_controller(tmp_path, capsys):
    fs = FakeCgroupFiles(tmp_path, controllers="memory pids")
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 1
    assert calls == []
    assert "cpu controller is missing" in capsys.readouterr().err


def test_bootstrap_redacts_invalid_settings(tmp_path, monkeypatch, capsys):
    secret = "SYNTHETIC_SECRET_REVIEW_VALUE"
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", secret)
    monkeypatch.setenv("MAGMA_CPU_MS_PER_SEC", "0")
    fs = FakeCgroupFiles(tmp_path)

    def forbid_cgroup_read(_path):
        raise AssertionError("cgroup read")

    fs.read = forbid_cgroup_read
    calls = []

    status = main(root=tmp_path, fs=fs, exec_fn=lambda *args: calls.append(args))

    assert status == 1
    assert calls == []
    diagnostic = capsys.readouterr().err
    assert secret.split("_", 1)[1] not in diagnostic
    assert "invalid configuration (settings: value_error)" in diagnostic


def test_bootstrap_stops_over_budget(tmp_path, capsys):
    fs = FakeCgroupFiles(tmp_path)
    status, calls = run_fake_bootstrap(tmp_path, fs, max_concurrent=6)

    assert status == 1
    assert calls == []
    assert "child memory budget exceeds 2048 MiB" in capsys.readouterr().err


def test_bootstrap_stops_over_task_budget(tmp_path, capsys):
    fs = FakeCgroupFiles(tmp_path)
    status, calls = run_fake_bootstrap(tmp_path, fs, max_concurrent=5)

    assert status == 1
    assert calls == []
    assert "child task budget exceeds 320 tasks" in capsys.readouterr().err


def test_bootstrap_stops_when_limit_write_fails(tmp_path, capsys):
    fs = FakeCgroupFiles(tmp_path, probe_writable=False)
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 1
    assert calls == []
    assert "controller is not delegated" in capsys.readouterr().err


def test_bootstrap_stops_when_zero_swap_readback_differs(tmp_path, capsys):
    fs = FakeCgroupFiles(tmp_path, swap_write_sticks=False)
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 1
    assert calls == []
    assert "memory.swap.max write did not stick" in capsys.readouterr().err
    assert_no_bootstrap_probe(fs)


def test_probe_cleanup_assertion_rejects_a_leaked_directory(tmp_path):
    fs = FakeCgroupFiles(tmp_path)
    fs.directories.add(tmp_path / "bootstrap-check-leaked")

    with pytest.raises(AssertionError, match="bootstrap-check-leaked"):
        assert_no_bootstrap_probe(fs)


def test_bootstrap_execs_after_probe(tmp_path):
    fs = FakeCgroupFiles(tmp_path)
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 0
    assert len(calls) == 1
    assert calls[0][1][-2:] == ["-m", "app.main"]
    assert any(
        path.name == "memory.swap.max" and value == "0"
        for path, value in fs.writes
    )
    assert_no_bootstrap_probe(fs)


def read_only_cgroup(root):
    fs = FakeCgroupFiles(root)
    fs.files[Path("/proc/self/mountinfo")] = (
        f"29 23 0:26 / {root} ro,nosuid,nodev,noexec - cgroup2 cgroup rw"
    )
    fs.files[Path("/proc/self/attr/current")] = "magma-calculator (enforce)"
    return fs


def test_bootstrap_remounts_scoped_cgroup_before_migrating_api(tmp_path):
    fs = read_only_cgroup(tmp_path)
    remounted = []

    def remount(root):
        assert fs.writes == []
        remounted.append(root)
        mountinfo = Path("/proc/self/mountinfo")
        fs.files[mountinfo] = fs.files[mountinfo].replace(" ro,", " rw,")

    fs.remount_cgroup = remount
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 0
    assert len(calls) == 1
    assert remounted == [tmp_path]
    assert any(path.name == "cgroup.subtree_control" for path, _ in fs.writes)


@pytest.mark.parametrize("change", ["namespace", "mount_root", "fstype", "memory", "pids", "profile"])
def test_bootstrap_rejects_unsafe_remount_before_any_mutation(tmp_path, change):
    fs = read_only_cgroup(tmp_path)
    if change == "namespace":
        fs.files[Path("/proc/self/cgroup")] = "0::/host/service"
    elif change == "mount_root":
        key = Path("/proc/self/mountinfo")
        fs.files[key] = fs.files[key].replace("0:26 / ", "0:26 /host ")
    elif change == "fstype":
        key = Path("/proc/self/mountinfo")
        fs.files[key] = fs.files[key].replace(" - cgroup2 ", " - tmpfs ")
    elif change == "memory":
        fs.files[tmp_path / "memory.max"] = "max"
    elif change == "pids":
        fs.files[tmp_path / "pids.max"] = "max"
    else:
        fs.files[Path("/proc/self/attr/current")] = "unconfined"

    def forbidden_remount(_root):
        pytest.fail("unsafe mount was remounted")

    fs.remount_cgroup = forbidden_remount
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 1
    assert calls == []
    assert fs.writes == []


@pytest.mark.parametrize("denied", [False, True])
def test_bootstrap_stops_when_remount_cannot_make_cgroup_writable(tmp_path, capsys, denied):
    fs = read_only_cgroup(tmp_path)
    attempts = []

    def remount(root):
        attempts.append(root)
        if denied:
            raise PermissionError("remount denied")

    fs.remount_cgroup = remount
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert attempts == [tmp_path]
    assert status == 1
    assert calls == []
    assert fs.writes == []
    diagnostic = capsys.readouterr().err
    assert ("remount denied" if denied else "still read-only after remount") in diagnostic


@pytest.mark.parametrize("outcome", ["ok", "denied", "timeout"])
def test_bootstrap_uses_bounded_bind_remount(tmp_path, monkeypatch, capsys, outcome):
    fs = read_only_cgroup(tmp_path)

    def mount(command, **kwargs):
        assert command == [
            "/bin/mount", "--no-mtab", "-o",
            "remount,bind,rw,nosuid,nodev,noexec", "--", str(tmp_path),
        ]
        assert kwargs["timeout"] == 5
        assert fs.writes == []
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        if outcome == "denied":
            return subprocess.CompletedProcess(command, 32, "", "permission denied")
        key = Path("/proc/self/mountinfo")
        fs.files[key] = fs.files[key].replace(" ro,", " rw,")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", mount)
    status, calls = run_fake_bootstrap(tmp_path, fs)

    if outcome == "ok":
        assert status == 0
        assert len(calls) == 1
    else:
        assert status == 1
        assert calls == []
        assert fs.writes == []
        assert "cgroup remount" in capsys.readouterr().err


@pytest.mark.parametrize("profile", ["unconfined", "magma-calculator (complain)", "docker-default (enforce)"])
def test_bootstrap_requires_enforced_profile_even_on_writable_cgroup(tmp_path, capsys, profile):
    fs = FakeCgroupFiles(tmp_path)
    fs.files[Path("/proc/self/attr/current")] = profile

    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 1
    assert calls == []
    assert fs.writes == []
    assert "requires the enforced magma-calculator AppArmor profile" in capsys.readouterr().err
