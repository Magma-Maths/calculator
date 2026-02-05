import time


class RateLimiter:
    def __init__(self, per_minute: int, per_hour: int):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._requests: dict[str, list[float]] = {}

    def is_allowed(self, ip: str) -> bool:
        now = time.time()
        if ip not in self._requests:
            self._requests[ip] = []

        timestamps = self._requests[ip]

        one_minute_ago = now - 60
        recent_minute = sum(1 for t in timestamps if t > one_minute_ago)
        if recent_minute >= self.per_minute:
            return False

        one_hour_ago = now - 3600
        recent_hour = sum(1 for t in timestamps if t > one_hour_ago)
        if recent_hour >= self.per_hour:
            return False

        timestamps.append(now)
        return True

    def cleanup(self) -> None:
        cutoff = time.time() - 3600
        to_delete = []
        for ip, timestamps in self._requests.items():
            self._requests[ip] = [t for t in timestamps if t > cutoff]
            if not self._requests[ip]:
                to_delete.append(ip)
        for ip in to_delete:
            del self._requests[ip]
