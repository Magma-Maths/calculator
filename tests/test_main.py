import asyncio
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

from app.executor import ExecutionResult, SupervisorBusy, SupervisorUnavailable
from app.submission_logger import SubmissionLogger
from app.usage_logger import UsageLogger

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # rate_limiter is a module-level singleton shared by the whole pytest
    # session; without this, enough tests posting to /execute from the
    # TestClient's fixed "testclient" IP eventually trips a real 429.
    from app import main
    main.rate_limiter._requests.clear()
    yield


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
    assert (completion["status"], completion["reason"]) == (500, "error")
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


def test_requests_turned_away_before_admission_are_logged(client, monkeypatch, caplog, usage_log):
    from app import main
    from app.ratelimit import RateLimiter
    monkeypatch.setattr(main, "rate_limiter", RateLimiter(per_minute=0, per_hour=0))
    with caplog.at_level("INFO", logger="calculator"):
        assert client.post("/execute", json={"code": "1;"}).status_code == 429
        assert client.post("/execute", json={"code": "x" * (50 * 1024 + 1)}).status_code == 413
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "calculator"]
    assert [(r["event"], r["status"], r["reason"]) for r in records] == [
        ("rejected", 429, "rate_limited"), ("rejected", 413, "too_large"),
    ]
    assert _entries(usage_log) == []


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
    assert (completion["status"], completion["reason"]) == (200, "completed")
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


@pytest.fixture
def submission_log(tmp_path, monkeypatch):
    """Point main's submission logger at a fresh file and return that path."""
    from app import main
    path = tmp_path / "submissions.jsonl"
    monkeypatch.setattr(main, "submission_logger", SubmissionLogger(str(path)))
    return path


def test_submission_logged_before_execution(client, submission_log):
    seen_by_executor = []

    async def snapshot_then_run(code, settings):
        seen_by_executor.extend(_entries(submission_log))
        return ExecutionResult(stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0)

    with patch("app.main.execute_magma", side_effect=snapshot_then_run):
        resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200

    [arrival] = seen_by_executor
    assert arrival["client_ip"] == "testclient"
    assert arrival["code"] == "print 1+1;"
    assert arrival["timestamp"]

    [_, completion] = _entries(submission_log)
    assert completion["request_id"] == arrival["request_id"]
    assert completion["exit_code"] == 0
    assert completion["timed_out"] is False
    assert completion["seccomp_killed"] is False
    assert completion["elapsed"] >= 0


def test_submission_logs_one_completion_even_for_an_unrecognized_exception(client, submission_log, usage_log):
    # An unrecognized exception is not a hang: the outer finally always
    # runs, so both logs get exactly one outcome line, never a silent
    # arrival with no matching completion.
    async def boom(code, settings):
        raise RuntimeError("magma never came back")

    with patch("app.main.execute_magma", side_effect=boom), pytest.raises(RuntimeError):
        client.post("/execute", json={"code": "print 1+1;"})

    [arrival, completion] = _entries(submission_log)
    assert arrival["client_ip"] == "testclient"
    assert arrival["code"] == "print 1+1;"
    assert completion["request_id"] == arrival["request_id"]
    assert completion["outcome"] == "error"
    assert completion["elapsed"] >= 0
    assert "code" not in completion

    [_, usage_completion] = _entries(usage_log)
    assert (usage_completion["status"], usage_completion["reason"]) == (500, "error")


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_submission_logs_timed_out(mock_exec, client, submission_log):
    mock_exec.return_value = ExecutionResult(
        stdout="Magma V2.29-4 [Seed = 1]\nquit.\n",
        stderr="Alarm clock\n",
        exit_code=0,
        timed_out=True,
    )
    resp = client.post("/execute", json={"code": "while true do end while;"})
    assert resp.status_code == 200

    [_, completion] = _entries(submission_log)
    assert completion["timed_out"] is True


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_submission_logs_resource_stats(mock_exec, client, submission_log):
    mock_exec.return_value = ExecutionResult(stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0)
    resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200

    [_, completion] = _entries(submission_log)
    assert completion["time_sec"] == 0.05
    assert completion["memory_mb"] == 12.34
    assert completion["stdout_bytes"] == len("2\n")
    assert completion["stdout_truncated"] is False
    assert completion["stdout_bytes_raw"] == len(MOCK_MAGMA_STDOUT)
    assert completion["stderr_bytes"] == 0
    assert completion["in_flight_at_admission"] == 0


def test_submission_too_large_logs_metadata_only(client, submission_log):
    big_code = "x" * (50 * 1024 + 1)
    resp = client.post("/execute", json={"code": big_code})
    assert resp.status_code == 413

    [entry] = _entries(submission_log)
    assert entry["reason"] == "too_large"
    assert entry["input_size"] == len(big_code)
    assert "code" not in entry


def test_submission_rate_limited_logs_metadata_only(client, submission_log, monkeypatch):
    from app import main
    monkeypatch.setattr(main.rate_limiter, "is_allowed", lambda ip: False)

    resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 429

    [entry] = _entries(submission_log)
    assert entry["reason"] == "rate_limited"
    assert entry["input_size"] == len("print 1+1;")
    assert "code" not in entry


def test_repeated_429s_cannot_grow_the_log_with_code(client, submission_log, monkeypatch):
    from app import main
    monkeypatch.setattr(main.rate_limiter, "is_allowed", lambda ip: False)

    # Control characters cost the most to escape in JSON; a 429 that kept
    # this body would turn a 50 KB post into a line several times larger.
    near_limit_code = "\x01" * (50 * 1024)
    for _ in range(5):
        resp = client.post("/execute", json={"code": near_limit_code})
        assert resp.status_code == 429

    entries = _entries(submission_log)
    assert len(entries) == 5
    for entry in entries:
        assert "code" not in entry
        assert entry["input_size"] == len(near_limit_code)
    assert max(len(json.dumps(e)) for e in entries) < 1024


def test_submission_slots_busy_logs_metadata_only(client, submission_log, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "semaphore", asyncio.Semaphore(0))

    resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 503

    [entry] = _entries(submission_log)
    assert entry["reason"] == "busy"
    assert entry["input_size"] == len("print 1+1;")
    assert "code" not in entry


def test_submission_busy_supervisor_writes_completion_line(client, submission_log):
    async def busy(code, settings):
        raise SupervisorBusy()

    with patch("app.main.execute_magma", side_effect=busy):
        resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 503

    [arrival, completion] = _entries(submission_log)
    assert arrival["code"] == "print 1+1;"
    assert completion["request_id"] == arrival["request_id"]
    assert completion["outcome"] == "busy"
    assert completion["elapsed"] >= 0


def test_submission_unavailable_supervisor_writes_completion_line(client, submission_log):
    # A request the supervisor turns away after admission (busy or
    # unavailable) was already counted by the rate limiter, so it keeps
    # its code; only the completion's outcome marks why it did not run.
    async def unavailable(code, settings):
        raise SupervisorUnavailable()

    with patch("app.main.execute_magma", side_effect=unavailable):
        resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 503

    [arrival, completion] = _entries(submission_log)
    assert arrival["code"] == "print 1+1;"
    assert completion["request_id"] == arrival["request_id"]
    assert completion["outcome"] == "unavailable"
    assert completion["elapsed"] >= 0


def test_write_error_does_not_permanently_disable_submission_log(client, submission_log, monkeypatch):
    calls = {"n": 0}
    import app.submission_logger as sl_module
    original_open = sl_module.os.open

    def flaky_open(path, flags, mode):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("ENOSPC (simulated)")
        return original_open(path, flags, mode)

    monkeypatch.setattr(sl_module.os, "open", flaky_open)

    with patch("app.main.execute_magma", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = ExecutionResult(stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0)
        resp1 = client.post("/execute", json={"code": "print 1;"})
        resp2 = client.post("/execute", json={"code": "print 2;"})

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # The first write's arrival line failed to open (simulated ENOSPC) and was
    # dropped, but the logger must not have latched off: the second request's
    # arrival and completion lines both land.
    entries = _entries(submission_log)
    assert [e.get("code") for e in entries if "code" in e] == ["print 2;"]


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_submission_logging_disabled_when_empty(mock_exec, client, monkeypatch, tmp_path):
    from app import main
    mock_exec.return_value = ExecutionResult(stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0)
    monkeypatch.setattr(main, "submission_logger", SubmissionLogger(""))

    resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200
    assert list(tmp_path.iterdir()) == []


@patch("app.main.execute_magma", new_callable=AsyncMock)
def test_submission_unwritable_path_does_not_break_request(mock_exec, client, monkeypatch, tmp_path):
    from app import main
    mock_exec.return_value = ExecutionResult(stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setattr(main, "submission_logger", SubmissionLogger(str(blocker / "submissions.jsonl")))

    resp = client.post("/execute", json={"code": "print 1+1;"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
