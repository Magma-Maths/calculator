import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger("calculator")


class UsageLogger:
    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._writable = True

        # All-time counters
        self.total_requests = 0
        self.unique_ips: set[str] = set()
        self.total_elapsed_sec = 0.0
        self.successes = 0
        self.failures = 0

        # Last-24h entries: (timestamp, elapsed_sec, success, client_ip)
        self._recent: deque[tuple[float, float, bool, str]] = deque()

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._writable = False
            logger.warning("Cannot create usage log directory: %s", self._path.parent)
        self._replay()

    def _replay(self):
        if not self._path.exists():
            return
        cutoff = time.time() - 86400
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._update_alltime(entry)
                ts = self._parse_timestamp(entry.get("timestamp", ""))
                if ts and ts >= cutoff:
                    self._recent.append((
                        ts,
                        entry.get("elapsed_sec", 0.0),
                        entry.get("success", False),
                        entry.get("client_ip", ""),
                    ))

    @staticmethod
    def _parse_timestamp(ts_str: str) -> float | None:
        try:
            return time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except (ValueError, OverflowError):
            return None

    def _update_alltime(self, entry: dict):
        self.total_requests += 1
        ip = entry.get("client_ip", "")
        if ip:
            self.unique_ips.add(ip)
        self.total_elapsed_sec += entry.get("elapsed_sec", 0.0)
        if entry.get("success", False):
            self.successes += 1
        else:
            self.failures += 1

    def log(self, entry: dict):
        line = json.dumps(entry, default=str)
        with self._lock:
            if self._writable:
                try:
                    with open(self._path, "a") as f:
                        f.write(line + "\n")
                except OSError:
                    self._writable = False
                    logger.warning("Cannot write to usage log: %s", self._path)
            self._update_alltime(entry)
            ts = self._parse_timestamp(entry.get("timestamp", ""))
            if ts:
                self._recent.append((
                    ts,
                    entry.get("elapsed_sec", 0.0),
                    entry.get("success", False),
                    entry.get("client_ip", ""),
                ))

    def prune_24h(self):
        cutoff = time.time() - 86400
        with self._lock:
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.popleft()

    def stats(self) -> dict:
        self.prune_24h()
        with self._lock:
            all_avg = (
                round(self.total_elapsed_sec / self.total_requests, 3)
                if self.total_requests else 0.0
            )

            recent_requests = len(self._recent)
            recent_ips: set[str] = set()
            recent_elapsed = 0.0
            recent_successes = 0
            recent_failures = 0
            for _, elapsed, success, ip in self._recent:
                if ip:
                    recent_ips.add(ip)
                recent_elapsed += elapsed
                if success:
                    recent_successes += 1
                else:
                    recent_failures += 1

            recent_avg = (
                round(recent_elapsed / recent_requests, 3)
                if recent_requests else 0.0
            )

        return {
            "all_time": {
                "total_requests": self.total_requests,
                "unique_ips": len(self.unique_ips),
                "avg_elapsed_sec": all_avg,
                "successes": self.successes,
                "failures": self.failures,
            },
            "last_24h": {
                "total_requests": recent_requests,
                "unique_ips": len(recent_ips),
                "avg_elapsed_sec": recent_avg,
                "successes": recent_successes,
                "failures": recent_failures,
            },
        }
