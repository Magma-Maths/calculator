import asyncio
import os
import tempfile
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

    # Write wrapped code to temp file
    fd, tmppath = tempfile.mkstemp(suffix=".m", prefix="magma_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(wrapped)

        cmd = [
            "nsjail",
            "--config", "/app/nsjail.cfg",
            "--", "magma", "-w", "-n", tmppath,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
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
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
