import json

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.executor import ExecutionResult
from app.usage_logger import UsageLogger


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
    assert any("time limit" in w for w in data["warnings"])


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


def test_arrival_line_outlives_an_execution_that_never_returns(client, usage_log):
    async def vanish(code, settings):
        raise RuntimeError("magma never came back")

    with patch("app.main.execute_magma", side_effect=vanish), pytest.raises(RuntimeError):
        client.post("/execute", json={"code": "print 1+1;"})

    [arrival] = _entries(usage_log)
    assert arrival["event"] == "start"
    assert arrival["client_ip"] == "testclient"
    assert _stats()["all_time"]["total_requests"] == 0


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
