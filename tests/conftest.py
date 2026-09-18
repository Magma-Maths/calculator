import asyncio
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.executor import ExecutionResult, wrap_magma_code

FAKE_MAGMA = str(Path(__file__).parent / "fake_magma.py")
FAKE_NSJAIL = str(Path(__file__).parent / "fake_nsjail.py")

# Check if real Magma is available on the system
HAS_MAGMA = shutil.which("magma") is not None

_FAKE_NSJAIL_CMD = f"""import runpy
runpy.run_path({FAKE_NSJAIL!r}, run_name="__main__")
"""


def _fake_magma_exe(record: Path) -> str:
    """Stand-in for magma.exe that records its argv and environment at `record`.

    Like the real binary it will not start without its license file, which
    the launcher script normally exports as MAGMAPASSFILE.
    """
    return f"""import json, os, runpy, sys
if not os.path.isfile(os.environ.get("MAGMAPASSFILE", "")):
    sys.exit("magma.exe: MAGMAPASSFILE is not set or does not exist")
with open({str(record)!r}, "w") as f:
    json.dump({{"argv": sys.argv, "env": dict(os.environ)}}, f)
runpy.run_path({FAKE_MAGMA!r}, run_name="__main__")
"""


def _install_script(path: Path, body: str) -> None:
    # The jail drops the caller's environment, so the interpreter is pinned
    # by absolute path rather than found through PATH.
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


@pytest.fixture
def magma_launch(tmp_path) -> Path:
    """Where the fake magma.exe records the argv and environment it started with."""
    return tmp_path / "magma-launch.json"


def _fake_magma_tree(tmp_path: Path, record: Path) -> Path:
    """Copy of the real install layout (magma.exe next to magmapassfile)."""
    tree = tmp_path / "magma-2.29-10"
    tree.mkdir()
    (tree / "magmapassfile").write_text("")
    _install_script(tree / "magma.exe", _fake_magma_exe(record))
    return tree


def _jailed_client(tmp_path: Path, monkeypatch, magma_root: Path):
    """POST /execute through the real execute_magma with a fake nsjail on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_script(bin_dir / "nsjail", _FAKE_NSJAIL_CMD)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MAGMA_ROOT", str(magma_root))
    from app import main
    monkeypatch.setattr(main, "settings", Settings())
    from fastapi.testclient import TestClient
    return TestClient(main.app)


@pytest.fixture
def jailed_magma(tmp_path, monkeypatch, magma_launch):
    """MAGMA_ROOT points straight at the fake install tree."""
    return _jailed_client(tmp_path, monkeypatch, _fake_magma_tree(tmp_path, magma_launch))


@pytest.fixture
def jailed_magma_via_symlink(tmp_path, monkeypatch, magma_launch):
    """MAGMA_ROOT is a `current` symlink to the fake install tree, as on the host."""
    current = tmp_path / "current"
    current.symlink_to(_fake_magma_tree(tmp_path, magma_launch))
    return _jailed_client(tmp_path, monkeypatch, current)


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
