import asyncio
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.executor import ExecutionResult, wrap_magma_code

FAKE_MAGMA = str(Path(__file__).parent / "fake_magma.py")

# Check if real Magma is available on the system
HAS_MAGMA = shutil.which("magma") is not None


async def _execute_with_fake_magma(code: str, settings: Settings) -> ExecutionResult:
    """Run code through fake_magma.py instead of nsjail + real Magma."""
    wrapped = wrap_magma_code(code, settings.magma_timeout)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, FAKE_MAGMA,
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
        return ExecutionResult(stdout="", stderr="Killed", exit_code=-1)

    return ExecutionResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        exit_code=proc.returncode or 0,
    )


@pytest.fixture
def fake_magma():
    """Monkeypatch execute_magma to use fake_magma.py."""
    with patch("app.main.execute_magma", side_effect=_execute_with_fake_magma):
        from app.main import app
        from fastapi.testclient import TestClient
        yield TestClient(app)


async def _execute_with_real_magma(code: str, settings: Settings) -> ExecutionResult:
    """Run code through real Magma binary (without nsjail)."""
    wrapped = wrap_magma_code(code, settings.magma_timeout)

    proc = await asyncio.create_subprocess_exec(
        "magma", "-w", "-n",
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
        return ExecutionResult(stdout="", stderr="Killed", exit_code=-1)

    return ExecutionResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        exit_code=proc.returncode or 0,
    )


@pytest.fixture
def real_magma():
    """Use real Magma binary (skips if not available)."""
    if not HAS_MAGMA:
        pytest.skip("Magma not available")
    with patch("app.main.execute_magma", side_effect=_execute_with_real_magma):
        from app.main import app
        from fastapi.testclient import TestClient
        yield TestClient(app)
