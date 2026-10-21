import calendar
import gzip
import json
import logging
import os
import re
import tempfile
import time
import zlib
from pathlib import Path

logger = logging.getLogger("calculator")


def parse_timestamp(value: str) -> float | None:
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError, OverflowError):
        return None


def prune_records(path: Path | None, cutoff: float) -> None:
    if path is None:
        return
    archives = re.compile(re.escape(path.name) + r"(?:\.\d+|-\d{8})(?:\.gz)?$")
    for candidate in path.parent.iterdir():
        original, marker, _ = candidate.name.removeprefix(".").partition(".retention-")
        if candidate.name.startswith(".") and marker and (original == path.name or archives.fullmatch(original)):
            candidate.unlink()
        elif candidate == path or archives.fullmatch(candidate.name):
            _prune_file(candidate, cutoff)


def _prune_file(path: Path, cutoff: float) -> None:
    before = path.stat()
    opener = gzip.open if path.suffix == ".gz" else open
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".retention-", dir=path.parent)
    try:
        os.close(fd)
        with opener(path, "rt", encoding="utf-8") as source:
            with opener(temporary, "wt", encoding="utf-8") as target:
                for line in source:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    timestamp = parse_timestamp(entry.get("timestamp")) if isinstance(entry, dict) else None
                    if timestamp is not None and timestamp >= cutoff:
                        target.write(line if line.endswith("\n") else line + "\n")
        with open(temporary, "rb") as target:
            os.fsync(target.fileno())
        # A rotation outside the append lock must not be overwritten.
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise OSError("log changed during cleanup")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LogRetention:
    def __init__(self, days: int | None, *logs):
        self.days = days
        self.logs = logs
        self.succeeded = False

    def cleanup(self):
        if self.days is None:
            return
        cutoff = time.time() - self.days * 86400
        succeeded = False
        try:
            results = [self._clean(log, cutoff) for log in self.logs]
            succeeded = all(results)
        finally:
            # The previous result stands while a sweep runs. An unexpected
            # error also ends the periodic sweeps, so it reports unknown.
            self.succeeded = succeeded

    @staticmethod
    def _clean(log, cutoff: float) -> bool:
        try:
            log.cleanup(cutoff)
            return True
        except (OSError, ValueError, EOFError, zlib.error):
            logger.warning("Cannot clean retained request records", exc_info=True)
            return False
