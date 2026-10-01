import asyncio
import json

import pytest

from app import executor
from app.config import Settings
from firecracker import protocol


async def _fake_supervisor(path, reply):
    seen = {}

    async def handle(reader, writer):
        seen["request"] = await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES)
        await protocol.write_frame(writer, reply)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=path)
    return server, seen


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


def test_firecracker_backend_socket_missing(tmp_path):
    settings = _settings(tmp_path)
    result = asyncio.run(executor.execute_magma("1;", settings))
    assert result.exit_code == -1 and "unavailable" in result.stderr


def test_default_backend_is_nsjail():
    assert Settings().executor_backend == "nsjail"


def test_main_returns_503_on_busy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as app_main

    async def busy(code, settings):
        raise executor.SupervisorBusy()

    monkeypatch.setattr(app_main, "execute_magma", busy)
    client = TestClient(app_main.app)
    response = client.post("/execute", json={"code": "1;"})
    assert response.status_code == 503
