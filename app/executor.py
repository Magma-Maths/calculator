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

    cmd = [
        "nsjail",
        "--config", "/app/nsjail.cfg",
        "--time_limit", str(settings.magma_timeout + 1),
        "--cgroup_mem_max", str(settings.magma_memory_mb * 1024 * 1024),
        "--rlimit_cpu", str(settings.magma_cpu_timeout),
    ]
    for var in magma_environment(settings.magma_root):
        cmd += ["--env", var]
    cmd += ["--", f"{settings.magma_root.rstrip('/')}/magma.exe", "-w", "-n"]

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
