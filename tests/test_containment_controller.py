import os
import subprocess
from pathlib import Path

from tests.containment import conftest as containment


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


def test_runtime_preflight_uses_supported_nsjail_help(tmp_path, monkeypatch):
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
