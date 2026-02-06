import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.executor import ExecutionResult


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
