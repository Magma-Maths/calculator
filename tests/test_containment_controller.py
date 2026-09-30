import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.containment import conftest as containment


def test_acceptance_body_reports_missing_evidence_as_blocked(tmp_path):
    containment_root = Path(__file__).parent / "containment"
    script = tmp_path / "test_missing_evidence.py"
    script.write_text(
        """
import importlib.util
import sys
from pathlib import Path

import pytest

root = Path(CONTAINMENT_ROOT)
conftest_spec = importlib.util.spec_from_file_location("conftest", root / "conftest.py")
conftest = importlib.util.module_from_spec(conftest_spec)
sys.modules["conftest"] = conftest
conftest_spec.loader.exec_module(conftest)
acceptance_spec = importlib.util.spec_from_file_location("acceptance", root / "test_acceptance.py")
acceptance = importlib.util.module_from_spec(acceptance_spec)
acceptance_spec.loader.exec_module(acceptance)


class Controller:
    def __init__(self, escape_mode=None):
        self.escape_mode = escape_mode

    def jail_observation(self):
        mount = conftest.MountObservation
        return {
            "/opt/magma": mount("/opt/magma", frozenset({"ro"}), "overlay", "none"),
            "/usr/lib": mount("/usr/lib", frozenset({"ro"}), "overlay", "none"),
            "/lib": mount("/lib", frozenset({"ro"}), "overlay", "none"),
            "/tmp": mount("/tmp", frozenset({"rw", "nosuid", "nodev", "noexec"}), "tmpfs", "tmpfs"),
            "/unchecked": mount("/unchecked", frozenset({"rw"}), "ext4", "/dev/vda"),
        }, {}

    def execute(self, mode, argument):
        status = "OK" if mode == self.escape_mode else "DENIED"
        if mode == "path_write":
            target = argument.split(":", 1)[0]
            line = f"PROBE path_write {status} target={target} operation=create errno=13"
        else:
            line = f"PROBE tmp_exec {status} operation=copy_exec errno=13"
        return conftest.ApiResponse(
            200, {"success": True, "exit_code": 0, "stdout": line + "\\n"}, 0.0
        )


def test_missing_evidence():
    acceptance.test_persistent_paths_are_read_only_and_tmp_is_noexec(Controller())


def test_assertion_failure():
    class IncompleteController(Controller):
        def jail_observation(self):
            mounts, namespaces = super().jail_observation()
            mounts.pop("/tmp")
            return mounts, namespaces

    acceptance.test_persistent_paths_are_read_only_and_tmp_is_noexec(IncompleteController())


@pytest.mark.parametrize("mode", ["path_write", "tmp_exec"])
def test_escape_precedes_missing_mount_coverage(mode):
    with pytest.raises(AssertionError, match="OK"):
        acceptance.test_persistent_paths_are_read_only_and_tmp_is_noexec(
            Controller(escape_mode=mode)
        )
""".replace("CONTAINMENT_ROOT", repr(str(containment_root))).lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "BLOCKED: untested writable persistent mounts" in result.stdout + result.stderr
    assert "AssertionError" in result.stdout + result.stderr
    assert "2 passed" in result.stdout + result.stderr
    assert "2 failed" in result.stdout + result.stderr


def test_raw_containment_blocks_keep_failed_reports_with_labels(tmp_path):
    script = tmp_path / "test_raw_blocks.py"
    script.write_text(
        """
import pytest

from tests.containment.conftest import ContainmentBlocked


@pytest.fixture
def blocked_setup():
    raise ContainmentBlocked("setup evidence unavailable")


@pytest.fixture
def blocked_teardown():
    yield
    raise ContainmentBlocked("teardown evidence unavailable")


def test_setup(blocked_setup):
    pass


def test_body():
    raise ContainmentBlocked("body evidence unavailable")


def test_teardown(blocked_teardown):
    pass


def test_assertion():
    assert False, "ordinary assertion"
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text(
        """
def pytest_runtest_logreport(report):
    if report.failed:
        blocked = report.longreprtext.startswith("BLOCKED: ")
        print(f"REPORT {report.nodeid.split('::')[-1]} {report.when} failed {blocked}")
""".lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "tests.containment.conftest", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    for test_name, phase in (
        ("test_setup", "setup"),
        ("test_body", "call"),
        ("test_teardown", "teardown"),
    ):
        assert f"REPORT {test_name} {phase} failed True" in output
    assert "REPORT test_assertion call failed False" in output
    assert "AssertionError: ordinary assertion" in output


def controller_without_init(**attributes):
    controller = object.__new__(containment.DockerController)
    for name, value in attributes.items():
        setattr(controller, name, value)
    return controller


def cgroup_state(
    path: Path,
    *,
    memory_events=None,
    memory_max=None,
    memory_swap_max=None,
    pids_max=None,
    cpu_max=None,
):
    return containment.CgroupState(
        path=path,
        members=(),
        memory_events=memory_events or {},
        pids_events={},
        cpu_stat={},
        memory_max=memory_max,
        memory_swap_max=memory_swap_max,
        pids_max=pids_max,
        cpu_max=cpu_max,
    )


def test_abstract_observer_accepts_resolved_fixture_argv(tmp_path, monkeypatch):
    identity = containment.ProcessIdentity(
        pid=4123,
        start_time=991,
        ppid=4000,
        nspid=(4123, 1),
        cgroup="/calculator/NSJAIL.4000",
        command="/opt/magma/versions/probe/magma.exe -w -n",
    )
    fixture_root = tmp_path / "magma-fixture"
    (fixture_root / "versions" / "probe").mkdir(parents=True)
    (fixture_root / "current").symlink_to("versions/probe")
    controller = containment.DockerController("sha256:" + "1" * 64, "2" * 64, fixture_root)
    controller.live_service_processes = lambda: [identity]
    monkeypatch.setattr(containment, "_read_text", lambda _path: "@calc-probe-resolved")
    monkeypatch.setattr(containment.ProcessIdentity, "still_exists", lambda _self: True)

    try:
        observed = controller.wait_for_abstract_listener("resolved", timeout=0.02)
    finally:
        controller._executor.shutdown()

    assert observed == identity
    assert controller.fixture_executable == "/opt/magma/versions/probe/magma.exe"


@pytest.mark.parametrize("profile", ["magma-calculator (enforce)", "magma-calculator (complain)", "unconfined"])
def test_runtime_preflight_requires_enforced_profile_and_nsjail_help(tmp_path, monkeypatch, profile):
    root = tmp_path / "service"
    api = root / "api"
    api.mkdir(parents=True)
    files = {
        root / "cgroup.procs": "",
        root / "memory.events": "oom 0\noom_kill 0\n",
        root / "pids.events": "max 0\n",
        root / "cpu.stat": "usage_usec 0\n",
        root / "memory.max": str(containment.OUTER_MEMORY_MAX),
        root / "pids.max": str(containment.OUTER_PIDS_MAX),
        root / "cpu.max": "max 100000",
        root / "cgroup.controllers": "cpu memory pids\n",
        root / "cgroup.subtree_control": "cpu memory pids\n",
        api / "cgroup.procs": "123\n",
    }
    for path, value in files.items():
        path.write_text(value)

    inspection = {
        "AppArmorProfile": "magma-calculator",
        "HostConfig": {
            "Privileged": False,
            "CapAdd": ["SYS_ADMIN"],
            "Memory": containment.OUTER_MEMORY_MAX,
            "PidsLimit": containment.OUTER_PIDS_MAX,
            "CgroupnsMode": "private",
            "Tmpfs": {"/tmp": "size=128m", "/data": ""},
        },
        "Mounts": [{"Destination": "/opt/magma", "RW": False}],
    }
    controller = controller_without_init(
        container="calculator-test",
        image_id="sha256:" + "1" * 64,
        archive_sha="2" * 64,
        init_pid=123,
        service_cgroup=root,
    )
    controller.require_service_identity = lambda: None
    controller.inspect = lambda _name: [inspection]
    docker_calls = []

    def fake_docker(args, *, check=False):
        docker_calls.append(args)
        if args[-1] == "-h":
            return subprocess.CompletedProcess(args, 0, "Usage: nsjail [options]\n", "")
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 1, "", "Usage: nsjail [options]\n")
        return subprocess.CompletedProcess(args, 0, "7f454c46\n", "")

    controller.docker = fake_docker
    original_read_text = containment._read_text

    def fake_read_text(path):
        if path == Path("/proc/123/attr/current"):
            return profile
        if path == Path("/proc/123/status"):
            return "CapEff:\t0000000000200000"
        return original_read_text(path)

    def fake_readlink(path):
        if path.startswith("/proc/123/ns/"):
            return "service:[1]"
        if path.startswith("/proc/self/ns/"):
            return "host:[2]"
        return os.readlink(path)

    monkeypatch.setattr(containment, "_read_text", fake_read_text)
    monkeypatch.setattr(containment.os, "readlink", fake_readlink)

    if profile != "magma-calculator (enforce)":
        with pytest.raises(containment.ContainmentBlocked, match="AppArmor profile is not enforced"):
            controller._check_runtime()
        assert docker_calls == []
        return

    controller._check_runtime()

    nsjail_call = next(call for call in docker_calls if "/usr/local/bin/nsjail" in call)
    assert nsjail_call[-1] == "-h"


def test_hierarchical_oom_event_survives_request_cgroup_removal():
    root = Path("/sys/fs/cgroup/calculator")
    watch = object.__new__(containment.CgroupWatch)
    watch.root = root
    watch.baseline = {
        root: cgroup_state(root, memory_events={"oom": 0, "oom_kill": 0}),
    }
    watch.states = {
        root: [cgroup_state(root, memory_events={"oom": 1, "oom_kill": 1})],
    }

    assert watch.counter_delta("memory_events", "oom_kill") == 1


def test_request_limit_observation_requires_zero_swap():
    root = Path("/sys/fs/cgroup/calculator")
    request = root / "NSJAIL.456"
    watch = object.__new__(containment.CgroupWatch)
    watch.root = root
    watch.baseline = {}
    watch.states = {
        request: [
            cgroup_state(
                request,
                memory_max=str(containment.EXPECTED_MEMORY_MAX),
                memory_swap_max="max",
                pids_max=str(containment.EXPECTED_PIDS_MAX),
                cpu_max=containment.EXPECTED_CPU_MAX,
            )
        ]
    }

    assert not watch.saw_limits(
        str(containment.EXPECTED_MEMORY_MAX),
        "0",
        str(containment.EXPECTED_PIDS_MAX),
        containment.EXPECTED_CPU_MAX,
    )

    watch.states[request] = [
        cgroup_state(
            request,
            memory_max=str(containment.EXPECTED_MEMORY_MAX),
            memory_swap_max="0",
            pids_max=str(containment.EXPECTED_PIDS_MAX),
            cpu_max=containment.EXPECTED_CPU_MAX,
        )
    ]
    assert watch.saw_limits(
        str(containment.EXPECTED_MEMORY_MAX),
        "0",
        str(containment.EXPECTED_PIDS_MAX),
        containment.EXPECTED_CPU_MAX,
    )


def test_controller_starts_service_with_requested_timeout(tmp_path, monkeypatch):
    fixture_root = tmp_path / "magma-fixture"
    (fixture_root / "versions" / "probe").mkdir(parents=True)
    (fixture_root / "current").symlink_to("versions/probe")
    controller = containment.DockerController(
        "sha256:" + "1" * 64,
        "2" * 64,
        fixture_root,
        magma_timeout=10,
    )
    docker_calls = []

    def fake_docker(args, *, check=False):
        docker_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    controller.docker = fake_docker
    controller._resolve_runtime = lambda: None
    controller._wait_ready = lambda: None
    controller._observe_service_cgroup = lambda: None
    controller._check_runtime = lambda: None
    controller.answer = lambda: None
    controller.jail_observation = lambda: None
    monkeypatch.setattr(containment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(containment.shutil, "which", lambda _name: "/usr/bin/docker")

    try:
        assert controller.__enter__() is controller
    finally:
        controller._executor.shutdown()

    run_call = next(call for call in docker_calls if call[:2] == ["run", "-d"])
    assert "MAGMA_TIMEOUT=10" in run_call
    assert run_call[run_call.index("--cgroupns") + 1] == "private"
    assert run_call[run_call.index("--security-opt") + 1] == "apparmor=magma-calculator"


@pytest.mark.parametrize("migration_before_inspect", [False, True])
def test_controller_observes_service_root_after_api_migration(tmp_path, monkeypatch, migration_before_inspect):
    fixture = tmp_path / "fixture"
    (fixture / "versions" / "probe").mkdir(parents=True)
    (fixture / "current").symlink_to("versions/probe")
    controller = containment.DockerController("sha256:" + "1" * 64, "2" * 64, fixture)
    ready = False

    def health(_request, *, timeout):
        nonlocal ready
        ready = True
        return nullcontext(SimpleNamespace(status=200))

    def process(pid):
        group = "/calculator-test/api" if ready or migration_before_inspect else "/calculator-test"
        return containment.ProcessIdentity(pid, 99, 1, (pid, 1), group, "python")

    controller.docker = lambda args, **_kw: subprocess.CompletedProcess(args, 0, "", "")
    controller.inspect = lambda *_args, **_kw: [{
        "State": {"Pid": 123, "Running": True},
        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}},
    }]
    controller._check_runtime = lambda: None
    controller.answer = lambda: None
    controller.jail_observation = lambda: None
    monkeypatch.setattr(containment.os, "geteuid", lambda: 0)
    monkeypatch.setattr(containment.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(containment.urllib.request, "urlopen", health)
    monkeypatch.setattr(containment, "read_process", process)

    try:
        with controller:
            assert controller.init_identity.cgroup == "/calculator-test/api"
            assert controller.service_cgroup == Path("/sys/fs/cgroup/calculator-test")
    finally:
        controller.cleanup()
