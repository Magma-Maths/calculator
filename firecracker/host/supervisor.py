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
import signal
import time

from app.magma_cmd import MAGMA_CONSTANT_ENV, magma_environment
from firecracker import protocol
from firecracker.host import jail, vsock

log = logging.getLogger("magma-fc")

GUEST_MAGMA_ROOT = "/opt/magma/current"
STOP_GRACE = 8.0
REPLY_WRITE_TIMEOUT = 10.0
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


class _Vm:
    """A booted guest whose agent has accepted the host's vsock connection."""

    def __init__(self, started: float, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.started = started
        self.reader = reader
        self.writer = writer


class _Next:
    """A free slot's next guest; ready resolves to a _Vm, or None if its boot failed."""

    def __init__(self):
        self.ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self.claimed = asyncio.Event()


class Runner:
    def __init__(self, config: dict, systemctl=real_systemctl, cgroup_populated=real_cgroup_populated):
        self.config = config
        self.systemctl = systemctl
        self.cgroup_populated = cgroup_populated
        self.images = jail.Images(**config["images"])
        self._free: list[jail.Slot] | None = None
        self._slots = [jail.Slot(s["name"], s["mac"]) for s in config["slots"]]
        self._quarantined: list[str] = []
        self._next: dict[str, _Next] = {}
        self._tasks: set[asyncio.Task] = set()
        self._threads: set[asyncio.Future] = set()
        self._preboot = False
        # Fail fast on a bad slot MAC (jail.render_config raises ValueError)
        # instead of only discovering it mid-job when staging that slot.
        for slot in self._slots:
            jail.render_config(slot.mac, config["mem_mib"], config["vcpus"], config.get("guest_seccomp", "on"))

    def _free_list(self) -> list[jail.Slot]:
        if self._free is None:
            self._free = list(self._slots)
        return self._free

    def free_slots(self) -> int:
        return len(self._free_list())

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
        if not protocol.code_fits(request["code"]):
            return protocol.CODE_TOO_LARGE
        return None

    def _claim(self) -> tuple[jail.Slot, _Next | None] | None:
        """Take a free slot, preferring one whose guest is ready, then one still booting."""
        free = self._free_list()
        if not free:
            return None

        def rank(slot: jail.Slot) -> int:
            nxt = self._next.get(slot.name)
            if nxt is None:
                return 2
            if not nxt.ready.done():
                return 1
            return 0 if nxt.ready.result() is not None else 2

        slot = min(free, key=rank)
        free.remove(slot)
        nxt = self._next.pop(slot.name, None)
        if nxt is not None:
            nxt.claimed.set()
        return slot, nxt

    async def run_job(self, request: dict) -> dict:
        problem = self._validate(request)
        if problem:
            reply = _error("bad_request", problem)
            self._log_outcome(None, reply)
            return reply
        claimed = self._claim()
        if claimed is None:
            reply = _error("busy", "no free worker slot")
            self._log_outcome(None, reply)
            return reply
        slot, nxt = claimed
        try:
            reply = await self._run_on_slot(slot, nxt, request)
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
                self._add_free(slot)
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
            await self._in_thread(jail.cleanup, self.config["jail_base"], slot.name)
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
        self._free = clean

    async def start(self) -> None:
        """reset(), then boot every free slot's guest ahead of its first job."""
        await self.reset()
        self._preboot = True
        for slot in self._free_list():
            self._prepare(slot)

    async def shutdown(self) -> None:
        """Stop booting replacements and tear down every idle guest."""
        self._preboot = False
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._threads:
            await asyncio.wait(self._threads)
        for slot in list(self._free_list()):
            nxt = self._next.pop(slot.name, None)
            if nxt is not None and nxt.ready.done() and nxt.ready.result() is not None:
                nxt.ready.result().writer.close()
            if not await self._release(slot):
                self._free_list().remove(slot)
                self._quarantined.append(slot.name)

    async def _in_thread(self, fn, *args):
        """Run fn in a thread that shutdown() waits for even if the caller was cancelled."""
        fut = asyncio.get_running_loop().run_in_executor(None, fn, *args)
        self._threads.add(fut)
        fut.add_done_callback(self._thread_done)
        return await asyncio.shield(fut)

    def _thread_done(self, fut: asyncio.Future) -> None:
        self._threads.discard(fut)
        if not fut.cancelled():
            fut.exception()  # retrieved here when the awaiting caller was cancelled

    def _add_free(self, slot: jail.Slot) -> None:
        self._free_list().append(slot)
        if self._preboot:
            self._prepare(slot)

    def _prepare(self, slot: jail.Slot) -> None:
        nxt = _Next()
        self._next[slot.name] = nxt
        task = asyncio.create_task(self._keep_warm(slot, nxt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _idle_limit(self) -> float:
        # RuntimeMaxSec counts idle time too, so a guest must be handed off
        # early enough for the longest job to finish before systemd kills it.
        cfg = self.config
        limit = cfg.get("runtime_max_sec", 150) - cfg.get("max_timeout", 300) - 5
        return max(limit, cfg["boot_timeout"])

    async def _keep_warm(self, slot: jail.Slot, nxt: _Next) -> None:
        """Boot the free slot's next guest and replace it if it ages out unclaimed.

        A job that claims the slot takes ownership, including releasing a
        failed boot; until then this task owns it.
        """
        try:
            while True:
                try:
                    vm = await self._boot(slot)
                except Exception:
                    log.exception("boot failed on %s", slot.name)
                    vm = None
                if not isinstance(vm, _Vm):
                    if not nxt.claimed.is_set():
                        await self._release_idle(slot)
                    return
                nxt.ready.set_result(vm)
                age = time.monotonic() - vm.started
                try:
                    await asyncio.wait_for(nxt.claimed.wait(), self._idle_limit() - age)
                    return
                except asyncio.TimeoutError:
                    pass
                if nxt.claimed.is_set():
                    return
                nxt.ready = asyncio.get_running_loop().create_future()
                vm.writer.close()
                if not await self._release_idle(slot):
                    return
        finally:
            if not nxt.ready.done():
                nxt.ready.set_result(None)

    async def _release_idle(self, slot: jail.Slot) -> bool:
        """Release a free slot nobody has claimed; quarantine it if that fails."""
        try:
            clean = await self._release(slot)
        except Exception:
            log.exception("release failed for %s; quarantining", slot.name)
            clean = False
        if not clean and slot in self._free_list():
            self._free_list().remove(slot)
            self._next.pop(slot.name, None)
            self._quarantined.append(slot.name)
            log.info("idle %s quarantined (%d free, %d quarantined)", slot.name, self.free_slots(), len(self._quarantined))
        return clean

    async def _boot(self, slot: jail.Slot) -> _Vm | dict:
        """Stage the slot, start its unit and connect to the guest agent; an error reply if that fails."""
        cfg = self.config
        unit = f"magma-fc@{slot.name}.service"
        root = await self._in_thread(
            jail.stage, cfg["jail_base"], slot, self.images, cfg["mem_mib"], cfg["vcpus"], cfg["uid"], cfg["gid"],
            cfg.get("guest_seccomp", "on"),
        )
        started = time.monotonic()
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
        return _Vm(started, reader, writer)

    def _usable(self, vm: _Vm, request: dict) -> bool:
        age = time.monotonic() - vm.started
        return not vm.reader.at_eof() and age + request["timeout"] + 5 < self.config.get("runtime_max_sec", 150)

    @staticmethod
    async def _wait_ready(nxt: _Next) -> _Vm | None:
        """Await the slot's boot without letting a cancelled job cancel it.

        On cancellation the boot still settles and its connection is closed
        before run_job() releases the slot.
        """
        try:
            await asyncio.wait([nxt.ready])
        except asyncio.CancelledError:
            await asyncio.wait([nxt.ready])
            if isinstance(nxt.ready.result(), _Vm):
                nxt.ready.result().writer.close()
            raise
        return nxt.ready.result()

    async def _run_on_slot(self, slot: jail.Slot, nxt: _Next | None, request: dict) -> dict:
        unit = f"magma-fc@{slot.name}.service"
        # Retrying a boot the job already waited on could stack two boots and
        # a release ahead of a full-length job, past the API's read deadline.
        waited = nxt is not None and not nxt.ready.done()
        vm = await self._wait_ready(nxt) if nxt is not None else None
        if waited and (vm is None or not self._usable(vm, request)):
            if vm is not None:
                vm.writer.close()
            return _error("worker_failed", "guest did not come up")
        if nxt is not None and (vm is None or not self._usable(vm, request)):
            # The job has not reached the guest yet, so one retry on a fresh boot is safe.
            log.warning("next guest on %s is not usable; booting a fresh one", slot.name)
            if vm is not None:
                vm.writer.close()
            if not await self._release(slot):
                return _error("worker_failed", "guest did not come up")
            vm = None
        if vm is None:
            vm = await self._boot(slot)
            if not isinstance(vm, _Vm):
                return vm
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
                self._exchange(vm.reader, vm.writer, guest_request), timeout=request["timeout"] + 5
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
            vm.writer.close()
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
    await runner.start()
    server = await serve(runner, config["socket"], config.get("socket_group"))
    log.info(
        "listening on %s with %d free slots (%d quarantined)",
        config["socket"], runner.free_slots(), len(runner.quarantined()),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()
    await runner.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
