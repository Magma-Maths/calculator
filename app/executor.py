import asyncio
import os
import signal
from dataclasses import dataclass

from app.config import Settings


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    limit_reason: str | None = None
    limit_detail: str | None = None


class ExecutionIOError(Exception):
    pass


def wrap_magma_code(code: str, timeout: int) -> str:
    alarm_timeout = timeout - 1
    return (
        f"Alarm({alarm_timeout});\n"
        f"SetIgnorePrompt(true);\n"
        f"{code}\n"
        f";\n"
        f"quit;\n"
    )


def magma_environment(magma_root: str) -> list[str]:
    """Root-dependent variables the magma launcher script exports for magma.exe.

    The launcher (magma_root/magma) is a shell script and the jail mounts no
    shell and no /usr/bin, so the binary is exec'd directly and gets these
    from nsjail --env instead. The launcher's constant exports are in
    nsjail.cfg.
    """
    root = magma_root.rstrip("/")
    return [
        f"MAGMA_CMD={root}/magma",
        f"MAGMAPASSFILE={root}/magmapassfile",
        f"MAGMA_SYSTEM_SPEC={root}/package/spec",
        f"MAGMA_SYSTEM_PACKAGE_ROOT={root}/package",
        f"MAGMA_LIBRARY_ROOT={root}/libs",
        f"MAGMA_HELP_DIR={root}/InternalHelp",
        f"MAGMA_HTML_DIR={root}/doc/html",
    ]


async def execute_magma(code: str, settings: Settings) -> ExecutionResult:
    wrapped = wrap_magma_code(code, settings.magma_timeout)
    # Resolved per request, like the launcher's readlink -f: Magma opens
    # package and library files lazily through these literal paths, so a
    # session must stay on one tree across a `current` symlink flip while
    # the next session picks up the new tree.
    root = os.path.realpath(settings.magma_root)

    cmd = [
        "nsjail",
        "--config", "/app/nsjail.cfg",
        "--time_limit", str(settings.magma_timeout + 1),
        "--cgroup_mem_max", str(settings.magma_memory_mb * 1024 * 1024),
        "--cgroup_pids_max", str(settings.magma_pids_max),
        "--cgroup_cpu_ms_per_sec", str(settings.magma_cpu_ms_per_sec),
        "--rlimit_cpu", str(settings.magma_cpu_timeout),
    ]
    for var in magma_environment(root):
        cmd += ["--env", var]
    cmd += ["--", f"{root}/magma.exe", "-w", "-n"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.magma_timeout + 2
    capture_limit = settings.magma_capture_bytes
    stderr_limit = 8 * 1024
    captured = 0
    stderr_captured = 0
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    overflow = asyncio.Event()
    limit_detail: str | None = None

    async def read_stream(stream: asyncio.StreamReader, parts: list[bytes], is_stderr: bool):
        nonlocal captured, stderr_captured, limit_detail
        while chunk := await stream.read(4096):
            room = capture_limit - captured
            if is_stderr:
                room = min(room, stderr_limit - stderr_captured)
            keep = min(len(chunk), max(0, room))
            if keep:
                parts.append(chunk[:keep])
                captured += keep
                if is_stderr:
                    stderr_captured += keep
            if keep < len(chunk):
                if limit_detail is None:
                    limit_detail = (
                        "stderr_capture"
                        if is_stderr and stderr_captured == stderr_limit
                        else "combined_capture"
                    )
                overflow.set()
                return

    async def write_input():
        try:
            for offset in range(0, len(input_bytes), 4096):
                proc.stdin.write(input_bytes[offset:offset + 4096])
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        finally:
            proc.stdin.close()

    def kill_group():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close_pipes():
        for fd in (1, 2):
            transport = proc._transport.get_pipe_transport(fd)
            if transport is not None:
                transport.close()

    async def cleanup():
        kill_group()
        proc.stdin.close()
        writer.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=0.5)
        except asyncio.TimeoutError:
            close_pipes()
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=0.5)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Child cleanup exceeded deadline") from exc
        close_pipes()
        for task in (stdout_reader, stderr_reader):
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(writer, stdout_reader, stderr_reader, return_exceptions=True),
                timeout=0.5,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Pipe cleanup exceeded deadline") from exc

    cleanup_task: asyncio.Task | None = None

    async def finish_cleanup():
        nonlocal cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(cleanup())
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        cleanup_task.result()
        if interrupted:
            raise asyncio.CancelledError

    input_bytes = wrapped.encode("utf-8")
    stdout_reader = asyncio.create_task(read_stream(proc.stdout, stdout_parts, False))
    stderr_reader = asyncio.create_task(read_stream(proc.stderr, stderr_parts, True))
    writer = asyncio.create_task(write_input())
    waiter = asyncio.create_task(proc.wait())
    overflow_waiter = asyncio.create_task(overflow.wait())
    work = {stdout_reader, stderr_reader, writer, waiter}
    limit_reason = None
    try:
        while True:
            if overflow.is_set():
                limit_reason = "output_limit"
                break
            if all(task.done() for task in work):
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                limit_reason = "wall_timeout"
                break
            done, _ = await asyncio.wait(
                {task for task in work if not task.done()} | {overflow_waiter}, timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if overflow_waiter in done:
                limit_reason = "output_limit"
                break
            if not done:
                limit_reason = "wall_timeout"
                break
            for task in done & work:
                if task.done() and not task.cancelled():
                    task.result()
        if limit_reason is not None:
            await finish_cleanup()
        else:
            for task in work:
                task.result()
    except asyncio.CancelledError:
        await finish_cleanup()
        raise
    except Exception as exc:
        try:
            await finish_cleanup()
        except Exception as cleanup_exc:
            exc = cleanup_exc
        raise ExecutionIOError("Execution I/O failed") from exc
    finally:
        overflow_waiter.cancel()

    return ExecutionResult(
        stdout=b"".join(stdout_parts).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_parts).decode("utf-8", errors="replace"),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        limit_reason=limit_reason,
        limit_detail=limit_detail,
    )
