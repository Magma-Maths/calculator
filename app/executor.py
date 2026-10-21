import asyncio
import logging
import time
from dataclasses import dataclass

from app.config import Settings
from app.magma_cmd import wrap_magma_code
from app.parser import parse_magma_output
from firecracker import protocol

logger = logging.getLogger(__name__)
magma_version: str | None = None


class SupervisorBusy(Exception):
    """Every Firecracker slot is in use; the caller should answer 503."""


class SupervisorUnavailable(Exception):
    """The supervisor socket refused or dropped the connection attempt; answer 503."""


class InputTooLargeForWorker(Exception):
    """The code fits MAGMA_INPUT_KB but not the worker frame once JSON-escaped; answer 413."""


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False
    timed_out: bool = False
    seccomp_killed: bool = False
    # Measured by the guest agent independently of stdout, since the footer
    # they would otherwise come from is lost whenever output is truncated or
    # the job is killed.
    cpu_time_sec: float | None = None
    peak_memory_kb: int | None = None


METADATA_CACHE_SECONDS = 10
_metadata_lock = asyncio.Lock()
_metadata: dict = {}


async def supervisor_metadata(settings: Settings) -> dict:
    """The supervisor's limits, or {} when unavailable, fetched at most once per
    METADATA_CACHE_SECONDS. Each fetch holds one of the supervisor connections
    that jobs need, so concurrent callers wait for one fetch and share its result.
    """
    async with _metadata_lock:
        if _metadata and time.monotonic() - _metadata["at"] < METADATA_CACHE_SECONDS:
            return _metadata["reply"]
        reply = await _fetch_metadata(settings)
        _metadata.update(at=time.monotonic(), reply=reply)
        return reply


async def _fetch_metadata(settings: Settings) -> dict:
    async def exchange():
        reader, writer = await asyncio.open_unix_connection(settings.supervisor_socket)
        try:
            await protocol.write_frame(writer, {"operation": "metadata"})
            return await protocol.read_frame(reader, 4096)
        finally:
            await protocol.close_writer(writer)

    try:
        return await asyncio.wait_for(exchange(), timeout=2)
    except (OSError, asyncio.TimeoutError, protocol.FrameError):
        return {}


async def execute_magma(code: str, settings: Settings) -> ExecutionResult:
    global magma_version
    wrapped = wrap_magma_code(code, settings.magma_timeout)
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
    reply_task = asyncio.create_task(asyncio.wait_for(
        protocol.read_frame(reader, protocol.MAX_REPLY_BYTES),
        timeout=settings.magma_timeout + 60,
    ))
    try:
        await protocol.write_frame(writer, request)
        reply = await asyncio.shield(reply_task)
    except asyncio.CancelledError:
        # Half-close requests cancellation while keeping the reply readable.
        # The supervisor replies only after stopping and draining the guest.
        try:
            writer.write_eof()
            await reply_task
        except (asyncio.TimeoutError, protocol.FrameError, OSError):
            pass
        raise
    except (asyncio.TimeoutError, protocol.FrameError, OSError) as exc:
        return ExecutionResult(stdout="", stderr=f"worker service error: {exc}", exit_code=-1)
    finally:
        reply_task.cancel()
        await asyncio.gather(reply_task, return_exceptions=True)
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
    result = ExecutionResult(
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
    version = parse_magma_output(result.stdout, settings.magma_output_bytes).version
    if version is not None:
        magma_version = version
    return result
