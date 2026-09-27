import os
import secrets
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/fixtures/containment_probe.c"


@pytest.fixture(scope="module")
def probe():
    scratch = ROOT / "_worktrees"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-cli-", dir=scratch) as directory:
        executable = Path(directory) / "magma.exe"
        subprocess.run(
            ["cc", "-O2", "-static", "-pthread", "-Wall", "-Wextra", "-Werror",
             str(SOURCE), "-o", str(executable)],
            check=True, capture_output=True, timeout=30,
        )
        yield executable


def invoke(probe: Path, request: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(probe), "-w", "-n"], input=f"CALC_PROBE {request}\n".encode(),
        capture_output=True, timeout=3, check=False,
    )


def test_answer(probe):
    result = invoke(probe, "answer")
    assert result.returncode == 0
    assert result.stdout.endswith(b"quit.\nPROBE answer OK 42\n")


def test_tmp_read_rejects_hold_suffix(probe):
    result = invoke(probe, f"tmp_read {secrets.token_hex(8)}:1")
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"invalid probe request\n"


def test_abstract_connect_rejects_hold_suffix(probe):
    result = invoke(probe, f"abstract_connect {secrets.token_hex(8)}:1")
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"invalid probe request\n"


def test_tmp_exec_reports_completed_exec(probe):
    if os.statvfs("/tmp").f_flag & os.ST_NOEXEC:
        pytest.skip("/tmp is mounted noexec")
    nonce = secrets.token_hex(8)
    target = Path(f"/tmp/calc-probe-exec-{nonce}")
    try:
        result = invoke(probe, f"tmp_exec {nonce}")
    finally:
        target.unlink(missing_ok=True)
    assert result.returncode == 0
    assert result.stdout.endswith(
        f"quit.\nPROBE tmp_exec OK nonce={nonce} operation=copy_exec errno=0\n".encode()
    )
