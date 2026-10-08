import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("calculator")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SubmissionLogger:
    """Separate from usage_logger's usage.jsonl, which is replayed at
    startup for stats and must never carry code. Nothing reads this file
    back; it exists only for a human to grep after an incident.
    """

    def __init__(self, path: str):
        self._path = Path(path) if path else None
        self._lock = threading.Lock()
        # Only a startup failure disables logging permanently; a later
        # write error is transient and retried next write, so one full
        # disk does not blind the log for the process's life.
        self._enabled = self._path is not None
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._enabled = False
                logger.warning(
                    "Cannot create submission log directory: %s", self._path.parent
                )

    def _write(self, entry: dict) -> None:
        if not self._enabled:
            return
        line = json.dumps(entry, default=str) + "\n"
        with self._lock:
            try:
                # Open-append-close per line: no handle held across
                # requests, so logrotate can rename this file without
                # copytruncate.
                fd = os.open(
                    str(self._path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                try:
                    os.fchmod(fd, 0o600)  # enforce regardless of umask
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                logger.warning("Cannot write to submission log: %s", self._path)

    def log_arrival(self, request_id: str, client_ip: str, code: str) -> None:
        """Written before execution starts, so a run that kills or hangs
        the worker still leaves the code that caused it.
        """
        self._write({
            "request_id": request_id,
            "timestamp": _utc_timestamp(),
            "client_ip": client_ip,
            "code": code,
        })

    def log_completion(self, request_id: str, **outcome) -> None:
        self._write({
            "request_id": request_id,
            "timestamp": _utc_timestamp(),
            **outcome,
        })

    def log_rejected(
        self, request_id: str, client_ip: str, reason: str, input_size: int,
    ) -> None:
        """Metadata only, no code: 413 and 429 both precede the rate
        limiter's own bookkeeping, so logging code here would let a client
        grow this file without bound just by being rejected.
        """
        self._write({
            "request_id": request_id,
            "timestamp": _utc_timestamp(),
            "client_ip": client_ip,
            "reason": reason,
            "input_size": input_size,
        })
