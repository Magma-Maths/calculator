import json
import time

import pytest

from app.usage_logger import UsageLogger


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _make_entry(client_ip="1.2.3.4", elapsed_sec=1.5, success=True, ts=None):
    """A completion line as written before arrival records existed: no event marker."""
    if ts is None:
        ts = _now()
    return {
        "timestamp": ts,
        "client_ip": client_ip,
        "input_size": 10,
        "elapsed_sec": elapsed_sec,
        "memory_used": "12.34MB",
        "success": success,
        "warnings": [] if success else ["error"],
    }


def test_log_appends_jsonl(tmp_path):
    path = tmp_path / "usage.jsonl"
    ul = UsageLogger(str(path))
    ul.log(_make_entry())
    ul.log(_make_entry(client_ip="5.6.7.8"))

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # must be valid JSON


def test_stats_alltime(tmp_path):
    path = tmp_path / "usage.jsonl"
    ul = UsageLogger(str(path))
    ul.log(_make_entry(client_ip="1.1.1.1", elapsed_sec=2.0, success=True))
    ul.log(_make_entry(client_ip="2.2.2.2", elapsed_sec=4.0, success=False))
    ul.log(_make_entry(client_ip="1.1.1.1", elapsed_sec=3.0, success=True))

    s = ul.stats()["all_time"]
    assert s["total_requests"] == 3
    assert s["unique_ips"] == 2
    assert s["successes"] == 2
    assert s["failures"] == 1
    assert s["avg_elapsed_sec"] == 3.0


def test_stats_last_24h_excludes_old(tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    ul = UsageLogger(str(path))

    # Log an entry that appears to be from 25 hours ago
    old_ts = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90000)
    )
    ul.log(_make_entry(ts=old_ts, client_ip="1.1.1.1"))

    # Log a recent entry
    ul.log(_make_entry(client_ip="2.2.2.2"))

    s = ul.stats()
    assert s["all_time"]["total_requests"] == 2
    assert s["last_24h"]["total_requests"] == 1
    assert s["last_24h"]["unique_ips"] == 1


def test_replay_on_init(tmp_path):
    path = tmp_path / "usage.jsonl"
    ul = UsageLogger(str(path))
    ul.log(_make_entry(client_ip="1.1.1.1", elapsed_sec=2.0, success=True))
    ul.log(_make_entry(client_ip="2.2.2.2", elapsed_sec=4.0, success=False))

    # Create a new logger from the same file — should replay
    ul2 = UsageLogger(str(path))
    s = ul2.stats()["all_time"]
    assert s["total_requests"] == 2
    assert s["unique_ips"] == 2
    assert s["successes"] == 1
    assert s["failures"] == 1


def _arrival_entry(client_ip="1.2.3.4", request_id="r1", ts=None):
    return {
        "event": "start",
        "request_id": request_id,
        "timestamp": ts or _now(),
        "client_ip": client_ip,
        "input_size": 10,
    }


def _completion_entry(request_id="r1", **kwargs):
    return {"event": "end", "request_id": request_id, **_make_entry(**kwargs)}


def test_arrival_lines_are_persisted_but_not_counted(tmp_path):
    path = tmp_path / "usage.jsonl"
    ul = UsageLogger(str(path))
    ul.log(_arrival_entry(client_ip="9.9.9.9", request_id="r1"))
    ul.log(_arrival_entry(client_ip="8.8.8.8", request_id="r2"))  # never completes
    ul.log(_completion_entry(request_id="r1", client_ip="9.9.9.9", elapsed_sec=2.0))

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["start", "start", "end"]
    assert lines[0]["request_id"] == lines[2]["request_id"] == "r1"

    s = ul.stats()
    for window in ("all_time", "last_24h"):
        assert s[window] == {
            "total_requests": 1,
            "unique_ips": 1,
            "avg_elapsed_sec": 2.0,
            "successes": 1,
            "failures": 0,
        }


def test_replay_mixed_file(tmp_path):
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90000))
    lines = [
        # Written before arrival records existed: no marker, counts as completed.
        _make_entry(client_ip="1.1.1.1", elapsed_sec=2.0, success=True, ts=old_ts),
        _make_entry(client_ip="2.2.2.2", elapsed_sec=4.0, success=False),
        _arrival_entry(client_ip="3.3.3.3", request_id="r1"),
        _completion_entry(request_id="r1", client_ip="3.3.3.3", elapsed_sec=6.0, success=True),
        # Arrived and never completed.
        _arrival_entry(client_ip="4.4.4.4", request_id="r2"),
    ]
    path = tmp_path / "usage.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    s = UsageLogger(str(path)).stats()
    assert s["all_time"] == {
        "total_requests": 3,
        "unique_ips": 3,
        "avg_elapsed_sec": 4.0,
        "successes": 2,
        "failures": 1,
    }
    assert s["last_24h"] == {
        "total_requests": 2,
        "unique_ips": 2,
        "avg_elapsed_sec": 5.0,
        "successes": 1,
        "failures": 1,
    }


def test_missing_file(tmp_path):
    path = tmp_path / "nonexistent" / "usage.jsonl"
    ul = UsageLogger(str(path))
    s = ul.stats()
    assert s["all_time"]["total_requests"] == 0
    assert s["last_24h"]["total_requests"] == 0


def test_empty_file(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text("")
    ul = UsageLogger(str(path))
    s = ul.stats()
    assert s["all_time"]["total_requests"] == 0
