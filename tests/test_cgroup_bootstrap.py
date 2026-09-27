from pathlib import Path

from app.cgroup_bootstrap import main
from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_resource_configuration_contract():
    jail = (ROOT / "nsjail.cfg").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    env = (ROOT / "calculator.env.example").read_text()

    assert 'cgroupv2_mount: "/sys/fs/cgroup"' in jail
    assert "use_cgroupv2: true" in jail
    assert "detect_cgroupv2: false" in jail
    assert "cgroup_mem_parent" not in jail
    assert "cgroup_pids_parent" not in jail
    assert "cgroup_cpu_parent" not in jail
    assert "mem_limit: 3g" in compose
    assert "pids_limit: 320" in compose
    assert "MAGMA_PIDS_MAX=64" in env
    assert "MAGMA_CPU_MS_PER_SEC=1000" in env


class FakeCgroupFiles:
    def __init__(self, root: Path, *, controllers: str = "memory pids cpu", probe_writable=True):
        self.root = root
        self.probe_writable = probe_writable
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
        }

    def read(self, path: Path) -> str:
        return self.files[path]

    def write(self, path: Path, value: str) -> None:
        if path.parent.name.startswith("bootstrap-check-") and not self.probe_writable:
            raise PermissionError("controller is not delegated")
        if path == self.root / "cgroup.subtree_control":
            value = value.replace("+", "")
        if path == self.root / "api" / "cgroup.procs":
            self.files[self.root / "cgroup.procs"] = ""
        self.files[path] = value

    def mkdir(self, path: Path) -> None:
        if path.name.startswith("bootstrap-check-"):
            for name in ("memory.max", "pids.max", "cpu.max"):
                self.files[path / name] = "max"
        else:
            self.files[path / "cgroup.procs"] = ""

    def rmdir(self, path: Path) -> None:
        for file in list(self.files):
            if file.parent == path:
                del self.files[file]


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


def test_bootstrap_execs_after_probe(tmp_path):
    fs = FakeCgroupFiles(tmp_path)
    status, calls = run_fake_bootstrap(tmp_path, fs)

    assert status == 0
    assert len(calls) == 1
    assert calls[0][1][-2:] == ["-m", "app.main"]
    assert not any(path.name.startswith("bootstrap-check-") for path in fs.files)
