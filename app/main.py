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

from app.config import Settings
from app.executor import execute_magma, ExecutionIOError, ExecutionResult
from app.parser import parse_magma_output, parse_stderr_warnings
from app.ratelimit import RateLimiter
from app.usage_logger import UsageLogger

settings = Settings()
rate_limiter = RateLimiter(
    per_minute=settings.rate_limit_per_minute,
    per_hour=settings.rate_limit_per_hour,
)
semaphore = asyncio.Semaphore(settings.max_concurrent)
usage_logger = UsageLogger(settings.usage_log_file)

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


def _origin_allowed(origin: str) -> bool:
    if _allow_all_origins:
        return True
    if origin in _fixed_origins:
        return True
    if _allow_localhost and re.match(r"^http://localhost(:\d+)?$", origin):
        return True
    return False


@app.middleware("http")
async def cors_middleware(request: Request, call_next):
    origin = request.headers.get("origin", "")

    if request.method == "OPTIONS":
        if _origin_allowed(origin):
            return JSONResponse(
                content="",
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": origin or "*",
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Max-Age": "3600",
                },
            )
        return JSONResponse(content={"error": "Forbidden"}, status_code=403)

    response = await call_next(request)

    if _allow_all_origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    return response


class ExecuteRequest(BaseModel):
    code: str


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_ANSI_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[A-Za-z])"
)


def _stderr_preview(stderr: str) -> str:
    clean = _ANSI_SEQUENCE.sub("", stderr)
    clean = "".join(char if char.isprintable() else " " for char in clean)
    clean = " ".join(clean.split())
    return clean.encode("utf-8")[:2048].decode("utf-8", errors="ignore")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stats")
async def stats():
    return usage_logger.stats()


@app.post("/execute")
async def execute(req: ExecuteRequest, request: Request):
    start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"

    # Check input size
    if len(req.code.encode("utf-8")) > settings.magma_input_bytes:
        return JSONResponse(
            status_code=413,
            content={"error": "Input too large"},
        )

    # Check rate limit
    if not rate_limiter.is_allowed(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded"},
            headers={"Retry-After": "60"},
        )

    # Try to acquire concurrency slot without blocking
    acquired = semaphore.locked() is False or semaphore._value > 0
    if not acquired:
        return JSONResponse(
            status_code=503,
            content={"error": "All execution slots busy"},
        )

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

    try:
        async with semaphore:
            result: ExecutionResult = await execute_magma(req.code, settings)
    except (OSError, ExecutionIOError) as exc:
        launch_failure = isinstance(exc, OSError)
        error = (
            "Failed to launch the computation."
            if launch_failure else "Execution I/O failed before a child result was available."
        )
        log_entry = {
            "event": "end", "request_id": request_id,
            "timestamp": _utc_timestamp(), "client_ip": client_ip,
            "input_size": len(req.code),
            "elapsed_sec": round(time.time() - start_time, 3),
            "memory_used": None, "success": False,
            "warnings": [error], "error": error,
        }
        logger.info(json.dumps(log_entry))
        usage_logger.log(log_entry)
        return JSONResponse(status_code=503 if launch_failure else 502, content={"error": error})

    # Parse output
    parsed = parse_magma_output(result.stdout, settings.magma_output_bytes)
    stderr_warnings = (
        [] if result.limit_reason == "output_limit"
        else parse_stderr_warnings(result.stderr)
    )
    all_warnings = parsed.warnings + stderr_warnings

    error = None
    if result.limit_reason == "output_limit":
        ceiling = (
            "stderr capture" if result.limit_detail == "stderr_capture"
            else "combined capture"
        )
        error = f"The {ceiling} ceiling was exceeded."
        all_warnings.append(error)
    elif result.limit_reason == "wall_timeout":
        error = "The computation exceeded the time limit and so was terminated prematurely."
        if error not in all_warnings:
            all_warnings.append(error)
    elif result.exit_code != 0:
        preview = _stderr_preview(result.stderr)
        detail = preview if preview else "no stderr was captured"
        error = f"The computation exited with code {result.exit_code}: {detail}."
        all_warnings.append(error)

    success = result.exit_code == 0 and not all_warnings and result.limit_reason is None

    response_data = {
        "success": success,
        "stdout": parsed.stdout,
        "exit_code": result.exit_code,
        "truncated": parsed.truncated or result.limit_reason == "output_limit",
        "magma": {
            "version": parsed.version,
            "seed": parsed.seed,
            "time_sec": parsed.time_sec,
            "memory": parsed.memory,
        },
        "warnings": all_warnings,
    }

    if not success:
        if error:
            response_data["error"] = error
        elif stderr_warnings:
            response_data["error"] = stderr_warnings[0]
        elif parsed.warnings:
            response_data["error"] = parsed.warnings[0]

    elapsed = time.time() - start_time
    log_entry = {
        "event": "end",
        "request_id": request_id,
        "timestamp": _utc_timestamp(),
        "client_ip": client_ip,
        "input_size": len(req.code),
        "elapsed_sec": round(elapsed, 3),
        "memory_used": parsed.memory,
        "success": success,
        "warnings": all_warnings,
    }
    if "error" in response_data:
        log_entry["error"] = response_data["error"]
    logger.info(json.dumps(log_entry))
    usage_logger.log(log_entry)

    return response_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)
