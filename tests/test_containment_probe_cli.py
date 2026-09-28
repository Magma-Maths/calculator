import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
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


def test_descendant_flood_reports_child_before_bounded_overflow(probe):
    started = time.monotonic()
    process = subprocess.Popen(
        [str(probe), "-w", "-n"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(
                input=b"CALC_PROBE descendant_flood\n",
                timeout=3,
            )
        except subprocess.TimeoutExpired as exc:
            match = re.search(rb"child_pid=(\d+)", exc.output or b"")
            assert match is not None, exc.output
            os.kill(int(match.group(1)), signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=1)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=1)

    assert time.monotonic() - started < 4
    assert process.returncode == 0
    assert stderr == b""
    assert b"PROBE descendant_flood OK child_pid=" in stdout[:512]
    assert b"hold_ms=8000 observe_ms=2000" in stdout[:512]
    assert len(stdout) > 256 * 1024
    assert len(stdout) <= 1024 * 1024
