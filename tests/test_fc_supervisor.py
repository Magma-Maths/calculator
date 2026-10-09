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

    def __init__(
        self, base, agent_reply=None, boot=True, agent_delay=0.0, stop_leaves_cgroup=False, agent_frame=None,
        boot_delay=0.0,
    ):
        self.base = base
        self.boot_delay = boot_delay
        self.agents = {}
        self.connected = []
        self.served = []
        self.agent_frame = agent_frame
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
                self.agents[slot] = writer
                self.connected.append((time.monotonic(), slot))
                generation = len(self.connected)
                try:
                    self.seen_requests.append(await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES))
                except protocol.FrameError:
                    return  # the host dropped an idle guest
                self.served.append(generation)
                await asyncio.sleep(self.agent_delay)
                if self.agent_frame is not None:
                    writer.write(self.agent_frame); await writer.drain()
                else:
                    await protocol.write_frame(writer, self.agent_reply)
                writer.close()

            if self.boot_delay:
                await asyncio.sleep(self.boot_delay)
            self.servers[slot] = await asyncio.start_unix_server(handle, path=path)
        if args[0] == "stop":
            slot = args[1].split("@", 1)[1].removesuffix(".service")
            srv = self.servers.pop(slot, None)
            if srv:
                srv.close()
            self.kill(slot)
        return 0, ""

    async def cgroup_populated(self, unit):
        return self.stop_leaves_cgroup

    def kill(self, slot):
        """The guest's Firecracker exits, closing its end of the vsock connection."""
        agent = self.agents.pop(slot, None)
        if agent:
            agent.close()

    def starts(self, slot):
        return self.calls.count(["start", f"magma-fc@{slot}.service"])


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


def test_reply_that_overflows_the_frame_once_escaped_is_truncated(config, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_REPLY_BYTES", 64 * 1024)
    # A guest may send raw UTF-8; the host re-encodes it with each "é" as \u00e9.
    body = json.dumps(
        {"stdout": "é" * 20_000, "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False},
        ensure_ascii=False,
    ).encode()
    host = FakeHost(config["jail_base"], agent_frame=len(body).to_bytes(4, "big") + body)

    async def run():
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        server = await supervisor.serve(runner, config["socket"], group=None)
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        await protocol.write_frame(writer, _request(output_bytes=32 * 1024))
        try:
            return await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)
        finally:
            writer.close()
            server.close()
            await server.wait_closed()

    reply = asyncio.run(run())
    assert reply["truncated"] is True
    assert reply["exit_code"] == 0
    assert reply["stdout"] and set(reply["stdout"]) == {"é"}


def test_code_that_overflows_the_guest_frame_once_escaped_is_rejected(config):
    host = FakeHost(config["jail_base"])
    runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
    reply = asyncio.run(runner.run_job(_request(code="\x01" * 44_000)))
    assert reply["error"] == "bad_request"
    assert reply["stderr"] == "code too large"
    assert host.calls == []


def test_reset_quarantines_a_slot_whose_release_raises(config):
    """A release failure during startup reset() must quarantine that slot,
    not crash the whole supervisor before it ever opens its socket."""
    async def systemctl(args):
        if args == ["stop", "magma-fc@slot1.service"]:
            raise OSError("systemctl not found")
        return 0, ""

    async def cgroup_populated(unit):
        return False

    runner = supervisor.Runner(config, systemctl=systemctl, cgroup_populated=cgroup_populated)
    asyncio.run(runner.reset())
    assert runner.quarantined() == ["slot1"]
    assert runner.free_slots() == 1


async def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


def _pool_config(config):
    config.update(max_timeout=10, runtime_max_sec=150)
    return config


def test_start_preboots_every_slot_and_shutdown_tears_them_down(config):
    host = FakeHost(config["jail_base"])

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await _until(lambda: len(host.connected) == 2)
        assert runner.free_slots() == 2
        await runner.shutdown()

    asyncio.run(run())
    for slot in ("slot1", "slot2"):
        assert host.starts(slot) == 1
        assert host.calls[-2:].count(["stop", f"magma-fc@{slot}.service"]) == 1
        assert not os.path.exists(os.path.join(config["jail_base"], "firecracker", slot))
    assert host.seen_requests == []


def test_job_on_a_preboot_slot_does_not_wait_for_boot(config):
    host = FakeHost(config["jail_base"], boot_delay=1.0)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        server = await supervisor.serve(runner, config["socket"], group=None)
        await runner.start()
        await _until(lambda: len(host.connected) == 2)
        t0 = time.monotonic()
        reader, writer = await asyncio.open_unix_connection(config["socket"])
        await protocol.write_frame(writer, _request())
        reply = await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)
        elapsed = time.monotonic() - t0
        writer.close()
        server.close()
        await server.wait_closed()
        await runner.shutdown()
        return reply, t0, elapsed

    reply, t0, elapsed = asyncio.run(run())
    assert reply["stdout"] == "2\n"
    assert all(when < t0 for when, _ in host.connected[:2])
    assert elapsed < host.boot_delay / 2


def test_replacement_boots_after_each_job(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"])

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await _until(lambda: len(host.connected) == 1)
        first = await runner.run_job(_request(code="first;"))
        await _until(lambda: len(host.connected) == 2)
        second = await runner.run_job(_request(code="second;"))
        await _until(lambda: len(host.connected) == 3)
        assert runner.free_slots() == 1
        await runner.shutdown()
        return first, second

    first, second = asyncio.run(run())
    assert first["stdout"] == second["stdout"] == "2\n"
    assert host.served == [1, 2]
    assert [r["code"] for r in host.seen_requests] == ["first;", "second;"]
    starts = [i for i, c in enumerate(host.calls) if c == ["start", "magma-fc@slot1.service"]]
    assert len(starts) == 3
    assert ["stop", "magma-fc@slot1.service"] in host.calls[starts[0]:starts[1]]


def test_job_waits_for_a_slot_that_is_still_booting(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], boot_delay=0.5)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        reply = await runner.run_job(_request())
        await runner.shutdown()
        return reply

    assert asyncio.run(run())["stdout"] == "2\n"
    assert host.calls.index(["start", "magma-fc@slot1.service"]) == 1


def test_dead_idle_guest_is_replaced_and_the_job_still_runs(config):
    host = FakeHost(config["jail_base"])

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await _until(lambda: len(host.connected) == 2)
        host.kill("slot1")
        host.kill("slot2")
        await asyncio.sleep(0.1)
        reply = await runner.run_job(_request())
        await runner.shutdown()
        return reply

    reply = asyncio.run(run())
    assert reply["stdout"] == "2\n"
    assert len(host.seen_requests) == 1
    assert max(host.starts(s) for s in ("slot1", "slot2")) >= 2


def test_failed_preboot_is_retried_once_by_the_job(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], boot=False)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await asyncio.sleep(config["boot_timeout"] + 0.5)
        assert host.starts("slot1") == 1
        reply = await runner.run_job(_request())
        starts_by_job = host.starts("slot1") - 1
        await runner.shutdown()
        return reply, starts_by_job, runner

    reply, starts_by_job, runner = asyncio.run(run())
    assert reply["error"] == "worker_failed"
    assert starts_by_job == 1
    assert runner.quarantined() == []


def test_job_waiting_on_a_boot_that_fails_does_not_boot_again(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], boot=False)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        t0 = time.monotonic()
        reply = await runner.run_job(_request())
        elapsed = time.monotonic() - t0
        starts_by_job = host.starts("slot1")
        await runner.shutdown()
        return reply, elapsed, starts_by_job

    reply, elapsed, starts = asyncio.run(run())
    assert reply["error"] == "worker_failed"
    assert starts == 1
    assert elapsed < config["boot_timeout"] + 1


def test_failed_preboot_that_does_not_drain_quarantines_the_slot(config, monkeypatch, caplog):
    monkeypatch.setattr(supervisor, "STOP_GRACE", 0.3)
    host = FakeHost(config["jail_base"], boot=False)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        host.stop_leaves_cgroup = True
        await _until(lambda: len(runner.quarantined()) == 2, timeout=config["boot_timeout"] + 3)
        return await runner.run_job(_request()), runner

    with caplog.at_level("INFO", logger="magma-fc"):
        reply, runner = asyncio.run(run())
    assert reply["error"] == "busy"
    assert runner.free_slots() == 0
    assert "(0 free, 2 quarantined)" in caplog.text


def test_busy_only_when_every_slot_is_running_a_job(config):
    host = FakeHost(config["jail_base"], agent_delay=0.5)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await _until(lambda: len(host.connected) == 2)
        replies = await asyncio.gather(*(runner.run_job(_request()) for _ in range(3)))
        await runner.shutdown()
        return replies

    errors = sorted(r.get("error", "ok") for r in asyncio.run(run()))
    assert errors == ["busy", "ok", "ok"]


def test_idle_guest_is_recycled_before_the_unit_runtime_limit(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"])

    async def run():
        # 9 - 3 - 5 = 1 s of idle budget, floored at boot_timeout (2 s).
        config.update(max_timeout=3, runtime_max_sec=9)
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await _until(lambda: len(host.connected) == 2, timeout=4)
        reply = await runner.run_job(_request())
        await runner.shutdown()
        return reply

    assert asyncio.run(run())["stdout"] == "2\n"
    assert host.starts("slot1") >= 2
    assert len(host.seen_requests) == 1


def test_job_waiting_on_a_boot_whose_guest_is_unusable_does_not_boot_again(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], boot_delay=1.0)

    async def run():
        # The guest is 1 s old once ready, and 1 + 1 + 5 >= 7 leaves no room under RuntimeMaxSec.
        config.update(max_timeout=1, runtime_max_sec=7)
        runner = supervisor.Runner(config, systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        reply = await runner.run_job(_request(timeout=1, cpu_timeout=1))
        starts = host.starts("slot1")
        await runner.shutdown()
        return reply, starts

    reply, starts = asyncio.run(run())
    assert reply["error"] == "worker_failed"
    assert starts == 1
    assert host.seen_requests == []


def test_cancelled_job_lets_the_boot_it_waited_on_settle(config):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], boot_delay=0.5)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        job = asyncio.create_task(runner.run_job(_request()))
        await asyncio.sleep(0.1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        connected_at_release = len(host.connected)
        await _until(lambda: len(host.connected) == 2)
        reply = await runner.run_job(_request())
        await runner.shutdown()
        return connected_at_release, reply

    connected_at_release, reply = asyncio.run(run())
    assert connected_at_release == 1
    assert reply["stdout"] == "2\n"
    assert host.served == [2]


def test_shutdown_waits_for_staging_in_flight(config, monkeypatch):
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"])
    real_stage = jail.stage

    def slow_stage(*args):
        time.sleep(0.5)
        return real_stage(*args)

    monkeypatch.setattr(jail, "stage", slow_stage)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        await asyncio.sleep(0.1)
        await runner.shutdown()
        await asyncio.sleep(0.7)

    asyncio.run(run())
    assert not os.path.exists(os.path.join(config["jail_base"], "firecracker", "slot1"))


def test_idle_quarantine_logs_the_counts(config, monkeypatch, caplog):
    monkeypatch.setattr(supervisor, "STOP_GRACE", 0.3)
    config["slots"] = config["slots"][:1]
    host = FakeHost(config["jail_base"], boot=False)

    async def run():
        runner = supervisor.Runner(_pool_config(config), systemctl=host.systemctl, cgroup_populated=host.cgroup_populated)
        await runner.start()
        host.stop_leaves_cgroup = True
        await _until(lambda: runner.quarantined() == ["slot1"], timeout=config["boot_timeout"] + 3)

    with caplog.at_level("INFO", logger="magma-fc"):
        asyncio.run(run())
    assert "idle slot1 quarantined (0 free, 1 quarantined)" in caplog.text
