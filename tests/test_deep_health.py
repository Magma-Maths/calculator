import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import deep_health, main
from app.executor import ExecutionResult, SupervisorBusy
from app.ratelimit import RateLimiter

MAGMA_TWO = (
    "Magma V2.29-4     Fri Jan 31 2026 [Seed = 42]\n"
    "quit.\n"
    "2\n"
    "Total time: 0.050 seconds, Total memory usage: 12.34MB\n"
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(deep_health, "_cached", {})
    return TestClient(main.app)


@pytest.fixture
def magma():
    with patch("app.deep_health.execute_magma", new_callable=AsyncMock) as mock:
        mock.return_value = ExecutionResult(stdout=MAGMA_TWO, stderr="", exit_code=0)
        yield mock


def _expire_cache():
    deep_health._cached["at"] -= deep_health.CACHE_SECONDS


def test_healthy(client, magma):
    resp = client.get("/health/deep")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "probe": "ok", "problems": []}
    [(code, settings), _] = magma.call_args
    assert code == "print 1+1;"
    assert settings.magma_timeout == deep_health.PROBE_TIMEOUT


def test_one_probe_per_cache_period(client, magma):
    for _ in range(5):
        assert client.get("/health/deep").status_code == 200
    assert magma.call_count == 1
    _expire_cache()
    client.get("/health/deep")
    assert magma.call_count == 2


def test_probe_does_not_use_the_rate_limit(client, magma, monkeypatch):
    monkeypatch.setattr(main, "rate_limiter", RateLimiter(per_minute=1, per_hour=1))
    for _ in range(3):
        assert client.get("/health/deep").status_code == 200
        _expire_cache()
    with patch("app.main.execute_magma", new_callable=AsyncMock) as execute:
        execute.return_value = ExecutionResult(stdout=MAGMA_TWO, stderr="", exit_code=0)
        assert client.post("/execute", json={"code": "print 1+1;"}).status_code == 200


def test_all_slots_taken_reports_busy_without_running(client, magma, monkeypatch):
    monkeypatch.setattr(main, "semaphore", asyncio.Semaphore(0))
    resp = client.get("/health/deep")
    assert resp.status_code == 200
    assert resp.json()["status"] == "busy"
    magma.assert_not_called()


def test_supervisor_busy_reports_busy(client, magma):
    magma.side_effect = SupervisorBusy()
    resp = client.get("/health/deep")
    assert resp.status_code == 200
    assert resp.json()["status"] == "busy"


@pytest.mark.parametrize("result, problem", [
    (ExecutionResult(stdout="", stderr="worker service unavailable", exit_code=-1), "probe-wrong-output"),
    (ExecutionResult(stdout=MAGMA_TWO.replace("\n2\n", "\n3\n"), stderr="", exit_code=0), "probe-wrong-output"),
    (ExecutionResult(
        stdout="Error: Cannot read Magma passfile\nThis host has the following MAC address(es):\n",
        stderr="", exit_code=1,
    ), "probe-licence-rejected"),
])
def test_failed_probe(client, magma, result, problem):
    magma.return_value = result
    resp = client.get("/health/deep")
    assert resp.status_code == 503
    assert resp.json()["status"] == "fail"
    assert resp.json()["problems"] == [problem]


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"health_status_file": str(path)}))
    return path


def _write_status(path, problems, valid_for=600):
    path.write_text(json.dumps({"problems": problems, "valid_until": time.time() + valid_for}))


def test_host_checks_passing(client, magma, status_file):
    _write_status(status_file, [])
    assert client.get("/health/deep").json()["status"] == "ok"


@pytest.mark.parametrize("contents, problems", [
    ({"problems": ["disk:/"], "valid_for": 600}, ["disk:/"]),
    ({"problems": [], "valid_for": -1}, ["host-checks-stale"]),
    (None, ["host-checks-missing"]),
])
def test_host_checks_failing(client, magma, status_file, contents, problems):
    if contents is not None:
        _write_status(status_file, contents["problems"], contents["valid_for"])
    resp = client.get("/health/deep")
    assert resp.status_code == 503
    assert resp.json() == {"status": "fail", "probe": "ok", "problems": problems}


def test_host_checks_unreadable(client, magma, status_file):
    status_file.write_text("{")
    assert client.get("/health/deep").json()["problems"] == ["host-checks-unreadable"]



def test_executor_error_is_a_cached_failure(client, magma, status_file):
    magma.side_effect = OSError(11, "Resource temporarily unavailable")
    _write_status(status_file, ["disk:/"])
    for _ in range(2):
        resp = client.get("/health/deep")
        assert resp.status_code == 503
        assert resp.json() == {"status": "fail", "probe": "error", "problems": ["probe-error", "disk:/"]}
    assert magma.call_count == 1
