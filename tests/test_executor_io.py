import json
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.config import Settings
from app.executor import ExecutionIOError, execute_magma
from app.ratelimit import RateLimiter
from app.usage_logger import UsageLogger


@pytest.mark.parametrize("limits", [
    {"magma_capture_kb": 19},
    {"magma_capture_kb": 0},
    {"magma_output_kb": 0},
    {"magma_pids_max": 0},
    {"magma_cpu_ms_per_sec": 0},
])
def test_executor_rejects_invalid_limits(limits):
    with pytest.raises(ValidationError):
        Settings(**limits)


def _client(tmp_path, monkeypatch, body, *, flag_file=None, **settings_overrides):
    root = tmp_path / "magma"
    root.mkdir()
    executable = root / "magma.exe"
    executable.write_text(f"#!{sys.executable}\n{body}")
    executable.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_nsjail = Path(__file__).parent / "fake_nsjail.py"
    nsjail = bin_dir / "nsjail"
    record_flags = (
        f"import json, sys\nopen({str(flag_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        if flag_file is not None else ""
    )
    nsjail.write_text(
        f"#!{sys.executable}\n{record_flags}import runpy\n"
        f"runpy.run_path({str(fake_nsjail)!r}, run_name='__main__')\n"
    )
    nsjail.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(main, "settings", Settings(magma_root=str(root), **settings_overrides))
    monkeypatch.setattr(main, "rate_limiter", RateLimiter(per_minute=1000, per_hour=1000))
    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(main, "usage_logger", UsageLogger(str(usage_path)))
    return TestClient(main.app, backend_options={"use_uvloop": True}), usage_path


def _entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_early_exit_uvloop_transport(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, "import sys\nsys.exit(7)\n")
    for _ in range(20):
        response = client.post("/execute", json={"code": "x" * (32 * 1024)})
        assert response.status_code == 200
        assert response.json()["exit_code"] == 7


def test_failed_child_unknown_stderr(tmp_path, monkeypatch):
    client, usage_path = _client(
        tmp_path, monkeypatch,
        "import sys\nsys.stderr.write('startup failed\\n')\nsys.exit(23)\n",
    )
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["exit_code"] == 23
    assert "startup failed" in data["error"]
    assert "23" in data["error"]
    start, end = _entries(usage_path)
    assert start["request_id"] == end["request_id"]
    assert end["event"] == "end"
    assert end["success"] is False
    assert "startup failed" in end["error"]


def test_failed_child_diagnostic_is_single_line_and_bounded(tmp_path, monkeypatch):
    body = (
        "import sys\n"
        "sys.stderr.write('\\x1b[31mfailed\\x1b[0m\\n' + 'x' * 5000)\n"
        "sys.exit(4)\n"
    )
    client, usage_path = _client(tmp_path, monkeypatch, body)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    error = response.json()["error"]
    assert "failed" in error
    assert "\x1b" not in error
    assert "\n" not in error
    assert len(error.encode("utf-8")) <= 2100
    assert error == _entries(usage_path)[1]["error"]


def test_failed_child_diagnostic_removes_osc_sequence(tmp_path, monkeypatch):
    body = (
        "import sys\n"
        "sys.stderr.write('start\\x1b]8;;https://example.invalid\\x07end')\n"
        "sys.exit(4)\n"
    )
    client, _ = _client(tmp_path, monkeypatch, body)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    error = response.json()["error"]
    assert "startend" in error
    assert "example.invalid" not in error


def test_failed_child_without_stderr_names_absence(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, "import sys\nsys.exit(9)\n")
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    assert "no stderr was captured" in response.json()["error"]


def test_launch_failure_has_completion_without_exit_code(tmp_path, monkeypatch):
    client, usage_path = _client(tmp_path, monkeypatch, "pass\n")
    monkeypatch.setenv("PATH", "")
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 503
    assert "exit_code" not in response.json()
    start, end = _entries(usage_path)
    assert start["request_id"] == end["request_id"]
    assert end["success"] is False
    assert "exit_code" not in end


def test_runtime_io_failure_has_completion_without_exit_code(tmp_path, monkeypatch):
    client, usage_path = _client(tmp_path, monkeypatch, "pass\n")

    async def fail_io(code, settings):
        raise ExecutionIOError("closed transport")

    monkeypatch.setattr(main, "execute_magma", fail_io)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 502
    assert "exit_code" not in response.json()
    assert "closed transport" not in response.json()["error"]
    start, end = _entries(usage_path)
    assert start["request_id"] == end["request_id"]
    assert end["success"] is False
    assert "exit_code" not in end


@pytest.mark.parametrize(
    ("stream", "ceiling"),
    [("stdout", "combined"), ("stderr", "stderr")],
)
def test_capture_overflow_reports_ceiling(tmp_path, monkeypatch, stream, ceiling):
    size = 256 * 1024 + 1 if stream == "stdout" else 8 * 1024 + 1
    body = f"import sys\nsys.{stream}.buffer.write(b'x' * {size})\n"
    client, usage_path = _client(tmp_path, monkeypatch, body)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["truncated"] is True
    assert ceiling in data["error"]
    assert ceiling in " ".join(data["warnings"])
    assert len(data["stdout"].encode()) <= 20 * 1024
    assert _entries(usage_path)[1]["success"] is False


def test_exact_capture_boundary_succeeds(tmp_path, monkeypatch):
    client, _ = _client(
        tmp_path, monkeypatch,
        "import sys\nsys.stdout.buffer.write(b'x' * 1024)\n",
        magma_capture_kb=1, magma_output_kb=1,
    )
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["truncated"] is False


def test_executor_passes_process_and_cpu_limits(tmp_path, monkeypatch):
    flag_file = tmp_path / "flags.json"
    client, _ = _client(
        tmp_path, monkeypatch, "import sys\nsys.exit(0)\n",
        flag_file=flag_file, magma_pids_max=17, magma_cpu_ms_per_sec=500,
    )
    assert client.post("/execute", json={"code": "1;"}).status_code == 200
    flags = json.loads(flag_file.read_text())
    assert flags[flags.index("--cgroup_pids_max") + 1] == "17"
    assert flags[flags.index("--cgroup_cpu_ms_per_sec") + 1] == "500"
    assert flags[flags.index("--cgroup_mem_max") + 1] == str(400 * 1024 * 1024)
    assert flags[flags.index("--rlimit_cpu") + 1] == "120"
    assert flags[flags.index("--time_limit") + 1] == "121"


def test_interleaved_streams_within_capture_succeed(tmp_path, monkeypatch):
    body = (
        "import sys\n"
        "for _ in range(4):\n"
        " sys.stdout.buffer.write(b'x' * 1024); sys.stdout.flush()\n"
        " sys.stderr.buffer.write(b'y' * 1024); sys.stderr.flush()\n"
    )
    client, _ = _client(tmp_path, monkeypatch, body)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_multibyte_body_uses_utf8_budget(tmp_path, monkeypatch):
    body = "import sys\nsys.stdout.write('Magma V2.29\\nquit.\\n' + 'é' * 600 + '\\n')\n"
    client, _ = _client(
        tmp_path, monkeypatch, body,
        magma_output_kb=1, magma_capture_kb=2,
    )
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 200
    data = response.json()
    assert data["truncated"] is True
    assert len(data["stdout"].encode("utf-8")) <= 1024
    assert data["stdout"] == "é" * 512


def test_timeout_kills_child_group(tmp_path, monkeypatch):
    marker = tmp_path / "child-pid"
    body = (
        "import os, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(20)\n"
    )
    client, _ = _client(tmp_path, monkeypatch, body, magma_timeout=1)
    started = time.monotonic()
    response = client.post("/execute", json={"code": "1;"})
    assert time.monotonic() - started < 5
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "time limit" in response.json()["error"]
    pid = int(marker.read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_descendant_held_pipe_has_finite_cleanup(tmp_path, monkeypatch):
    marker = tmp_path / "child-pid"
    body = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
    )
    client, _ = _client(tmp_path, monkeypatch, body, magma_timeout=1)
    started = time.monotonic()
    response = client.post("/execute", json={"code": "1;"})
    assert time.monotonic() - started < 5
    assert response.status_code == 200
    assert response.json()["success"] is False
    pid = int(marker.read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_cancellation_reaps_child_group(tmp_path, monkeypatch):
    marker = tmp_path / "child-pid"
    body = (
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(20)\n"
    )
    _client(tmp_path, monkeypatch, body)

    async def run():
        task = asyncio.create_task(execute_magma("1;", main.settings))
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
    pid = int(marker.read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
