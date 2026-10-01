import os
import stat
import textwrap
import time

import pytest

from firecracker.guest import agent

FAKE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, signal, sys, time
    if os.environ.get("FAKE_SIGSYS") == "1":
        os.kill(os.getpid(), signal.SIGSYS)
    elif os.environ.get("FAKE_NOSTIN") == "1":
        time.sleep(30)
    elif os.environ.get("FAKE_CPU_BURN") == "1":
        while True:
            pass
    elif os.environ.get("FAKE_STDERR_FLOOD") == "1":
        sys.stderr.write("y" * 500_000)
    else:
        data = sys.stdin.read()
        if data.startswith("SLEEP"):
            time.sleep(30)
        elif data.startswith("FLOOD"):
            sys.stdout.write("x" * 500_000)
        elif data.startswith("FAIL"):
            sys.stderr.write("boom\\n")
            sys.exit(3)
        else:
            sys.stdout.write("env:" + os.environ.get("MAGMAPASSFILE", "") + "\\n")
            sys.stdout.write("out:" + data.strip() + "\\n")
    """
)


@pytest.fixture(autouse=True)
def seccomp_off(monkeypatch):
    # The host has no libseccomp bindings; the filter itself is guest-only.
    monkeypatch.setenv("AGENT_SECCOMP_MODE", "off")


@pytest.fixture
def fake_magma(tmp_path):
    exe = tmp_path / "magma.exe"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def _req(exe, code, **over):
    base = {
        "code": code,
        "env": {"MAGMAPASSFILE": "/opt/magma/current/magmapassfile", "PATH": os.environ["PATH"]},
        "magma_exe": exe,
        "timeout": 5,
        "cpu_timeout": 5,
        "output_bytes": 20 * 1024,
    }
    base.update(over)
    return base


def test_run_job_success(fake_magma):
    reply = agent.run_job(_req(fake_magma, "print 1+1;"))
    assert reply["exit_code"] == 0
    assert reply["timed_out"] is False
    assert reply["truncated"] is False
    assert "env:/opt/magma/current/magmapassfile" in reply["stdout"]
    assert "out:print 1+1;" in reply["stdout"]
    assert reply["seccomp_killed"] is False
    assert reply["seccomp_mode"] == "off"
    assert reply["seccomp_log"] == []


def test_run_job_nonzero_exit_and_stderr(fake_magma):
    reply = agent.run_job(_req(fake_magma, "FAIL"))
    assert reply["exit_code"] == 3
    assert reply["stderr"] == "boom\n"


def test_run_job_times_out(fake_magma):
    reply = agent.run_job(_req(fake_magma, "SLEEP", timeout=1))
    assert reply["timed_out"] is True
    assert reply["exit_code"] == -1


def test_run_job_caps_stdout(fake_magma):
    reply = agent.run_job(_req(fake_magma, "FLOOD", output_bytes=1024))
    assert reply["truncated"] is True
    assert len(reply["stdout"].encode()) <= 1024


def test_run_job_rejects_bad_request(fake_magma):
    reply = agent.run_job({"code": "x"})
    assert reply["exit_code"] == -1
    assert "invalid request" in reply["stderr"]


def test_run_job_missing_executable():
    reply = agent.run_job(_req("/nonexistent/magma.exe", "print 1;"))
    assert reply["exit_code"] == -1
    assert "magma.exe" in reply["stderr"]


def test_run_job_stdin_deadlock_prevention(fake_magma):
    """Child that doesn't read stdin; large write should not deadlock."""
    start = time.time()
    reply = agent.run_job(
        _req(fake_magma, "x" * (2 * 1024 * 1024), timeout=1, env={
            **_req(fake_magma, "x").get("env", {}),
            "FAKE_NOSTIN": "1",
        })
    )
    elapsed = time.time() - start
    assert reply["timed_out"] is True
    assert reply["exit_code"] == -1
    assert elapsed < 10


def test_run_job_cpu_timeout_signal(fake_magma):
    """CPU timeout via RLIMIT_CPU should be reported as timed_out."""
    start = time.time()
    reply = agent.run_job(
        _req(fake_magma, "x", cpu_timeout=1, timeout=10, env={
            **_req(fake_magma, "x").get("env", {}),
            "FAKE_CPU_BURN": "1",
        })
    )
    elapsed = time.time() - start
    assert reply["exit_code"] == -1
    assert reply["timed_out"] is True
    assert elapsed < 5


def test_run_job_caps_stderr(fake_magma):
    """Stderr should be capped at STDERR_CAP independently."""
    reply = agent.run_job(
        _req(fake_magma, "x", env={
            **_req(fake_magma, "x").get("env", {}),
            "FAKE_STDERR_FLOOD": "1",
        })
    )
    assert reply["truncated"] is True
    assert len(reply["stderr"].encode()) <= 64 * 1024


def test_run_job_reports_seccomp_kill(fake_magma):
    reply = agent.run_job(_req(fake_magma, "x", env={**_req(fake_magma, "x")["env"], "FAKE_SIGSYS": "1"}))
    assert reply["exit_code"] == -1
    assert reply["timed_out"] is False
    assert reply["seccomp_killed"] is True
    assert reply["seccomp_mode"] == "off"
