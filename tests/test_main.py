import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.executor import ExecutionResult
from app.usage_logger import UsageLogger

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


MOCK_MAGMA_STDOUT = (
    "Magma V2.29-4     Fri Jan 31 2026 [Seed = 42]\n"
    "quit.\n"
    "2\n"
    "Total time: 0.050 seconds, Total memory usage: 12.34MB\n"
)


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_execute_success(mock_exec, client):
    mock_exec.return_value = ExecutionResult(
        stdout=MOCK_MAGMA_STDOUT,
        stderr="",
        exit_code=0,
    )
    resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["stdout"] == "2\n"
    assert data["magma"]["version"] == "2.29-4"
    assert data["magma"]["seed"] == 42
    assert data["magma"]["time_sec"] == 0.05
    assert data["magma"]["memory"] == "12.34MB"
    assert data["truncated"] is False
    assert data["warnings"] == []


def test_execute_missing_code(client):
    resp = client.post("/execute", json={})
    assert resp.status_code == 422


def test_execute_input_too_large(client):
    big_code = "x" * (50 * 1024 + 1)
    resp = client.post("/execute", json={"code": big_code})
    assert resp.status_code == 413


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_execute_timeout(mock_exec, client):
    mock_exec.return_value = ExecutionResult(
        stdout="Magma V2.29-4 [Seed = 1]\nquit.\n",
        stderr="Alarm clock\n",
        exit_code=0,
    )
    resp = client.post("/execute", json={"code": "while true do end while;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert "time limit" in data["error"]
    assert data["warnings"] == []


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_failure_without_a_warning_still_has_an_error(mock_exec, client):
    # As from a guest that never started: no output and nothing the parser recognises.
    mock_exec.return_value = ExecutionResult(stdout="", stderr="", exit_code=-1)
    data = client.post("/execute", json={"code": "1;"}).json()
    assert data["success"] is False
    assert data["error"] == "Execution failed (exit code -1)"
    assert data["warnings"] == []


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_error_is_not_repeated_in_warnings(mock_exec, client):
    from app.parser import TRUNCATION_WARNING
    mock_exec.return_value = ExecutionResult(
        stdout="Magma V2.29-4 [Seed = 1]\nquit.\n", stderr="Alarm clock\n", exit_code=0, truncated=True,
    )
    data = client.post("/execute", json={"code": "1;"}).json()
    assert data["success"] is False
    assert "time limit" in data["error"]
    assert data["warnings"] == [TRUNCATION_WARNING]


@pytest.fixture
def usage_log(tmp_path, monkeypatch):
    """Point main's usage logger at a fresh file and return that path."""
    from app import main
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(main, "usage_logger", UsageLogger(str(path)))
    return path


def _entries(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _stats():
    from app import main
    return main.usage_logger.stats()


def test_request_is_logged_before_execution(client, usage_log):
    seen_by_executor = []

    async def snapshot_then_run(code, settings):
        seen_by_executor.extend(_entries(usage_log))
        return ExecutionResult(stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0)

    with patch("app.main.execute_magma", side_effect=snapshot_then_run):
        resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200

    [arrival] = seen_by_executor
    assert arrival["event"] == "start"
    assert arrival["client_ip"] == "testclient"
    assert arrival["input_size"] == len("print 1+1;")
    assert arrival["timestamp"]

    assert _entries(usage_log)[0] == arrival
    [_, completion] = _entries(usage_log)
    assert completion["event"] == "end"
    assert completion["request_id"] == arrival["request_id"]
    assert completion["success"] is True
    assert completion["elapsed_sec"] >= 0

    assert _stats()["all_time"]["total_requests"] == 1
    assert _stats()["last_24h"]["total_requests"] == 1


def test_arrival_line_is_written_before_execution(client, usage_log):
    during = []

    async def crash(code, settings):
        during.extend(_entries(usage_log))
        raise RuntimeError("magma never came back")

    with patch("app.main.execute_magma", side_effect=crash), pytest.raises(RuntimeError):
        client.post("/execute", json={"code": "print 1+1;"})

    [arrival] = during
    assert arrival["event"] == "start"
    assert arrival["client_ip"] == "testclient"
    [_, completion] = _entries(usage_log)
    assert completion["event"] == "end" and completion["success"] is False
    assert _stats()["all_time"]["failures"] == 1


def test_rejected_request_leaves_no_arrival_line(client, usage_log):
    resp = client.post("/execute", json={"code": "x" * (50 * 1024 + 1)})
    assert resp.status_code == 413
    assert _entries(usage_log) == []


def test_stats_endpoint(client):
    resp = client.get("/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "all_time" in data
    assert "last_24h" in data
    for key in ("total_requests", "unique_ips", "avg_elapsed_sec", "successes", "failures"):
        assert key in data["all_time"]
        assert key in data["last_24h"]


def test_cors_preflight_allows_any_origin(client):
    resp = client.options(
        "/execute",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://example.com"


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_cors_response_allows_all(mock_exec, client):
    mock_exec.return_value = ExecutionResult(
        stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0,
    )
    resp = client.post(
        "/execute",
        json={"code": "print 1;"},
        headers={"Origin": "https://example.com"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_rate_limited_cors_response_exposes_retry_after(client, monkeypatch):
    from app import main
    from app.ratelimit import RateLimiter
    monkeypatch.setattr(main, "rate_limiter", RateLimiter(per_minute=0, per_hour=0))
    resp = client.post("/execute", json={"code": "1;"}, headers={"Origin": "https://example.com"})
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "60"
    assert resp.headers["access-control-expose-headers"] == "Retry-After"


def test_seccomp_kill_reported_and_logged(jailed_magma_killed_by_sigsys, usage_log):
    resp = jailed_magma_killed_by_sigsys.post("/execute", json={"code": "print 1;"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["exit_code"] == -1
    assert "killed by seccomp policy" in data["error"]
    [_, completion] = _entries(usage_log)
    assert completion["success"] is False
    assert completion["warnings"] == [data["error"]]
    assert data["warnings"] == []


def _rate_limit_statuses(tmp_path, forwarded_allow_ips, forwarded_for):
    """Statuses from `python -m app.main` for one POST per X-Forwarded-For value.

    Each request arrives from 127.0.0.1 and the limit is one per minute, so a
    429 means the request was counted against an earlier one's client.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(
        os.environ,
        EXECUTOR_BACKEND="firecracker",
        SUPERVISOR_SOCKET=str(tmp_path / "absent.sock"),
        PORT=str(port),
        RATE_LIMIT_PER_MINUTE="1",
        USAGE_LOG_FILE=str(tmp_path / "usage.jsonl"),
        FORWARDED_ALLOW_IPS=forwarded_allow_ips,
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"{url}/health")
                break
            except httpx.TransportError:
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        return [
            httpx.post(f"{url}/execute", json={"code": "1;"},
                       headers={"X-Forwarded-For": xff}).status_code
            for xff in forwarded_for
        ]
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_clients_behind_a_trusted_proxy_get_separate_rate_limits(tmp_path):
    statuses = _rate_limit_statuses(
        tmp_path, "127.0.0.1", ["198.51.100.1", "198.51.100.1", "198.51.100.2"])
    assert [s == 429 for s in statuses] == [False, True, False]


def test_forwarded_for_from_an_untrusted_peer_is_ignored(tmp_path):
    statuses = _rate_limit_statuses(tmp_path, "192.0.2.1", ["198.51.100.1", "198.51.100.2"])
    assert [s == 429 for s in statuses] == [False, True]
