import asyncio
import gzip
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import executor, main
from app.config import Settings
from app.executor import ExecutionResult
from app.submission_logger import SubmissionLogger
from app.usage_logger import UsageLogger
from firecracker import protocol
from tests.test_main import MOCK_MAGMA_STDOUT, _start_server


@pytest.fixture(autouse=True)
def fresh_metadata(monkeypatch):
    monkeypatch.setattr(executor, "_metadata", {})
    monkeypatch.setattr(executor, "_metadata_lock", asyncio.Lock())


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_RETENTION_DAYS", "2")
    settings = Settings(
        usage_log_file=str(tmp_path / "usage.jsonl"),
        submission_log_file=str(tmp_path / "submissions.jsonl"),
        supervisor_socket=str(tmp_path / "absent.sock"),
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "_RETENTION_INTERVAL_SECONDS", 0.02, raising=False)
    monkeypatch.setattr(main, "execute_magma", AsyncMock(return_value=ExecutionResult(
        stdout=MOCK_MAGMA_STDOUT, stderr="", exit_code=0,
    )))
    return settings


def _reload_logs(settings, monkeypatch):
    monkeypatch.setattr(main, "usage_logger", UsageLogger(settings.usage_log_file, settings.log_retention_days))
    monkeypatch.setattr(main, "submission_logger", SubmissionLogger(settings.submission_log_file))


def _record(age, **extra):
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age)),
        "client_ip": "192.0.2.123", "elapsed_sec": 2, "success": True, **extra,
    }


def _write(path, entries):
    Path(path).write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.mark.parametrize("days", [2, None], ids=["bounded", "unbounded"])
def test_stats_retained_windows_and_live_updates(logs, monkeypatch, days):
    now = 1792368000
    gmtime = time.gmtime
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(time, "gmtime", lambda seconds=None: gmtime(now if seconds is None else seconds))
    logs.log_retention_days = days
    entries = [
        _record(10, client_ip="192.0.2.4", elapsed_sec=0),
        _record(2 * 86400, client_ip="192.0.2.2", elapsed_sec=4, success=False),
        _record(86400 + 1, client_ip="192.0.2.3", elapsed_sec=2),
        _record(86400, client_ip="192.0.2.3", elapsed_sec=2),
        _record(2 * 86400 + 1, client_ip="192.0.2.1", elapsed_sec=10),
        _record(0, event="start", client_ip="192.0.2.5"),
    ]
    _write(logs.usage_log_file, entries)
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        response = client.get("/stats")
        assert response.status_code == 200
        stats = response.json()
        assert set(stats) == {"retained", "last_24h"}
        assert stats["retained"] == {
            "days": days, "total_requests": 4 if days else 5,
            "unique_ips": 3 if days else 4,
            "avg_elapsed_sec": 2.0 if days else 3.6,
            "successes": 3 if days else 4, "failures": 1,
        }
        assert stats["last_24h"] == {
            "total_requests": 2, "unique_ips": 2, "avg_elapsed_sec": 1.0,
            "successes": 2, "failures": 0,
        }
        assert client.get("/info").json()["retention"]["submission_min_days"] == days
        now += 1
        stats = client.get("/stats").json()
        assert stats["last_24h"]["total_requests"] == 1
        assert stats["last_24h"]["unique_ips"] == 1
        assert stats["retained"]["total_requests"] == (3 if days else 5)
        assert stats["retained"]["unique_ips"] == (2 if days else 4)
        assert stats["retained"]["failures"] == (0 if days else 1)
        assert client.post("/execute", json={"code": "1;"}).status_code == 200
        stats = client.get("/stats").json()
        assert stats["retained"]["total_requests"] == (4 if days else 6)
        assert stats["retained"]["unique_ips"] == (3 if days else 5)
        assert stats["last_24h"]["total_requests"] == 2
        assert stats["last_24h"]["unique_ips"] == 2
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/stats").json() == stats
    assert not Path(logs.usage_log_file + ".stats.sqlite3").exists()


def test_stats_expire_when_retention_cleanup_fails(logs, monkeypatch):
    now = 1792368000
    monkeypatch.setattr(time, "time", lambda: now)
    _write(logs.usage_log_file, [_record(2 * 86400, client_ip="192.0.2.1")])
    _reload_logs(logs, monkeypatch)
    replace = os.replace

    def fail_replace(src, dst):
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "replace", fail_replace)
    with TestClient(main.app) as client:
        assert client.get("/info").json()["retention"]["submission_min_days"] is None
        assert client.get("/stats").json()["retained"]["total_requests"] == 1
        now += 1
        stats = client.get("/stats").json()
        assert stats["retained"] == {
            "days": 2, "total_requests": 0, "unique_ips": 0,
            "avg_elapsed_sec": 0.0, "successes": 0, "failures": 0,
        }
        assert stats["last_24h"]["unique_ips"] == 0
        assert "192.0.2.1" in Path(logs.usage_log_file).read_text()
        monkeypatch.setattr(os, "replace", replace)
        deadline = time.monotonic() + 3
        while client.get("/info").json()["retention"]["submission_min_days"] is None:
            assert time.monotonic() < deadline
            threading.Event().wait(0.01)
        assert Path(logs.usage_log_file).read_text() == ""


def test_info_configured_limits_without_job_admission(logs, monkeypatch):
    reply = {"guest_memory_bytes": 768 * 1024**2, "max_timeout_seconds": 180,
             "max_output_bytes": protocol.MAX_REPLY_BYTES // 2}
    received = []
    loop = asyncio.new_event_loop()

    async def handle(reader, writer):
        received.append(await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES))
        await protocol.write_frame(writer, reply)
        await protocol.close_writer(writer)

    Path("artifacts").mkdir(exist_ok=True)
    logs.supervisor_socket = "artifacts/info-" + uuid.uuid4().hex + ".sock"
    server = loop.run_until_complete(asyncio.start_unix_server(handle, logs.supervisor_socket))
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    logs.magma_timeout, logs.magma_cpu_timeout = 150, 170
    logs.magma_input_kb, logs.magma_output_kb = 7, 9
    logs.rate_limit_per_minute, logs.rate_limit_per_hour = 11, 39
    _reload_logs(logs, monkeypatch)
    monkeypatch.setattr(main, "semaphore", asyncio.Semaphore(0))
    monkeypatch.setattr(executor, "METADATA_CACHE_SECONDS", 0)
    try:
        with TestClient(main.app) as client:
            version = client.get("/version").json()
            response = client.get("/info", headers={"Origin": "https://example.org"})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["access-control-allow-origin"] == "*"
            assert response.json() == {
                "schema_version": 1,
                "limits": {
                    "wall_time_seconds": 150, "cpu_time_seconds": 170,
                    "guest_memory_bytes": 768 * 1024**2,
                    "input_bytes": 7 * 1024, "output_bytes": 9 * 1024,
                    "requests_per_minute": 11, "requests_per_hour": 39,
                    "max_concurrent_per_ip": 2, "queue_capacity": 0,
                },
                "retention": {"code_logged": True, "ip_logged": True, "submission_min_days": 2},
            }
            reply["max_timeout_seconds"] = 160
            reply["max_output_bytes"] = 1
            limits = client.get("/info").json()["limits"]
            assert limits["wall_time_seconds"] == 150
            assert limits["cpu_time_seconds"] is None
            assert limits["output_bytes"] is None
            reply.update(guest_memory_bytes="768", max_timeout_seconds=True, max_output_bytes=-1)
            limits = client.get("/info").json()["limits"]
            for field in ("wall_time_seconds", "cpu_time_seconds", "guest_memory_bytes", "output_bytes"):
                assert limits[field] is None
            logs.rate_limit_per_minute = 0
            assert client.get("/info").json()["limits"]["requests_per_minute"] == 0
            monkeypatch.setattr(main, "settings", logs.model_copy(update={"max_concurrent_per_ip": 3}))
            assert client.get("/info").json()["limits"]["max_concurrent_per_ip"] == 3
            assert client.get("/version").json() == version
            assert client.get("/stats").json()["retained"]["total_requests"] == 0
        assert received == [{"operation": "metadata"}] * 5
        main.execute_magma.assert_not_called()
    finally:
        loop.call_soon_threadsafe(server.close)
        asyncio.run_coroutine_threadsafe(server.wait_closed(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        Path(logs.supervisor_socket).unlink(missing_ok=True)


@pytest.mark.parametrize("enabled,days", [(True, None), (False, 2)])
def test_info_unknown_metadata_and_retention(logs, monkeypatch, enabled, days):
    logs = logs.model_copy(update={
        "submission_log_file": logs.submission_log_file if enabled else "", "log_retention_days": days,
    })
    monkeypatch.setattr(main, "settings", logs)
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        response = client.get("/info")
        assert response.status_code == 200
        data = response.json()
        for name in ("wall_time_seconds", "cpu_time_seconds", "guest_memory_bytes", "output_bytes"):
            assert data["limits"][name] is None
        assert data["limits"]["input_bytes"] == logs.magma_input_bytes
        assert data["retention"] == {
            "code_logged": enabled, "ip_logged": True, "submission_min_days": None,
        }


def test_concurrent_info_requests_share_one_supervisor_connection(logs, monkeypatch, tmp_path_factory):
    import httpx

    logs.supervisor_socket = str(tmp_path_factory.mktemp("info") / "sup.sock")
    reply = {"guest_memory_bytes": 512 * 1024**2, "max_timeout_seconds": 300,
             "max_output_bytes": protocol.MAX_REPLY_BYTES // 2}
    received = []

    async def scenario():
        release = asyncio.Event()

        async def handle(reader, writer):
            received.append(await protocol.read_frame(reader, protocol.MAX_REQUEST_BYTES))
            await release.wait()
            await protocol.write_frame(writer, reply)
            await protocol.close_writer(writer)

        server = await asyncio.start_unix_server(handle, logs.supervisor_socket)
        transport = httpx.ASGITransport(app=main.app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                requests = [asyncio.create_task(client.get("/info")) for _ in range(20)]
                async with asyncio.timeout(2):
                    while not received:
                        await asyncio.sleep(0.01)
                await asyncio.sleep(0.1)
                assert len(received) == 1
                release.set()
                for response in await asyncio.gather(*requests):
                    assert response.json()["limits"]["guest_memory_bytes"] == reply["guest_memory_bytes"]
                assert (await client.get("/info")).json()["limits"]["wall_time_seconds"] == logs.magma_timeout
                assert received == [{"operation": "metadata"}]
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_info_cors(tmp_path):
    with _start_server(tmp_path, ALLOWED_ORIGIN="https://example.org") as (_, url):
        import httpx
        for origin in ("https://example.org", "https://other.org"):
            response = httpx.get(url + "/info", headers={"Origin": origin})
            assert response.status_code == 200
            assert response.headers.get("access-control-allow-origin") == (
                origin if origin == "https://example.org" else None
            )


def test_retention_replay_archives_and_restart(logs, monkeypatch):
    old, recent = _record(3 * 86400), _record(10)
    _write(logs.usage_log_file, [old, recent])
    _write(logs.submission_log_file, [dict(old, code="expired"), dict(recent, code="kept")])
    _write(logs.submission_log_file + ".1", [dict(old, code="expired"), recent])
    with gzip.open(logs.submission_log_file + "-20261001.gz", "wt") as f:
        f.write(json.dumps(dict(old, code="expired")) + "\n")
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        stats = client.get("/stats").json()
        assert stats["retained"]["total_requests"] == 1
        assert stats["last_24h"]["total_requests"] == 1
        assert stats["retained"]["unique_ips"] == 1
        assert stats["last_24h"]["unique_ips"] == 1
        for path in (Path(logs.usage_log_file), Path(logs.submission_log_file),
                     Path(logs.submission_log_file + ".1"), Path(logs.submission_log_file + "-20261001.gz")):
            text = gzip.open(path, "rt").read() if path.suffix == ".gz" else path.read_text()
            assert "expired" not in text
            assert old["timestamp"] not in text
        assert client.post("/execute", json={"code": "print 1+1;"}).status_code == 200
        Path(logs.usage_log_file).rename(logs.usage_log_file + ".1")
        Path(logs.submission_log_file).rename(logs.submission_log_file + ".2")
        assert client.post("/execute", json={"code": "after rotation"}).status_code == 200
        assert "after rotation" in Path(logs.submission_log_file).read_text()
        assert client.get("/stats").json()["retained"]["total_requests"] == 3
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/stats").json()["retained"]["total_requests"] == 1


def test_retention_idle_cleanup_and_concurrent_requests(logs, monkeypatch):
    now = time.time()
    gmtime = time.gmtime
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(time, "gmtime", lambda seconds=None: gmtime(now if seconds is None else seconds))
    logs.max_concurrent_per_ip = 3
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        assert client.post("/execute", json={"code": "expired"}).status_code == 200
        now += 3 * 86400
        deadline = time.monotonic() + 3
        while "expired" in Path(logs.submission_log_file).read_text():
            assert time.monotonic() < deadline, "idle cleanup did not expire source"
            threading.Event().wait(0.01)
        assert client.get("/stats").json()["last_24h"]["total_requests"] == 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(lambda _: client.post("/execute", json={"code": "kept"}), range(12)))
        assert all(r.status_code == 200 for r in responses)
        stats = client.get("/stats").json()
        assert stats["retained"]["total_requests"] == 12
        assert stats["last_24h"]["total_requests"] == 12
        entries = [json.loads(line) for line in Path(logs.submission_log_file).read_text().splitlines()]
        assert len(entries) == 24
        assert sum(e.get("code") == "kept" for e in entries) == 12
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/stats").json() == stats


def test_retention_failure_then_recovery(logs, monkeypatch):
    _write(logs.submission_log_file, [_record(3 * 86400, code="expired")])
    _reload_logs(logs, monkeypatch)
    replace = os.replace

    def fail_replace(src, dst):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("os.replace", fail_replace)
        with TestClient(main.app) as client:
            response = client.get("/info")
            assert response.status_code == 200
            assert response.json()["retention"]["submission_min_days"] is None
            patch.setattr("os.replace", replace)
            deadline = time.monotonic() + 3
            while client.get("/info").json()["retention"]["submission_min_days"] is None:
                assert time.monotonic() < deadline
                threading.Event().wait(0.01)
            assert "expired" not in Path(logs.submission_log_file).read_text()


def test_retention_removes_interrupted_cleanup_files(logs, monkeypatch):
    abandoned = [Path(logs.submission_log_file).with_name(name) for name in (
        ".submissions.jsonl.retention-abcd", ".submissions.jsonl.1.gz.retention-abcd",
    )]
    for path in abandoned:
        _write(path, [_record(3 * 86400, code="expired")])
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/stats").status_code == 200
        assert not any(path.exists() for path in abandoned)


def test_info_discloses_ip_in_service_log_when_file_logs_disabled(logs, monkeypatch, caplog):
    logs.usage_log_file = logs.submission_log_file = ""
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client, caplog.at_level("INFO", logger="calculator"):
        assert client.post("/execute", json={"code": "print 1+1;"}).status_code == 200
        response = client.get("/info")
        assert response.status_code == 200
        assert response.json()["retention"] == {
            "code_logged": False, "ip_logged": True, "submission_min_days": None,
        }
    assert any('"client_ip": "testclient"' in r.message for r in caplog.records)


def test_retention_corrupt_archive_reports_unknown(logs, monkeypatch):
    archive = Path(logs.submission_log_file + ".1.gz")
    archive.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"not deflate" + b"\x00" * 8)
    _reload_logs(logs, monkeypatch)
    with TestClient(main.app) as client:
        response = client.get("/info")
        assert response.status_code == 200
        assert response.json()["retention"]["submission_min_days"] is None


@pytest.mark.parametrize("log_name,operation", [
    ("usage_logger", "execute"), ("usage_logger", "stats"),
    ("submission_logger", "execute"), ("submission_logger", "reject"),
])
def test_retention_wait_keeps_api_responsive(logs, monkeypatch, log_name, operation):
    _reload_logs(logs, monkeypatch)
    entered, release, waiting, armed = (threading.Event() for _ in range(4))
    module = __import__("app." + log_name, fromlist=["prune_records"])
    prune = module.prune_records

    def pause_rewrite(*args):
        if armed.is_set():
            armed.clear()
            entered.set()
            assert release.wait(10), "cleanup was not released"
        return prune(*args)

    monkeypatch.setattr(module, "prune_records", pause_rewrite)
    log = getattr(main, log_name)
    method = "stats" if operation == "stats" else (
        "log" if log_name == "usage_logger" else (
            "log_rejected" if operation == "reject" else "log_arrival"
        )
    )
    original = getattr(log, method)

    def observe_wait(*args, **kwargs):
        waiting.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(log, method, observe_wait)
    with TestClient(main.app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        armed.set()
        assert entered.wait(5)
        if operation == "stats":
            request = pool.submit(client.get, "/stats")
        else:
            code = "x" * (logs.magma_input_bytes + 1) if operation == "reject" else "1;"
            request = pool.submit(client.post, "/execute", json={"code": code})
        try:
            assert waiting.wait(5)
            assert not request.done()
            health = pool.submit(client.get, "/health")
            assert health.result(timeout=1).status_code == 200
            assert client.get("/info").json()["retention"]["submission_min_days"] == 2
            assert not request.done()
        finally:
            release.set()
        assert request.result(timeout=5).status_code == (413 if operation == "reject" else 200)
        if operation == "execute":
            entries = [json.loads(line) for line in Path(logs.usage_log_file).read_text().splitlines()]
            assert [entry["event"] for entry in entries] == ["start", "end"]
            assert entries[0]["request_id"] == entries[1]["request_id"]
            assert client.get("/stats").json()["retained"]["total_requests"] == 1


def test_cancelled_arrival_finishes_logging_and_releases_slot(logs, monkeypatch):
    import httpx

    _reload_logs(logs, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = main.submission_logger.log_arrival

    def pause_arrival(*args):
        entered.set()
        assert release.wait(5), "arrival was not released"
        return original(*args)

    monkeypatch.setattr(main.submission_logger, "log_arrival", pause_arrival)

    async def scenario():
        monkeypatch.setattr(main, "semaphore", asyncio.Semaphore(1))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            request = asyncio.create_task(client.post("/execute", json={"code": "cancelled"}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                assert (await client.post("/execute", json={"code": "busy"})).status_code == 503
                request.cancel()
                await asyncio.sleep(0)
                assert not request.done()
                request.cancel()
                await asyncio.sleep(0)
                assert not request.done()
                assert (await client.get("/health")).status_code == 200
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            main.execute_magma.assert_not_called()
            assert not main.semaphore.locked()
            assert (await client.post("/execute", json={"code": "next"})).status_code == 200
            stats = (await client.get("/stats")).json()["retained"]
            assert (stats["total_requests"], stats["failures"]) == (2, 1)

    asyncio.run(scenario())
    usage = [json.loads(line) for line in Path(logs.usage_log_file).read_text().splitlines()]
    assert [entry["event"] for entry in usage] == ["start", "end", "start", "end"]
    submissions = [json.loads(line) for line in Path(logs.submission_log_file).read_text().splitlines()]
    assert [entry.get("code", entry.get("outcome", entry.get("reason"))) for entry in submissions] == [
        "busy", "cancelled", "error", "next", "completed",
    ]
    assert submissions[1]["request_id"] == submissions[2]["request_id"] == usage[0]["request_id"]
