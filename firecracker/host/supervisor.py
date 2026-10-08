"""Root-owned service that runs one Firecracker guest per job.

The public API talks to this over a UNIX socket with a data-only protocol.
Nothing in a request can choose a host path, a unit name, an image or a
command: the request carries Magma source and numeric limits, and the
supervisor decides everything else from its own config.
"""
import asyncio
import grp
import json
import logging
import os
import time

from app.magma_cmd import MAGMA_CONSTANT_ENV, magma_environment
from firecracker import protocol
from firecracker.host import jail, vsock

log = logging.getLogger("magma-fc")

GUEST_MAGMA_ROOT = "/opt/magma/current"
STOP_GRACE = 8.0
REPLY_WRITE_TIMEOUT = 10.0
GUEST_REQUEST_OVERHEAD = 16 * 1024
REQUEST_KEYS = {"code": str, "timeout": int, "cpu_timeout": int, "output_bytes": int}


def guest_environment(root: str = GUEST_MAGMA_ROOT) -> dict:
    """The guest's Magma environment: parity with nsjail.cfg plus --env.

    Root-dependent variables come from the same magma_environment() helper
    the nsjail path uses; MAGMA_CONSTANT_ENV mirrors nsjail.cfg's constant
    envar lines, so both backends run Magma under the same environment.
    """
    env = {name: value for name, _, value in (item.partition("=") for item in magma_environment(root))}
    env.update(MAGMA_CONSTANT_ENV)
    env["PATH"] = "/usr/bin:/bin"
    env["HOME"] = "/tmp"
    env["TMPDIR"] = "/tmp"
    return env


def _error(kind: str, message: str, timed_out: bool = False) -> dict:
    return {"error": kind, "stdout": "", "stderr": message, "exit_code": -1, "timed_out": timed_out, "truncated": False}


async def real_systemctl(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def real_cgroup_populated(unit: str) -> bool:
    """True unless we can positively confirm the unit's cgroup is empty.

    `cgroup.procs` only lists directly-attached processes, and the jailer may
    place firecracker in a child cgroup, so this reads the recursive
    `cgroup.events` "populated" flag instead. Anything that stops us from
    reading a definite answer (a non-zero `systemctl show`, or any OSError
    other than the cgroup already being gone) is treated as populated: the
    slot gets quarantined rather than risk reuse while a process may be
    alive.
    """
    rc, out = await real_systemctl(["show", "-p", "ControlGroup", "--value", unit])
    if rc != 0:
        return True
    cg = out.strip()
    if not cg:
        return False
    events = f"/sys/fs/cgroup{cg}/cgroup.events"
    try:
        with open(events, encoding="ascii") as fh:
            return any(line.strip() == "populated 1" for line in fh)
    except FileNotFoundError:
        return False
    except OSError:
        return True


class Runner:
    def __init__(self, config: dict, systemctl=real_systemctl, cgroup_populated=real_cgroup_populated):
        self.config = config
        self.systemctl = systemctl
        self.cgroup_populated = cgroup_populated
        self.images = jail.Images(**config["images"])
        self._free: asyncio.Queue | None = None
        self._slots = [jail.Slot(s["name"], s["mac"]) for s in config["slots"]]
        self._quarantined: list[str] = []
        # Fail fast on a bad slot MAC (jail.render_config raises ValueError)
        # instead of only discovering it mid-job when staging that slot.
        for slot in self._slots:
            jail.render_config(slot.mac, config["mem_mib"], config["vcpus"], config.get("guest_seccomp", "on"))

    def _queue(self) -> asyncio.Queue:
        if self._free is None:
            self._free = asyncio.Queue()
            for slot in self._slots:
                self._free.put_nowait(slot)
        return self._free

    def free_slots(self) -> int:
        return self._queue().qsize()

    def quarantined(self) -> list[str]:
        return list(self._quarantined)

    def _validate(self, request: dict) -> str | None:
        for key, typ in REQUEST_KEYS.items():
            if key not in request or not isinstance(request[key], typ) or isinstance(request[key], bool):
                return f"missing or invalid {key}"
        if request["timeout"] < 1 or request["cpu_timeout"] < 1 or request["output_bytes"] < 1:
            return "limits must be positive"
        max_timeout = self.config.get("max_timeout", 300)
        if request["timeout"] > max_timeout or request["cpu_timeout"] > max_timeout:
            return "timeout too large"
        if request["output_bytes"] > protocol.MAX_REPLY_BYTES // 2:
            return "output_bytes too large"
        # Measured as encoded, so escaping cannot push the guest request,
        # which adds the environment and limits, past MAX_REQUEST_BYTES.
        if len(protocol.encode(request["code"])) > protocol.MAX_REQUEST_BYTES - GUEST_REQUEST_OVERHEAD:
            return "code too large"
        return None

    async def run_job(self, request: dict) -> dict:
        problem = self._validate(request)
        if problem:
            reply = _error("bad_request", problem)
            self._log_outcome(None, reply)
            return reply
        queue = self._queue()
        try:
            slot = queue.get_nowait()
        except asyncio.QueueEmpty:
            reply = _error("busy", "no free worker slot")
            self._log_outcome(None, reply)
            return reply
        unit = f"magma-fc@{slot.name}.service"
        try:
            reply = await self._run_on_slot(slot, unit, request)
        except Exception:
            log.exception("job failed on %s", slot.name)
            reply = _error("worker_failed", "internal error")
        finally:
            try:
                clean = await self._release(slot)
            except Exception:
                log.exception("release failed for %s; quarantining", slot.name)
                clean = False
            if clean:
                queue.put_nowait(slot)
            else:
                self._quarantined.append(slot.name)
        self._log_outcome(slot.name, reply)
        return reply

    def _log_outcome(self, slot_name: str | None, reply: dict) -> None:
        log.info(
            "job on %s: %s (%d free, %d quarantined)",
            slot_name or "-", reply.get("error", "ok"), self.free_slots(), len(self._quarantined),
        )

    async def _release(self, slot: jail.Slot) -> bool:
        """Stop the slot's unit and wait for its cgroup to drain.

        Returns True only once it is safe to restage this slot: the unit
        stopped, its cgroup emptied within STOP_GRACE, and cleanup succeeded.
        Anything else leaves the slot quarantined rather than reused, since
        jail.stage() has no liveness check of its own.
        """
        unit = f"magma-fc@{slot.name}.service"
        rc, out = await self.systemctl(["stop", unit])
        if rc != 0:
            log.error("stop %s failed rc=%s: %s", unit, rc, out.strip())
            return False
        deadline = time.monotonic() + STOP_GRACE
        while await self.cgroup_populated(unit):
            if time.monotonic() > deadline:
                log.error("%s cgroup still populated after stop; quarantining %s", unit, slot.name)
                return False
            await asyncio.sleep(0.2)
        try:
            await asyncio.to_thread(jail.cleanup, self.config["jail_base"], slot.name)
        except OSError:
            log.exception("cleanup failed for %s; quarantining", slot.name)
            return False
        return True

    async def reset(self) -> None:
        """Stop and drain every configured slot before any job can claim one.

        A slot left running by a previous supervisor process must not be
        queued until its cgroup is confirmed empty, for the same reason
        _release() checks before a slot goes back in the queue.
        """
        clean = []
        for slot in self._slots:
            try:
                ok = await self._release(slot)
            except Exception:
                log.exception("release failed for %s during reset; quarantining", slot.name)
                ok = False
            if ok:
                clean.append(slot)
            else:
                self._quarantined.append(slot.name)
        self._free = asyncio.Queue()
        for slot in clean:
            self._free.put_nowait(slot)

    async def _run_on_slot(self, slot: jail.Slot, unit: str, request: dict) -> dict:
        cfg = self.config
        root = await asyncio.to_thread(
            jail.stage, cfg["jail_base"], slot, self.images, cfg["mem_mib"], cfg["vcpus"], cfg["uid"], cfg["gid"],
            cfg.get("guest_seccomp", "on"),
        )
        rc, out = await self.systemctl(["start", unit])
        if rc != 0:
            log.error("start %s failed rc=%s: %s", unit, rc, out.strip())
            return _error("worker_failed", "unit start failed")
        uds = os.path.join(root, jail.UDS_PATH.lstrip("/"))
        try:
            reader, writer = await vsock.connect(
                uds, protocol.AGENT_PORT, time.monotonic() + cfg["boot_timeout"], expected_uid=cfg["uid"]
            )
        except vsock.VsockError as exc:
            log.error("vsock connect for %s failed: %s", unit, exc)
            return _error("worker_failed", "guest did not come up")
        guest_request = {
            "code": request["code"],
            "env": guest_environment(),
            "magma_exe": f"{GUEST_MAGMA_ROOT}/magma.exe",
            "timeout": request["timeout"],
            "cpu_timeout": request["cpu_timeout"],
            "output_bytes": request["output_bytes"],
        }
        try:
            reply = await asyncio.wait_for(
                self._exchange(reader, writer, guest_request), timeout=request["timeout"] + 5
            )
        except asyncio.TimeoutError:
            log.error("guest did not reply before the deadline on %s", slot.name)
            return _error("worker_failed", "guest did not reply before the deadline", timed_out=True)
        except OSError as exc:
            log.error("guest connection for %s failed: %s", unit, exc)
            return _error("worker_failed", "guest connection failed")
        except protocol.FrameError as exc:
            log.error("bad reply from guest on %s: %s", unit, exc)
            return _error("worker_failed", "bad reply from guest")
        finally:
            writer.close()
        return self._bound(reply, request["output_bytes"])

    @staticmethod
    async def _exchange(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, guest_request: dict) -> dict:
        await protocol.write_frame(writer, guest_request)
        return await protocol.read_frame(reader, protocol.MAX_REPLY_BYTES)

    @staticmethod
    def _bound(reply: dict, output_bytes: int) -> dict:
        stdout = str(reply.get("stdout", ""))
        stderr = str(reply.get("stderr", ""))
        truncated = bool(reply.get("truncated", False))
        if len(stdout.encode("utf-8")) > output_bytes:
            stdout = stdout.encode("utf-8")[:output_bytes].decode("utf-8", errors="ignore")
            truncated = True
        if len(stderr.encode("utf-8")) > 64 * 1024:
            stderr = stderr.encode("utf-8")[: 64 * 1024].decode("utf-8", errors="ignore")
            truncated = True
        exit_code = reply.get("exit_code", -1)
        log_lines = reply.get("seccomp_log", [])
        if not isinstance(log_lines, list):
            log_lines = []
        return protocol.fit_reply({
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else -1,
            "timed_out": bool(reply.get("timed_out", False)),
            "truncated": truncated,
            "seccomp_killed": bool(reply.get("seccomp_killed", False)),
            "seccomp_mode": str(reply.get("seccomp_mode", ""))[:16],
            "seccomp_log": [str(line)[:200] for line in log_lines[:20]],
        })


async def _send_reply(writer: asyncio.StreamWriter, reply: dict) -> None:
    """Write one reply frame and close, aborting a peer that will not read it."""
    try:
        await asyncio.wait_for(protocol.write_frame(writer, reply), timeout=REPLY_WRITE_TIMEOUT)
    except (asyncio.TimeoutError, OSError):
        writer.transport.abort()
    finally:
        writer.close()


async def serve(runner: Runner, path: str, group: str | None):
    # Counted from accept, so clients still sending a request or not yet
    # reading their reply count against the cap as well as running jobs.
    max_connections = runner.config.get("max_connections", 2 * len(runner.config["slots"]) + 2)
    active = 0

    async def handle(reader, writer):
        nonlocal active
        if active >= max_connections:
            writer.write(protocol.pack(_error("busy", "too many connections")))
            writer.close()
            return
        active += 1
        try:
            try:
                request = await asyncio.wait_for(protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES), timeout=10)
            except (asyncio.TimeoutError, OSError):
                writer.transport.abort()
                return
            except protocol.FrameError as exc:
                await _send_reply(writer, _error("bad_request", str(exc)))
                return
            try:
                reply = await runner.run_job(request)
            except Exception:
                log.exception("unhandled error running job")
                reply = _error("worker_failed", "internal error")
            await _send_reply(writer, reply)
        finally:
            active -= 1
            writer.close()

    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(handle, path=path)
    os.chmod(path, 0o660)
    if group:
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
    return server


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    with open(os.environ["MAGMA_FC_CONFIG"], encoding="utf-8") as fh:
        config = json.load(fh)
    max_timeout = config.get("max_timeout", 300)
    runtime_max_sec = config.get("runtime_max_sec", 150)
    if max_timeout + config["boot_timeout"] + 5 >= runtime_max_sec:
        log.warning(
            "max_timeout (%s) + boot_timeout (%s) + 5 >= the unit's RuntimeMaxSec (%s); "
            "a job near max_timeout can be killed by systemd before the supervisor replies",
            max_timeout, config["boot_timeout"], runtime_max_sec,
        )
    os.makedirs(config["jail_base"], mode=0o750, exist_ok=True)
    runner = Runner(config)
    await runner.reset()
    server = await serve(runner, config["socket"], config.get("socket_group"))
    log.info(
        "listening on %s with %d free slots (%d quarantined)",
        config["socket"], runner.free_slots(), len(runner.quarantined()),
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
