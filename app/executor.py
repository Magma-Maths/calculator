import asyncio
import logging
import os
from dataclasses import dataclass

from app.config import Settings
from app.magma_cmd import magma_environment, wrap_magma_code  # noqa: F401 - re-exported
from firecracker import protocol

logger = logging.getLogger(__name__)


class SupervisorBusy(Exception):
    """Every Firecracker slot is in use; the caller should answer 503."""


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False


async def execute_magma(code: str, settings: Settings) -> ExecutionResult:
    wrapped = wrap_magma_code(code, settings.magma_timeout)
    if settings.executor_backend == "firecracker":
        return await execute_via_supervisor(wrapped, settings)
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
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=wrapped.encode("utf-8")),
            timeout=settings.magma_timeout + 2,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ExecutionResult(
            stdout="",
            stderr="Killed",
            exit_code=-1,
        )

    return ExecutionResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        exit_code=proc.returncode or 0,
    )


async def execute_via_supervisor(wrapped: str, settings: Settings) -> ExecutionResult:
    request = {
        "code": wrapped,
        "timeout": settings.magma_timeout,
        "cpu_timeout": settings.magma_cpu_timeout,
        "output_bytes": settings.magma_output_bytes,
    }
    try:
        reader, writer = await asyncio.open_unix_connection(settings.supervisor_socket)
    except OSError:
        return ExecutionResult(stdout="", stderr="worker service unavailable", exit_code=-1)
    try:
        await protocol.write_frame(writer, request)
        reply = await asyncio.wait_for(
            protocol.read_frame(reader, protocol.MAX_REPLY_BYTES),
            timeout=settings.magma_timeout + 60,
        )
    except (asyncio.TimeoutError, protocol.FrameError, OSError) as exc:
        return ExecutionResult(stdout="", stderr=f"worker service error: {exc}", exit_code=-1)
    finally:
        writer.close()
    if reply.get("error") == "busy":
        raise SupervisorBusy()
    if reply.get("seccomp_killed") is True:
        log_lines = reply.get("seccomp_log") or [""]
        logger.warning(
            "guest seccomp filter killed magma (mode=%s): %s", reply.get("seccomp_mode"), log_lines[0]
        )
    return ExecutionResult(
        stdout=str(reply.get("stdout", "")),
        stderr=str(reply.get("stderr", "")),
        exit_code=reply.get("exit_code", -1) if isinstance(reply.get("exit_code"), int) else -1,
        truncated=reply.get("truncated") is True,
    )
