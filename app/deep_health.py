"""GET /health/deep: one real Magma job plus the worker's host checks."""

import asyncio
import json
import logging
import time

from app.config import Settings
from app.executor import SupervisorBusy, execute_magma
from app.parser import parse_magma_output

logger = logging.getLogger("calculator")

CACHE_SECONDS = 60
PROBE_TIMEOUT = 20
PROBE_CODE = "print 1+1;"
# Magma prints this, then the host's MACs, when the passfile rejects the machine.
LICENCE_REJECTED = "This host has the following MAC address"

_lock = asyncio.Lock()
_cached: dict = {}


def _status_file_problems(path: str) -> list[str]:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            status = json.load(fh)
        problems = [str(p) for p in status["problems"]]
        if time.time() > float(status["valid_until"]):
            problems.append("host-checks-stale")
        return problems
    except FileNotFoundError:
        return ["host-checks-missing"]
    except (OSError, ValueError, KeyError, TypeError):
        return ["host-checks-unreadable"]


async def _probe(settings: Settings, semaphore: asyncio.Semaphore) -> str:
    # Never wait for a slot: under load the service is evidently alive.
    if semaphore.locked():
        return "busy"
    probe_settings = settings.model_copy(
        update={"magma_timeout": PROBE_TIMEOUT, "magma_cpu_timeout": PROBE_TIMEOUT}
    )
    async with semaphore:
        try:
            result = await asyncio.wait_for(
                execute_magma(PROBE_CODE, probe_settings), timeout=PROBE_TIMEOUT + 10
            )
        except SupervisorBusy:
            return "busy"
        except asyncio.TimeoutError:
            return "timeout"
        except Exception:
            # Magma could not start (e.g. fork failed); report it like any failure.
            logger.exception("deep health probe could not run")
            return "error"
    if LICENCE_REJECTED in result.stdout or LICENCE_REJECTED in result.stderr:
        return "licence-rejected"
    parsed = parse_magma_output(result.stdout, settings.magma_output_bytes)
    if result.exit_code != 0 or parsed.stdout.strip() != "2":
        return "wrong-output"
    return "ok"


async def check(settings: Settings, semaphore: asyncio.Semaphore) -> tuple[int, dict]:
    """Return (HTTP status, body), running at most one probe per CACHE_SECONDS."""
    async with _lock:
        if _cached and time.monotonic() - _cached["at"] < CACHE_SECONDS:
            return _cached["reply"]
        probe = await _probe(settings, semaphore)
        problems = _status_file_problems(settings.health_status_file)
        if probe not in ("ok", "busy"):
            problems.insert(0, f"probe-{probe}")
            logger.warning("deep health probe failed: %s", probe)
        if problems:
            reply = (503, {"status": "fail", "probe": probe, "problems": problems})
        else:
            reply = (200, {"status": probe, "probe": probe, "problems": []})
        _cached.update(at=time.monotonic(), reply=reply)
        return reply
