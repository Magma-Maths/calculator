import heapq
import json
import logging
import threading
import time
from pathlib import Path

from app.log_retention import parse_timestamp, prune_records

logger = logging.getLogger("calculator")


class UsageLogger:
    def __init__(self, path: str, retention_days: int | None = None):
        self._path = Path(path) if path else None
        self._days = retention_days
        self._lock = threading.Lock()
        self._records: list[tuple[float, float, bool, str]] = []
        self._writable = self._path is not None
        try:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._writable = False
            logger.warning("Cannot create usage log directory: %s", self._path.parent)
        self._replay()
        self._prune()

    def _replay(self):
        if self._path is None or not self._path.exists():
            return
        with open(self._path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and self._is_completion(entry):
                    self._count(entry)

    @staticmethod
    def _is_completion(entry: dict) -> bool:
        # Legacy records have no event marker and describe completed requests.
        return entry.get("event", "end") == "end"

    def _count(self, entry: dict):
        timestamp = parse_timestamp(entry.get("timestamp"))
        if timestamp is None or (self._days is not None and timestamp < time.time() - self._days * 86400):
            return
        heapq.heappush(self._records, (
            timestamp, entry.get("elapsed_sec", 0.0),
            bool(entry.get("success", False)), entry.get("client_ip", ""),
        ))

    def _prune(self):
        if self._days is not None:
            cutoff = time.time() - self._days * 86400
            while self._records and self._records[0][0] < cutoff:
                heapq.heappop(self._records)

    def log(self, entry: dict):
        line = json.dumps(entry, default=str)
        with self._lock:
            if self._is_completion(entry):
                self._count(entry)
            self._prune()
            if self._writable:
                try:
                    with open(self._path, "a") as f:
                        f.write(line + "\n")
                except OSError:
                    logger.warning("Cannot write to usage log: %s", self._path)

    def cleanup(self, cutoff: float):
        with self._lock:
            self._prune()
            prune_records(self._path, cutoff)

    @staticmethod
    def _stats_row(records) -> dict:
        requests, elapsed, successes = 0, 0.0, 0
        ips = set()
        for _, duration, success, ip in records:
            requests += 1
            elapsed += duration
            successes += success
            if ip:
                ips.add(ip)
        return {
            "total_requests": requests,
            "unique_ips": len(ips),
            "avg_elapsed_sec": round(elapsed / requests, 3) if requests else 0.0,
            "successes": successes,
            "failures": requests - successes,
        }

    def stats(self) -> dict:
        with self._lock:
            self._prune()
            cutoff = time.time() - 86400
            return {
                "retained": {"days": self._days, **self._stats_row(self._records)},
                "last_24h": self._stats_row(record for record in self._records if record[0] >= cutoff),
            }
