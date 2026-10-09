import asyncio
import logging
import os
import signal
from dataclasses import dataclass

from app.config import Settings
from app.magma_cmd import magma_environment, wrap_magma_code  # noqa: F401 - re-exported
from firecracker import protocol

logger = logging.getLogger(__name__)


class SupervisorBusy(Exception):
    """Every Firecracker slot is in use; the caller should answer 503."""


class SupervisorUnavailable(Exception):
    """The supervisor socket refused or dropped the connection attempt; answer 503."""


class InputTooLargeForWorker(Exception):
    """The code fits MAGMA_INPUT_KB but not the worker frame once JSON-escaped; answer 413."""

SECCOMP_POLICY = "/app/security/seccomp/magma.kafel"
SECCOMP_KILLED = "killed by seccomp policy"
# nsjail exits with 128 + signal when the jailed process dies by a signal,
# and logs one of these lines at INFO on the stderr it shares with Magma.
# Magma can exit 159 and print the SIGSYS line itself, but it cannot stop
# nsjail logging a normal exit afterwards.
NSJAIL_EXIT_SIGSYS = 128 + signal.SIGSYS
NSJAIL_LOG_SIGSYS = "terminated with signal: SIGSYS (31)"
NSJAIL_LOG_EXITED = "exited with status: "
# Shared with app/parser.py so the warning it derives from stderr text and
# the executor's own `timed_out` flag never disagree about what counts.
TIMEOUT_STDERR_MARKERS = ("Alarm clock", "Cputime limit exceeded", "Killed")


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False
    timed_out: bool = False
    seccomp_killed: bool = False
    # Firecracker only: measured by the guest agent independently of
    # stdout, since the footer they would otherwise come from is lost
    # whenever output is truncated or the job is killed. nsjail has only
    # ever had the footer.
    cpu_time_sec: float | None = None
    peak_memory_kb: int | None = None


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
    if settings.jail_seccomp:
        cmd += ["--seccomp_policy", SECCOMP_POLICY]
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
            timed_out=True,
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if (
        settings.jail_seccomp
        and proc.returncode == NSJAIL_EXIT_SIGSYS
        and NSJAIL_LOG_SIGSYS in stderr
        and NSJAIL_LOG_EXITED not in stderr
    ):
        return ExecutionResult(
            stdout=stdout,
            stderr=f"{SECCOMP_KILLED}\n{stderr}",
            exit_code=-1,
            seccomp_killed=True,
        )
    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode or 0,
        timed_out=any(marker in stderr for marker in TIMEOUT_STDERR_MARKERS),
    )


async def execute_via_supervisor(wrapped: str, settings: Settings) -> ExecutionResult:
    request = {
        "code": wrapped,
        "timeout": settings.magma_timeout,
        "cpu_timeout": settings.magma_cpu_timeout,
        "output_bytes": settings.magma_output_bytes,
    }
    if not protocol.code_fits(wrapped):
        raise InputTooLargeForWorker()
    try:
        reader, writer = await asyncio.open_unix_connection(settings.supervisor_socket)
    except OSError as exc:
        raise SupervisorUnavailable() from exc
    try:
        await protocol.write_frame(writer, request)
        reply = await asyncio.wait_for(
            protocol.read_frame(reader, protocol.MAX_REPLY_BYTES),
            timeout=settings.magma_timeout + 60,
        )
    except (asyncio.TimeoutError, protocol.FrameError, OSError) as exc:
        return ExecutionResult(stdout="", stderr=f"worker service error: {exc}", exit_code=-1)
    finally:
        await protocol.close_writer(writer)
    if reply.get("error") == "busy":
        raise SupervisorBusy()
    if reply.get("error") == "bad_request" and reply.get("stderr") == protocol.CODE_TOO_LARGE:
        raise InputTooLargeForWorker()
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
        timed_out=reply.get("timed_out") is True,
        seccomp_killed=reply.get("seccomp_killed") is True,
        # Bounded again here, defensively, rather than trusting that the
        # supervisor already did: the same untrusted guest value, one hop
        # later.
        cpu_time_sec=protocol.bounded_cpu_time_sec(reply.get("cpu_time_sec")),
        peak_memory_kb=protocol.bounded_memory_kb(reply.get("peak_memory_kb")),
    )
