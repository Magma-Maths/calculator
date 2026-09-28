import errno
import json
import time
import uuid
from pathlib import Path

from conftest import (
    CgroupWatch,
    ContainmentBlocked,
    DockerController,
    EXPECTED_CPU_MAX,
    EXPECTED_MEMORY_MAX,
    EXPECTED_PIDS_MAX,
    NetworkSinks,
    parse_probe,
    read_cgroup,
    require_success,
)


def nonce(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:16]


def evidence(property_name: str, **facts) -> None:
    print(
        "CONTAINMENT_EVIDENCE "
        + json.dumps({"property": property_name, **facts}, sort_keys=True),
        flush=True,
    )


def observe_request(
    controller: DockerController,
    mode: str,
    argument: str,
):
    watch = controller.watch()
    try:
        response = controller.execute(mode, argument)
        time.sleep(0.05)
    finally:
        watch.stop()
    watch.require_request_paths()
    return response, watch


def require_limits(watch: CgroupWatch) -> None:
    watch.require_request_paths()
    assert watch.saw_limits(
        str(EXPECTED_MEMORY_MAX),
        str(EXPECTED_PIDS_MAX),
        EXPECTED_CPU_MAX,
    )


def test_verified_image_routes_answer_through_real_nsjail(controller: DockerController):
    response = controller.answer()
    assert response.status == 200
    assert response.body["success"] is True
    assert response.body["exit_code"] == 0
    assert response.body["stdout"] == "PROBE answer OK 42\n"
    evidence("loader", http_status=response.status, exit_code=response.body["exit_code"])


def test_network_namespaces_block_controlled_outer_sinks(
    controller: DockerController,
    network_sinks: NetworkSinks,
):
    positive_counts = {
        name: network_sinks.positive_control(name)
        for name in ("net4", "net6", "metadata")
    }
    cases = (
        ("net4", "19041", "inet", "loopback"),
        ("net6", "19042", "inet6", "loopback"),
        ("metadata", None, "inet", "metadata"),
    )
    for mode, argument, family, target in cases:
        response = controller.execute(mode, argument)
        record = require_success(response, mode)
        assert record.status == "BLOCKED", response.body
        assert record.fields["family"] == family
        assert record.fields["target"] == target
        assert record.integer("connected") == 0
        assert record.integer("errno") != 0
        assert record.integer("elapsed_ms") <= 300
        time.sleep(0.05)
        assert network_sinks.accept_count(mode) == positive_counts[mode]
        evidence(
            mode,
            http_status=response.status,
            errno=record.integer("errno"),
            elapsed_ms=record.integer("elapsed_ms"),
            sink_accepts=network_sinks.accept_count(mode),
            positive_controls=positive_counts[mode],
        )


def test_abstract_sockets_are_isolated_between_overlapping_requests(
    controller: DockerController,
):
    value = nonce("abstract")
    listener = controller.execute_async("abstract_listen", f"{value}:2000")
    identity = controller.wait_for_abstract_listener(value)
    connector_response = controller.execute("abstract_connect", value)
    listener_response = listener.result(timeout=6)
    assert not identity.still_exists()

    listen_record = require_success(listener_response, "abstract_listen")
    connect_record = require_success(connector_response, "abstract_connect")
    assert listen_record.status == "OK", listener_response.body
    assert listen_record.integer("errno") == 0
    assert connect_record.status == "BLOCKED", connector_response.body
    assert connect_record.integer("errno") != 0
    assert listen_record.integer("attempted_ms") <= connect_record.integer("attempted_ms")
    assert connect_record.integer("attempted_ms") <= listen_record.integer("closed_ms")
    evidence(
        "abstract_socket",
        listener_pid=identity.pid,
        listener_start_time=identity.start_time,
        listen_attempted_ms=listen_record.integer("attempted_ms"),
        connect_attempted_ms=connect_record.integer("attempted_ms"),
        listen_closed_ms=listen_record.integer("closed_ms"),
        connect_errno=connect_record.integer("errno"),
    )


def test_tmp_is_fresh_for_serial_and_overlapping_requests(controller: DockerController):
    scan = require_success(controller.execute("tmp_scan"), "tmp_scan")
    assert scan.status == "EMPTY"
    assert scan.integer("probe_entries") == 0

    serial_nonce = nonce("serial")
    written = require_success(controller.execute("tmp_write", serial_nonce), "tmp_write")
    assert written.status == "OK"
    assert written.integer("errno") == 0
    absent = require_success(controller.execute("tmp_read", serial_nonce), "tmp_read")
    assert absent.status == "ABSENT"
    assert absent.integer("errno") == errno.ENOENT

    overlap_nonce = nonce("overlap")
    writer = controller.execute_async("tmp_write", f"{overlap_nonce}:2000")
    identity = controller.wait_for_tmp_marker(overlap_nonce)
    concurrent_read = controller.execute("tmp_read", overlap_nonce)
    writer_response = writer.result(timeout=6)
    assert not identity.still_exists()
    write_record = require_success(writer_response, "tmp_write")
    read_record = require_success(concurrent_read, "tmp_read")
    assert write_record.status == "OK"
    assert read_record.status == "ABSENT"
    assert read_record.integer("errno") == errno.ENOENT
    evidence(
        "fresh_tmp",
        writer_pid=identity.pid,
        writer_start_time=identity.start_time,
        serial_errno=absent.integer("errno"),
        overlap_errno=read_record.integer("errno"),
    )


def test_persistent_paths_are_read_only_and_tmp_is_noexec(controller: DockerController):
    mounts, _namespaces = controller.jail_observation()
    required_read_only = ("/opt/magma", "/usr/lib", "/lib")
    for target in required_read_only:
        assert target in mounts, f"missing jail mount {target}"
        assert "ro" in mounts[target].options, mounts[target]
    if "/lib64" in mounts:
        assert "ro" in mounts["/lib64"].options, mounts["/lib64"]
    assert "/tmp" in mounts
    assert {"rw", "nosuid", "nodev", "noexec"} <= mounts["/tmp"].options

    path_classes = ("root", "app", "data", "magma", "home", "usrlib", "lib")
    accepted_denials = {errno.EACCES, errno.EROFS, errno.ENOENT}
    for path_class in path_classes:
        response = controller.execute("path_write", f"{path_class}:{nonce('path')}")
        record = require_success(response, "path_write")
        assert record.status == "DENIED", response.body
        assert record.fields["target"] == path_class
        assert record.fields["operation"] == "create"
        assert record.integer("errno") in accepted_denials

    execution = controller.execute("tmp_exec", nonce("exec"))
    record = require_success(execution, "tmp_exec")
    assert record.status == "DENIED", execution.body
    assert record.fields["operation"] == "copy_exec"
    assert record.integer("errno") == errno.EACCES

    persistent_filesystems = {"overlay", "ext2", "ext3", "ext4", "xfs", "btrfs"}
    untested = [
        mount.target
        for mount in mounts.values()
        if "rw" in mount.options
        and mount.filesystem in persistent_filesystems
        and not any(
            mount.target == root or mount.target.startswith(root + "/")
            for root in ("/", "/app", "/data", "/home/calculator")
        )
    ]
    if untested:
        raise ContainmentBlocked(f"untested writable persistent mounts: {sorted(untested)}")
    evidence(
        "filesystem",
        path_classes=list(path_classes),
        tmp_mount_options=sorted(mounts["/tmp"].options),
        tmp_exec_errno=record.integer("errno"),
    )


def test_memory_limit_kills_the_probe_and_increments_cgroup_events(
    controller: DockerController,
):
    response, watch = observe_request(controller, "memory_touch", "512")
    require_limits(watch)
    assert response.status == 200, response.body
    assert response.body.get("success") is False, response.body
    assert isinstance(response.body.get("exit_code"), int), response.body
    assert response.body["exit_code"] != 0, response.body
    assert "PROBE memory_touch OK" not in response.body.get("stdout", "")
    assert max(
        watch.counter_delta("memory_events", "oom"),
        watch.counter_delta("memory_events", "oom_kill"),
    ) > 0
    evidence(
        "memory",
        requested_mib=512,
        exit_code=response.body["exit_code"],
        oom=watch.counter_delta("memory_events", "oom"),
        oom_kill=watch.counter_delta("memory_events", "oom_kill"),
    )
    controller.answer()


def test_pid_limit_denies_forks_and_reaps_every_child(controller: DockerController):
    response, watch = observe_request(controller, "fork_limit", "80")
    require_limits(watch)
    record = require_success(response, "fork_limit")
    assert record.status == "DENIED", response.body
    assert record.integer("requested") == 80
    assert record.integer("created") < 80
    assert record.integer("errno") == errno.EAGAIN
    assert watch.counter_delta("pids_events", "max") > 0
    leaked = [identity for identity in watch.recorded_members() if identity.still_exists()]
    assert not leaked, leaked
    evidence(
        "pids",
        requested=record.integer("requested"),
        created=record.integer("created"),
        errno=record.integer("errno"),
        pids_max_events=watch.counter_delta("pids_events", "max"),
    )
    controller.answer()


def test_cpu_quota_throttles_two_workers(controller: DockerController):
    response, watch = observe_request(controller, "cpu_burn", "2500")
    require_limits(watch)
    record = require_success(response, "cpu_burn")
    assert record.status == "OK", response.body
    assert record.integer("workers") == 2
    assert record.integer("elapsed_ms") >= 2400
    assert watch.counter_delta("cpu_stat", "nr_throttled") > 0
    assert watch.counter_delta("cpu_stat", "throttled_usec") > 0
    evidence(
        "cpu",
        workers=record.integer("workers"),
        elapsed_ms=record.integer("elapsed_ms"),
        nr_throttled=watch.counter_delta("cpu_stat", "nr_throttled"),
        throttled_usec=watch.counter_delta("cpu_stat", "throttled_usec"),
    )
    controller.answer()


def assert_output_failure(response, ceiling: str) -> None:
    assert response.status == 200, response.body
    assert response.body.get("success") is False, response.body
    assert response.body.get("truncated") is True, response.body
    assert isinstance(response.body.get("exit_code"), int), response.body
    assert ceiling in response.body.get("error", "").lower(), response.body
    assert any(ceiling in warning.lower() for warning in response.body.get("warnings", []))
    assert len(response.body.get("stdout", "").encode("utf-8")) <= 20 * 1024


def test_combined_output_ceiling_reaps_the_group(controller: DockerController):
    response, watch = observe_request(controller, "stdout_flood", str(300 * 1024))
    watch.require_request_paths()
    assert_output_failure(response, "combined capture ceiling")
    leaked = [identity for identity in watch.recorded_members() if identity.still_exists()]
    assert not leaked, leaked
    evidence(
        "combined_output",
        exit_code=response.body["exit_code"],
        stdout_bytes=len(response.body.get("stdout", "").encode("utf-8")),
    )
    controller.answer()


def test_stderr_ceiling_is_independent_of_combined_capture(controller: DockerController):
    response, watch = observe_request(controller, "stderr_flood", str(16 * 1024))
    watch.require_request_paths()
    assert_output_failure(response, "stderr capture ceiling")
    leaked = [identity for identity in watch.recorded_members() if identity.still_exists()]
    assert not leaked, leaked
    evidence(
        "stderr_output",
        exit_code=response.body["exit_code"],
        stdout_bytes=len(response.body.get("stdout", "").encode("utf-8")),
    )
    controller.answer()


def test_timeout_removes_live_descendant_before_its_natural_exit(controller: DockerController):
    started = time.monotonic()
    natural_deadline = started + 8
    watch = controller.watch()
    request = controller.execute_async("descendant_hold", "8000", timeout=10)
    parent = None
    child = None
    try:
        observation_deadline = started + 2
        while time.monotonic() < observation_deadline and child is None:
            candidates = [
                identity
                for identity in watch.recorded_members()
                if controller.is_fixture_process(identity)
                and len(identity.nspid) >= 2
                and identity.still_exists()
            ]
            by_pid = {identity.pid: identity for identity in candidates}
            for candidate in candidates:
                possible_parent = by_pid.get(candidate.ppid)
                if possible_parent is not None:
                    parent = possible_parent
                    child = candidate
                    break
            time.sleep(0.01)
        if parent is None or child is None:
            raise ContainmentBlocked(
                "fixture parent and child ancestry were not visible while the request ran"
            )
        response = request.result(timeout=8)
    finally:
        watch.stop()

    require_limits(watch)
    assert response.status == 200, response.body
    assert response.body.get("success") is False, response.body
    assert isinstance(response.body.get("exit_code"), int), response.body
    assert time.monotonic() < natural_deadline
    record = parse_probe(response, "descendant_hold")
    assert record.status == "OK", response.body
    assert record.integer("hold_ms") == 8000
    assert record.integer("child_pid") == child.nspid[-1]
    assert child.ppid == parent.pid
    assert parent.cgroup == child.cgroup

    while time.monotonic() < natural_deadline and (parent.still_exists() or child.still_exists()):
        time.sleep(0.02)
    assert time.monotonic() < natural_deadline
    assert not parent.still_exists()
    assert not child.still_exists()
    for path in watch.request_paths:
        state = read_cgroup(Path(path))
        assert state is None or not state.members, (path, state)
    evidence(
        "descendants",
        parent_pid=parent.pid,
        parent_start_time=parent.start_time,
        parent_nspid=parent.nspid,
        child_pid=child.pid,
        child_start_time=child.start_time,
        child_nspid=child.nspid,
        cgroup=child.cgroup,
        api_elapsed=response.elapsed,
        error=response.body.get("error"),
        warnings=response.body.get("warnings"),
    )
    controller.answer()
