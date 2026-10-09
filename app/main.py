import asyncio
import logging
import json
import re
import time
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware

from app import deep_health
from app.config import Settings
from app.executor import (
    ExecutionResult,
    InputTooLargeForWorker,
    SupervisorBusy,
    SupervisorUnavailable,
    execute_magma,
)
from app.parser import TRUNCATION_WARNING, parse_magma_output, parse_stderr_warnings
from app.ratelimit import RateLimiter
from app.submission_logger import SubmissionLogger
from app.usage_logger import UsageLogger


settings = Settings()
rate_limiter = RateLimiter(
    per_minute=settings.rate_limit_per_minute,
    per_hour=settings.rate_limit_per_hour,
)
semaphore = asyncio.Semaphore(settings.max_concurrent)
# Counts executing submissions; health probes share the semaphore but are excluded.
_in_flight = 0
usage_logger = UsageLogger(settings.usage_log_file)
submission_logger = SubmissionLogger(settings.submission_log_file)

logger = logging.getLogger("calculator")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_periodic_cleanup())
    yield
    task.cancel()


async def _periodic_cleanup():
    while True:
        await asyncio.sleep(300)
        rate_limiter.cleanup()
        usage_logger.prune_24h()


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


# CORS configuration
_allow_all_origins = "*" in settings.allowed_origins_list
_allow_localhost = "http://localhost" in settings.allowed_origins_list
_fixed_origins = [
    o for o in settings.allowed_origins_list
    if o not in ("*", "http://localhost")
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all_origins else _fixed_origins,
    allow_origin_regex=r"^http://localhost(:\d+)?$" if _allow_localhost else None,
    allow_methods=["GET", "POST"],  # the only methods any route accepts
    allow_headers=["*"],
    expose_headers=["Retry-After"],
    max_age=3600,
)


class ExecuteRequest(BaseModel):
    code: str


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_MEMORY_UNIT_SCALE = {"B": 1 / 1024 / 1024, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}
_RE_MEMORY_VALUE = re.compile(r"(\d+\.?\d*)([A-Z]+)")


def _memory_mb(memory: str | None) -> float | None:
    """Magma's footer reports memory as e.g. "12.34MB"; normalize to a number."""
    if not memory:
        return None
    m = _RE_MEMORY_VALUE.match(memory)
    if not m:
        return None
    scale = _MEMORY_UNIT_SCALE.get(m.group(2))
    if scale is None:
        return None
    return round(float(m.group(1)) * scale, 3)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/deep")
async def health_deep():
    status_code, body = await deep_health.check(settings, semaphore)
    return JSONResponse(status_code=status_code, content=body)


@app.get("/stats")
async def stats():
    return usage_logger.stats()


def _reject(status: int, reason: str, message: str, client_ip: str, input_size: int, headers=None):
    """Answer a request turned away before admission; logged only, so /stats is unchanged."""
    logger.info(json.dumps({
        "event": "rejected",
        "timestamp": _utc_timestamp(),
        "client_ip": client_ip,
        "input_size": input_size,
        "status": status,
        "reason": reason,
    }))
    # Metadata only, never code: code is kept once a request reaches the
    # executor; the local no-free-slot 503, 429, and the 50 KB 413 never do.
    submission_logger.log_rejected(uuid.uuid4().hex, client_ip, reason, input_size)
    return JSONResponse(status_code=status, content={"error": message}, headers=headers)


# The status, reason and message for each failure the executor raises.
_EXECUTOR_REJECTIONS = {
    SupervisorBusy: (503, "busy", "All execution slots busy"),
    SupervisorUnavailable: (503, "unavailable", "Execution service unavailable"),
    InputTooLargeForWorker: (413, "too_large", "Input too large"),
}


@app.post("/execute")
async def execute(req: ExecuteRequest, request: Request):
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"

    # Check input size
    if len(req.code.encode("utf-8")) > settings.magma_input_bytes:
        return _reject(413, "too_large", "Input too large", client_ip, len(req.code))

    # Check rate limit
    if not rate_limiter.is_allowed(client_ip):
        return _reject(429, "rate_limited", "Rate limit exceeded", client_ip, len(req.code),
                       headers={"Retry-After": "60"})

    # Try to acquire concurrency slot without blocking
    acquired = not semaphore.locked()
    if not acquired:
        return _reject(503, "busy", "All execution slots busy", client_ip, len(req.code))

    # Persisted before execution so that a run which never returns still
    # leaves a record; the completion line below carries the same request_id.
    request_id = uuid.uuid4().hex
    arrival = {
        "event": "start",
        "request_id": request_id,
        "timestamp": _utc_timestamp(),
        "client_ip": client_ip,
        "input_size": len(req.code),
    }
    logger.info(json.dumps(arrival))
    usage_logger.log(arrival)
    submission_logger.log_arrival(request_id, client_ip, req.code)
    # Other requests already holding a slot when this one was admitted: a
    # cheap concurrency signal for the completion line below.
    in_flight_at_admission = _in_flight

    # An exception leaves these defaults in place, so both logs still
    # record exactly one outcome for every admitted request: never just a
    # silent arrival, even for an exception _run() does not recognize.
    outcome = {"status": 500, "reason": "error", "memory_used": None, "success": False, "warnings": []}
    submission_outcome = {"outcome": "error"}
    try:
        return await _run(req.code, outcome, submission_outcome)
    finally:
        elapsed = round(time.time() - start_time, 3)
        completion = {
            "event": "end",
            "request_id": request_id,
            "timestamp": _utc_timestamp(),
            "client_ip": client_ip,
            "input_size": len(req.code),
            "elapsed_sec": elapsed,
            **outcome,
        }
        logger.info(json.dumps(completion))
        usage_logger.log(completion)
        submission_logger.log_completion(
            request_id, elapsed=elapsed, in_flight_at_admission=in_flight_at_admission, **submission_outcome,
        )


async def _run(code: str, outcome: dict, submission_outcome: dict):
    """The /execute reply for admitted code; fills outcome and submission_outcome
    for the usage and submission completion records the outer finally writes.
    """
    global _in_flight
    async with semaphore:
        _in_flight += 1
        try:
            result: ExecutionResult = await execute_magma(code, settings)
        except tuple(_EXECUTOR_REJECTIONS) as exc:
            status, reason, message = _EXECUTOR_REJECTIONS[type(exc)]
            outcome.update(status=status, reason=reason)
            submission_outcome.update(outcome=reason)
            return JSONResponse(status_code=status, content={"error": message})
        finally:
            _in_flight -= 1

    # Parse output
    parsed = parse_magma_output(result.stdout, settings.magma_output_bytes)
    # The worker caps stdout before the parser sees it, so the parser alone
    # cannot tell that the original output was longer.
    if result.truncated and not parsed.truncated:
        parsed.truncated = True
        parsed.warnings.append(TRUNCATION_WARNING)
    stderr_warnings = parse_stderr_warnings(
        result.stderr, timed_out=result.timed_out, seccomp_killed=result.seccomp_killed
    )
    all_warnings = parsed.warnings + stderr_warnings

    success = result.exit_code == 0 and not all_warnings

    response_data = {
        "success": success,
        "stdout": parsed.stdout,
        "exit_code": result.exit_code,
        "truncated": parsed.truncated,
        "magma": {
            "version": parsed.version,
            "seed": parsed.seed,
            "time_sec": parsed.time_sec,
            "memory": parsed.memory,
        },
        "warnings": all_warnings,
    }

    if not success:
        if stderr_warnings:
            response_data["error"] = stderr_warnings[0]
        elif parsed.warnings:
            response_data["error"] = parsed.warnings[0]
        else:
            response_data["error"] = f"Execution failed (exit code {result.exit_code})"
        response_data["warnings"] = [w for w in all_warnings if w != response_data.get("error")]

    # The footer is Magma's own report, lost whenever stdout is truncated or
    # the job is killed before printing it. The guest agent measures the
    # job independently of stdout, so that reply is preferred when present.
    time_sec = result.cpu_time_sec if result.cpu_time_sec is not None else parsed.time_sec
    memory_mb = (
        round(result.peak_memory_kb / 1024, 3)
        if result.peak_memory_kb is not None
        else _memory_mb(parsed.memory)
    )
    submission_outcome.update(
        outcome="completed",
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        seccomp_killed=result.seccomp_killed,
        time_sec=time_sec,
        memory_mb=memory_mb,
        stdout_bytes=len(parsed.stdout.encode("utf-8")),
        stdout_truncated=parsed.truncated,
        # What the executor returned before the app's own output-size
        # truncation. This is already bounded by the guest's own output cap,
        # so it is not the size before that cap.
        stdout_bytes_raw=len(result.stdout.encode("utf-8")),
        stderr_bytes=len(result.stderr.encode("utf-8")),
    )
    outcome.update(status=200, reason="completed", memory_used=parsed.memory, success=success, warnings=all_warnings)
    return response_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=settings.port,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )
