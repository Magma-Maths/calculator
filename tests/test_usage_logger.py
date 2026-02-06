import json
import time

import pytest

from app.usage_logger import UsageLogger


def _make_entry(client_ip="1.2.3.4", elapsed_sec=1.5, success=True, ts=None):
    if ts is None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
