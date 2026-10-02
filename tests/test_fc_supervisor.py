import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import time

import pytest

from firecracker import protocol
from firecracker.host import jail, supervisor


@pytest.fixture
def config(request):
    # AF_UNIX socket paths are limited to 108 bytes, so the jail base must stay
    # short; pytest's default tmp_path nesting is long enough to blow that budget.
    base = pathlib.Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(base, ignore_errors=True))
    for name in ("vmlinux", "rootfs.ext4", "magma.ext4"):
        (base / name).write_bytes(b"x")
    return {
        "socket": str(base / "sup.sock"),
        "socket_group": None,
        "jail_base": str(base / "jails"),
        "images": {"kernel": str(base / "vmlinux"), "rootfs": str(base / "rootfs.ext4"), "magma": str(base / "magma.ext4")},
        "uid": os.getuid(), "gid": os.getgid(),
        "mem_mib": 512, "vcpus": 1, "boot_timeout": 2,
        "slots": [{"name": "slot1", "mac": "02:00:00:00:00:01"}, {"name": "slot2", "mac": "02:00:00:00:00:02"}],
    }


class FakeHost:
    """Fake systemctl + fake Firecracker vsock endpoint + fake agent."""

    def __init__(self, base, agent_reply=None, boot=True, agent_delay=0.0, stop_leaves_cgroup=False):
        self.base = base
        self.calls = []
        self.agent_reply = agent_reply or {"stdout": "2\n", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False}
        self.boot = boot
        self.agent_delay = agent_delay
        self.stop_leaves_cgroup = stop_leaves_cgroup
        self.servers = {}
        self.seen_requests = []

    async def systemctl(self, args):
        self.calls.append(args)
        if args[0] == "start" and self.boot:
            unit = args[1]
            slot = unit.split("@", 1)[1].removesuffix(".service")
            path = os.path.join(jail.jail_root(self.base, slot), "run", "vsock.sock")

            async def handle(reader, writer):
                assert await reader.readline() == b"CONNECT 52\n"
                writer.write(b"OK 1024\n"); await writer.drain()
                self.seen_requests.append(await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES))
                await asyncio.sleep(self.agent_delay)
                await protocol.write_frame(writer, self.agent_reply)
                writer.close()

            self.servers[slot] = await asyncio.start_unix_server(handle, path=path)
        if args[0] == "stop":
            slot = args[1].split("@", 1)[1].removesuffix(".service")
            srv = self.servers.pop(slot, None)
            if srv:
                srv.close()
        return 0, ""

    async def cgroup_populated(self, unit):
        return self.stop_leaves_cgroup


def _request(**over):
    r = {"code": "print 1+1;\nquit;\n", "timeout": 3, "cpu_timeout": 3, "output_bytes": 1024}
    r.update(over)
    return r


def test_run_job_success_full_lifecycle(config):
    host = FakeHost(config["jail_base"])
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request()))
    assert reply["stdout"] == "2\n" and reply["exit_code"] == 0
    assert host.calls[0] == ["start", "magma-fc@slot1.service"]
    assert host.calls[-1] == ["stop", "magma-fc@slot1.service"]
    assert not os.path.exists(os.path.join(config["jail_base"], "firecracker", "slot1"))
    req = host.seen_requests[0]
    assert req["magma_exe"] == "/opt/magma/current/magma.exe"
    assert req["env"]["MAGMAPASSFILE"] == "/opt/magma/current/magmapassfile"
    assert req["env"]["OMP_NUM_THREADS"] == "1"
    assert req["code"] == "print 1+1;\nquit;\n"
    assert runner.free_slots() == 2


def test_run_job_boot_timeout_stops_unit(config):
    host = FakeHost(config["jail_base"], boot=False)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    t0 = time.monotonic()
    reply = asyncio.run(runner.run_job(_request()))
    assert reply["error"] == "worker_failed"
    assert time.monotonic() - t0 < config["boot_timeout"] + 2
    assert ["stop", "magma-fc@slot1.service"] in host.calls
    assert runner.free_slots() == 2


def test_run_job_reply_deadline_stops_unit(config):
    host = FakeHost(config["jail_base"], agent_delay=10)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request(timeout=1)))
    assert reply["error"] == "worker_failed"
    assert reply["timed_out"] is True
    assert ["stop", "magma-fc@slot1.service"] in host.calls


def test_run_job_quarantines_slot_when_cgroup_not_empty(config, monkeypatch):
    monkeypatch.setattr(supervisor, "STOP_GRACE", 0.3)
    host = FakeHost(config["jail_base"], stop_leaves_cgroup=True)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request()))
    assert reply["stdout"] == "2\n"
    assert runner.free_slots() == 1
    assert runner.quarantined() == ["slot1"]
    assert os.path.exists(jail.jail_root(config["jail_base"], "slot1"))


def test_two_jobs_get_distinct_slots(config):
    host = FakeHost(config["jail_base"], agent_delay=0.3)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)

    async def run():
        return await asyncio.gather(runner.run_job(_request()), runner.run_job(_request()))

    replies = asyncio.run(run())
    assert all(r["exit_code"] == 0 for r in replies)
    starts = [c[1] for c in host.calls if c[0] == "start"]
    assert sorted(starts) == ["magma-fc@slot1.service", "magma-fc@slot2.service"]


def test_busy_when_no_slot(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], agent_delay=0.5)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)

    async def run():
        return await asyncio.gather(runner.run_job(_request()), runner.run_job(_request()))

    replies = asyncio.run(run())
    errors = sorted(r.get("error", "ok") for r in replies)
    assert errors == ["busy", "ok"]


def test_runner_rejects_uppercase_mac(config):
    config["slots"][0]["mac"] = "0A:00:00:00:00:01"
    with pytest.raises(ValueError):
        supervisor.Runner(config)


def test_bad_request(config):
    host = FakeHost(config["jail_base"])
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job({"code": 5}))
    assert reply["error"] == "bad_request"
    assert host.calls == []


def test_bad_request_rejects_oversized_limits(config):
    host = FakeHost(config["jail_base"])
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request(timeout=301)))
    assert reply["error"] == "bad_request"
    reply = asyncio.run(runner.run_job(_request(output_bytes=protocol.MAX_REPLY_BYTES)))
    assert reply["error"] == "bad_request"
    assert host.calls == []


def test_run_job_recovers_slot_on_internal_exception(config):
    for key in ("kernel", "rootfs", "magma"):
        os.remove(config["images"][key])
    host = FakeHost(config["jail_base"])

    async def run():
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        server = await supervisor.serve(runner, config["socket"], group=None)
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        await protocol.write_frame(writer, _request())
        reply = await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)
        writer.close()
        server.close()
        await server.wait_closed()
        return reply, runner

    reply, runner = asyncio.run(run())
    assert reply["error"] == "worker_failed"
    assert runner.free_slots() == len(config["slots"])


def test_host_caps_reply_even_if_agent_lies(config):
    big = {"stdout": "y" * 5000, "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False}
    host = FakeHost(config["jail_base"], agent_reply=big)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request(output_bytes=1024)))
    assert len(reply["stdout"].encode()) <= 1024
    assert reply["truncated"] is True


def test_unix_socket_server_roundtrip(config):
    host = FakeHost(config["jail_base"])

    async def run():
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        server = await supervisor.serve(runner, config["socket"], group=None)
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        await protocol.write_frame(writer, _request())
        reply = await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)
        writer.close()
        server.close()
        await server.wait_closed()
        return reply

    assert asyncio.run(run())["stdout"] == "2\n"


def test_run_job_passes_seccomp_fields_through(config):
    agent_reply = {
        "stdout": "", "stderr": "", "exit_code": -1, "timed_out": False, "truncated": False,
        "seccomp_killed": True, "seccomp_mode": "log", "seccomp_log": ["audit: type=1326 syscall=101"],
    }
    host = FakeHost(config["jail_base"], agent_reply=agent_reply)
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request()))
    assert reply["exit_code"] == -1 and reply["timed_out"] is False
    assert reply["seccomp_killed"] is True
    assert reply["seccomp_mode"] == "log"
    assert reply["seccomp_log"] == ["audit: type=1326 syscall=101"]


def test_guest_seccomp_reaches_boot_args(config, monkeypatch):
    config["guest_seccomp"] = "log"
    host = FakeHost(config["jail_base"])
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    seen = {}
    real_stage = jail.stage

    def spy(*args):
        root = real_stage(*args)
        with open(os.path.join(root, "config.json"), encoding="utf-8") as fh:
            seen["boot_args"] = json.load(fh)["boot-source"]["boot_args"]
        return root

    monkeypatch.setattr(jail, "stage", spy)
    asyncio.run(runner.run_job(_request()))
    assert "magma.seccomp=log audit=1" in seen["boot_args"]


def test_bad_guest_seccomp_rejected_at_startup(config):
    config["guest_seccomp"] = "strict"
    with pytest.raises(ValueError):
        supervisor.Runner(config)


def test_client_that_never_reads_is_dropped_after_the_write_deadline(config, monkeypatch):
    monkeypatch.setattr(supervisor, "REPLY_WRITE_TIMEOUT", 0.3, raising=False)
    big = {"stdout": "y" * 500_000, "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False}
    host = FakeHost(config["jail_base"], agent_reply=big)

    async def run():
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        server = await supervisor.serve(runner, config["socket"], group=None)
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        await protocol.write_frame(writer, _request(output_bytes=500_000))
        # Long enough for the job to finish and the write deadline to pass.
        await asyncio.sleep(1.5)
        try:
            with pytest.raises(protocol.FrameError):
                await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)
        finally:
            writer.close()
            server.close()
            await server.wait_closed()
        return runner

    runner = asyncio.run(run())
    assert runner.free_slots() == len(config["slots"])


def test_connections_beyond_the_cap_are_refused(config):
    config["max_connections"] = 1
    host = FakeHost(config["jail_base"])

    async def run():
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        server = await supervisor.serve(runner, config["socket"], group=None)
        _, idle = await asyncio.open_unix_connection(config["socket"])
        await asyncio.sleep(0.1)
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        await protocol.write_frame(writer, _request())
        t0 = time.monotonic()
        reply = await asyncio.wait_for(protocol.read_frame(reader, protocol.MAX_REPLY_BYTES), timeout=5)
        elapsed = time.monotonic() - t0
        writer.close()
        idle.close()
        server.close()
        await server.wait_closed()
        return reply, elapsed

    reply, elapsed = asyncio.run(run())
    assert reply["error"] == "busy"
    assert elapsed < 1
    assert host.calls == []
