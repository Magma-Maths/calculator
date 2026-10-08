import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app import executor
from app.config import Settings
from firecracker import protocol

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # rate_limiter is a module-level singleton shared by the whole pytest
    # session; without this, enough tests posting to /execute from the
    # TestClient's fixed "testclient" IP eventually trips a real 429.
    from app import main
    main.rate_limiter._requests.clear()
    yield


async def _fake_supervisor(path, reply):
    seen = {}

    async def handle(reader, writer):
        seen["request"] = await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES)
        await protocol.write_frame(writer, reply)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=path)
    return server, seen


def _usage_events(tmp_path, monkeypatch):
    """Point main's usage log at a fresh file; returns a reader of (event, status, reason)."""
    from app import main as app_main
    from app.usage_logger import UsageLogger

    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(app_main, "usage_logger", UsageLogger(str(path)))
    return lambda: [(e["event"], e.get("status"), e.get("reason")) for e in map(json.loads, path.read_text().splitlines())]


def _submission_log(tmp_path, monkeypatch):
    """Point main's submission log at a fresh file; returns a reader of its entries."""
    from app import main as app_main
    from app.submission_logger import SubmissionLogger

    path = tmp_path / "submissions.jsonl"
    monkeypatch.setattr(app_main, "submission_logger", SubmissionLogger(str(path)))
    return lambda: [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _settings(tmp_path):
    return Settings(executor_backend="firecracker", supervisor_socket=str(tmp_path / "sup.sock"), magma_timeout=7, magma_output_kb=1)


def test_firecracker_backend_success(tmp_path):
    settings = _settings(tmp_path)

    async def run():
        server, seen = await _fake_supervisor(settings.supervisor_socket, {"stdout": "2\n", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False})
        result = await executor.execute_magma("print 1+1;", settings)
        server.close(); await server.wait_closed()
        return result, seen["request"]

    result, request = asyncio.run(run())
    assert result.stdout == "2\n" and result.exit_code == 0
    assert request["code"].startswith("Alarm(6);")
    assert request["timeout"] == 7 and request["output_bytes"] == 1024


def test_firecracker_backend_busy_raises(tmp_path):
    settings = _settings(tmp_path)

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, {"error": "busy", "stdout": "", "stderr": "no free worker slot", "exit_code": -1, "timed_out": False, "truncated": False})
        try:
            with pytest.raises(executor.SupervisorBusy):
                await executor.execute_magma("1;", settings)
        finally:
            server.close(); await server.wait_closed()

    asyncio.run(run())


def test_firecracker_backend_worker_failed(tmp_path):
    settings = _settings(tmp_path)

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, {"error": "worker_failed", "stdout": "", "stderr": "guest did not come up", "exit_code": -1, "timed_out": False, "truncated": False})
        result = await executor.execute_magma("1;", settings)
        server.close(); await server.wait_closed()
        return result

    result = asyncio.run(run())
    assert result.exit_code == -1 and "guest did not come up" in result.stderr


def test_firecracker_backend_logs_seccomp_kill(tmp_path, caplog):
    settings = _settings(tmp_path)
    reply = {
        "stdout": "", "stderr": "", "exit_code": -1, "timed_out": False, "truncated": False,
        "seccomp_killed": True, "seccomp_mode": "on", "seccomp_log": [],
    }

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, reply)
        result = await executor.execute_magma("1;", settings)
        server.close(); await server.wait_closed()
        return result

    with caplog.at_level("WARNING", logger="app.executor"):
        result = asyncio.run(run())
    assert result == executor.ExecutionResult(
        stdout="", stderr="", exit_code=-1, seccomp_killed=True
    )
    assert "seccomp filter killed magma (mode=on)" in caplog.text


def test_firecracker_backend_reports_timed_out(tmp_path):
    settings = _settings(tmp_path)
    # An infinite loop killed on the supervisor's outer timeout can leave no
    # stderr text at all; only the flag says it was a timeout.
    reply = {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True, "truncated": False}

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, reply)
        result = await executor.execute_magma("while true do end while;", settings)
        server.close(); await server.wait_closed()
        return result

    result = asyncio.run(run())
    assert result.timed_out is True
    assert result.seccomp_killed is False
    assert result.exit_code == -1


def test_main_reports_timed_out_with_no_stderr_text(tmp_path, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app import main as app_main

    settings = _settings(tmp_path)
    reply = {"stdout": "", "stderr": "", "exit_code": -1, "timed_out": True, "truncated": False}
    submission_entries = _submission_log(tmp_path, monkeypatch)
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(_fake_supervisor(settings.supervisor_socket, reply))[0]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(app_main, "settings", settings)
    try:
        body = TestClient(app_main.app).post(
            "/execute", json={"code": "while true do end while;"}
        ).json()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        server.close()
        loop.close()

    # The one warning this produces is also the error, so it is deduped
    # out of "warnings" and shows up only there.
    assert body["success"] is False
    assert "time limit" in body["error"]

    assert submission_entries()[-1]["timed_out"] is True


def test_firecracker_backend_reports_cpu_time_and_memory(tmp_path):
    settings = _settings(tmp_path)
    reply = {
        "stdout": "2\n", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False,
        "cpu_time_sec": 1.5, "peak_memory_kb": 20480,
    }

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, reply)
        result = await executor.execute_magma("print 1+1;", settings)
        server.close(); await server.wait_closed()
        return result

    result = asyncio.run(run())
    assert result.cpu_time_sec == 1.5
    assert result.peak_memory_kb == 20480


@pytest.mark.parametrize(
    "value", [10**400, float("inf"), float("nan"), -1], ids=["huge-int", "inf", "nan", "negative"]
)
def test_firecracker_backend_nulls_untrusted_cpu_time(tmp_path, value):
    # Bypasses the real supervisor (which bounds these too) to check the
    # API's own defensive validation in isolation: this fake supervisor
    # hands back exactly what a guest claimed, unfiltered. peak_memory_kb
    # stays valid, to confirm only the invalid field is nulled.
    settings = _settings(tmp_path)
    reply = {
        "stdout": "2\n", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False,
        "cpu_time_sec": value, "peak_memory_kb": 1024,
    }

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, reply)
        result = await executor.execute_magma("print 1+1;", settings)
        server.close(); await server.wait_closed()
        return result

    result = asyncio.run(run())
    assert result.cpu_time_sec is None
    assert result.peak_memory_kb == 1024


@pytest.mark.parametrize(
    "value", [10**400, float("inf"), -1, 12.5, "1024"],
    ids=["huge-int", "inf", "negative", "float", "string"],
)
def test_firecracker_backend_nulls_untrusted_memory(tmp_path, value):
    # cpu_time_sec stays valid, to confirm only the invalid field is nulled.
    settings = _settings(tmp_path)
    reply = {
        "stdout": "2\n", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False,
        "cpu_time_sec": 1.5, "peak_memory_kb": value,
    }

    async def run():
        server, _ = await _fake_supervisor(settings.supervisor_socket, reply)
        result = await executor.execute_magma("print 1+1;", settings)
        server.close(); await server.wait_closed()
        return result

    result = asyncio.run(run())
    assert result.peak_memory_kb is None
    assert result.cpu_time_sec == 1.5


def test_main_prefers_guest_cpu_and_memory_over_the_missing_footer(tmp_path, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app import main as app_main

    settings = _settings(tmp_path)
    # No "quit." in stdout, so the parser finds no footer at all, exactly
    # what a truncated or killed job leaves; the guest's own numbers are
    # all there is.
    reply = {
        "stdout": "no footer here", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False,
        "cpu_time_sec": 2.5, "peak_memory_kb": 40960,
    }
    submission_entries = _submission_log(tmp_path, monkeypatch)
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(_fake_supervisor(settings.supervisor_socket, reply))[0]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(app_main, "settings", settings)
    try:
        TestClient(app_main.app).post("/execute", json={"code": "1;"})
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        server.close()
        loop.close()

    completion = submission_entries()[-1]
    assert completion["time_sec"] == 2.5
    assert completion["memory_mb"] == 40.0


@pytest.mark.parametrize("listening", [False, True], ids=["missing", "refused"])
def test_main_returns_503_when_the_supervisor_is_unreachable(tmp_path, monkeypatch, listening):
    from fastapi.testclient import TestClient
    from app import main as app_main

    settings = _settings(tmp_path)
    sock = socket.socket(socket.AF_UNIX)
    if listening:
        # Bound but never listening: connect() fails with ECONNREFUSED.
        sock.bind(settings.supervisor_socket)
    monkeypatch.setattr(app_main, "settings", settings)
    events = _usage_events(tmp_path, monkeypatch)
    try:
        response = TestClient(app_main.app).post("/execute", json={"code": "1;"})
    finally:
        sock.close()
    assert response.status_code == 503
    assert response.json() == {"error": "Execution service unavailable"}
    assert events() == [("start", None, None), ("end", 503, "unavailable")]


@pytest.mark.parametrize("backend", [None, "", "nsjial"])
def test_service_refuses_to_start_without_a_known_backend(backend):
    env = {k: v for k, v in os.environ.items() if k != "EXECUTOR_BACKEND"}
    if backend is not None:
        env["EXECUTOR_BACKEND"] = backend
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "executor_backend" in proc.stderr


def test_main_returns_503_on_busy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as app_main

    async def busy(code, settings):
        raise executor.SupervisorBusy()

    monkeypatch.setattr(app_main, "execute_magma", busy)
    events = _usage_events(tmp_path, monkeypatch)
    client = TestClient(app_main.app)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 503
    assert events() == [("start", None, None), ("end", 503, "busy")]


def test_main_reports_truncation_from_the_worker(tmp_path, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from app import main as app_main

    settings = _settings(tmp_path)
    reply = {"stdout": "partial\n", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": True}
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(_fake_supervisor(settings.supervisor_socket, reply))[0]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(app_main, "settings", settings)
    try:
        body = TestClient(app_main.app).post("/execute", json={"code": "1;"}).json()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        server.close()
        loop.close()
    assert body["truncated"] is True
    assert body["error"] == "The output is too long and has been truncated."
    assert body["warnings"] == []


def test_main_returns_413_for_code_too_large_once_escaped(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as app_main

    settings = _settings(tmp_path)
    code = "\x01" * settings.magma_input_bytes
    assert len(protocol.encode({"code": code})) > protocol.MAX_REQUEST_BYTES
    monkeypatch.setattr(app_main, "settings", settings)
    events = _usage_events(tmp_path, monkeypatch)
    submission_entries = _submission_log(tmp_path, monkeypatch)

    response = TestClient(app_main.app).post("/execute", json={"code": code})

    assert response.status_code == 413
    assert response.json() == {"error": "Input too large"}
    # Admitted and counted exactly like any other request, same as before
    # the submission log existed: one start, one end with the executor's
    # own 413/too_large mapping. Only the submission log treats it
    # specially, since the code was always going to be rejected.
    assert events() == [("start", None, None), ("end", 413, "too_large")]
    [arrival, completion] = submission_entries()
    assert arrival["reason"] == "too_large"
    assert "code" not in arrival
    assert completion["outcome"] == "too_large"
    assert "code" not in completion


@pytest.mark.parametrize("code", ["\x01" * 42_000, "1;"], ids=["over-code-limit", "small"])
def test_main_returns_413_when_the_supervisor_rejects_the_code_size(tmp_path, monkeypatch, code):
    import threading
    from fastapi.testclient import TestClient
    from app import main as app_main

    settings = _settings(tmp_path)
    # Whatever reaches it, this supervisor answers as the real one does for oversized code.
    reply = {"error": "bad_request", "stdout": "", "stderr": "code too large", "exit_code": -1, "timed_out": False, "truncated": False}
    loop = asyncio.new_event_loop()
    server = loop.run_until_complete(_fake_supervisor(settings.supervisor_socket, reply))[0]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(app_main, "settings", settings)
    try:
        response = TestClient(app_main.app).post("/execute", json={"code": code})
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        server.close()
        loop.close()
    assert response.status_code == 413
    assert response.json() == {"error": "Input too large"}
