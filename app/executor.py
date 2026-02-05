import asyncio
from dataclasses import dataclass

from app.config import Settings


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


def wrap_magma_code(code: str, timeout: int) -> str:
    alarm_timeout = timeout - 1
    return (
        f"Alarm({alarm_timeout});\n"
        f"SetIgnorePrompt(true);\n"
        f"{code}\n"
        f";\n"
        f"quit;\n"
    )


async def execute_magma(code: str, settings: Settings) -> ExecutionResult:
    wrapped = wrap_magma_code(code, settings.magma_timeout)

    cmd = [
        "nsjail",
        "--config", "/app/nsjail.cfg",
        "--time_limit", str(settings.magma_timeout + 1),
        "--cgroup_mem_max", str(settings.magma_memory_mb * 1024 * 1024),
        "--rlimit_cpu", str(settings.magma_cpu_timeout),
        "--", "magma", "-w", "-n",
    ]

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
