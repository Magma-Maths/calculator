import time
from app.ratelimit import RateLimiter


def test_allows_first_request():
    limiter = RateLimiter(per_minute=5, per_hour=100)
    assert limiter.is_allowed("1.2.3.4") is True


def test_blocks_after_per_minute_limit():
    limiter = RateLimiter(per_minute=3, per_hour=100)
    for _ in range(3):
        assert limiter.is_allowed("1.2.3.4") is True
    assert limiter.is_allowed("1.2.3.4") is False


def test_different_ips_independent():
    limiter = RateLimiter(per_minute=2, per_hour=100)
    assert limiter.is_allowed("1.1.1.1") is True
    assert limiter.is_allowed("1.1.1.1") is True
    assert limiter.is_allowed("1.1.1.1") is False
    assert limiter.is_allowed("2.2.2.2") is True


def test_blocks_after_per_hour_limit():
    limiter = RateLimiter(per_minute=1000, per_hour=5)
    for _ in range(5):
        assert limiter.is_allowed("1.2.3.4") is True
    assert limiter.is_allowed("1.2.3.4") is False


def test_cleanup_removes_old_entries():
    limiter = RateLimiter(per_minute=1000, per_hour=1000)
    limiter.is_allowed("1.2.3.4")
    assert "1.2.3.4" in limiter._requests
    limiter._requests["1.2.3.4"] = [time.time() - 7200]
    limiter.cleanup()
    assert "1.2.3.4" not in limiter._requests
