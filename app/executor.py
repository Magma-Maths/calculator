import asyncio
import os
from dataclasses import dataclass

from app.config import Settings
from app.magma_cmd import magma_environment, wrap_magma_code  # noqa: F401 - re-exported


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


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
