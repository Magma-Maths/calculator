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


async def _fake_supervisor(path, reply):
    seen = {}

    async def handle(reader, writer):
        seen["request"] = await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES)
        await protocol.write_frame(writer, reply)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=path)
    return server, seen


def _usage_events(tmp_path, monkeypatch):
    """Point main's usage log at a fresh file; returns a reader of (event, success) pairs."""
    from app import main as app_main
    from app.usage_logger import UsageLogger

    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(app_main, "usage_logger", UsageLogger(str(path)))
    return lambda: [(e["event"], e.get("success")) for e in map(json.loads, path.read_text().splitlines())]


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
    assert result == executor.ExecutionResult(stdout="", stderr="", exit_code=-1)
    assert "seccomp filter killed magma (mode=on)" in caplog.text


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
    assert events() == [("start", None), ("end", False)]


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
    assert events() == [("start", None), ("end", False)]


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
    response = TestClient(app_main.app).post("/execute", json={"code": code})
    assert response.status_code == 413
    assert response.json() == {"error": "Input too large"}
    assert events() == [("start", None), ("end", False)]


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
