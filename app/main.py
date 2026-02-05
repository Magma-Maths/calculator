import asyncio
import logging
import json
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings
from app.executor import execute_magma, ExecutionResult
from app.parser import parse_magma_output, parse_stderr_warnings
from app.ratelimit import RateLimiter

settings = Settings()
rate_limiter = RateLimiter(
    per_minute=settings.rate_limit_per_minute,
    per_hour=settings.rate_limit_per_hour,
)
semaphore = asyncio.Semaphore(settings.max_concurrent)

logger = logging.getLogger("calculator")
logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(docs_url=None, redoc_url=None)


# Custom CORS origin check: allow http://localhost with any port
_localhost_origins = [
    o for o in settings.allowed_origins_list if o == "http://localhost"
]
_fixed_origins = [
    o for o in settings.allowed_origins_list if o != "http://localhost"
]


def _origin_allowed(origin: str) -> bool:
    if origin in _fixed_origins:
        return True
    if _localhost_origins and re.match(r"^http://localhost(:\d+)?$", origin):
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
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Max-Age": "3600",
                },
            )
        return JSONResponse(content={"error": "Forbidden"}, status_code=403)

    response = await call_next(request)

    if origin and _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin

    return response


class ExecuteRequest(BaseModel):
    code: str


@app.get("/health")
async def health():
    return {"status": "ok"}


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

    # Acquire concurrency slot
    if semaphore.locked() and semaphore._value == 0:
        return JSONResponse(
            status_code=503,
            content={"error": "All execution slots busy"},
        )

    async with semaphore:
        result: ExecutionResult = await execute_magma(req.code, settings)

    # Parse output
    parsed = parse_magma_output(result.stdout, settings.magma_output_bytes)
    stderr_warnings = parse_stderr_warnings(result.stderr)
    all_warnings = parsed.warnings + stderr_warnings

    success = result.exit_code == 0 and not stderr_warnings

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

    if not success and stderr_warnings:
        response_data["error"] = stderr_warnings[0]

    elapsed = time.time() - start_time
    logger.info(
        json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "client_ip": client_ip,
            "input_size": len(req.code),
            "elapsed_sec": round(elapsed, 3),
            "success": success,
            "warnings": all_warnings,
        })
    )

    return response_data


if __name__ == "__main__":
    import os
    import uvicorn

    ssl_kwargs = {}
    cert = settings.tls_cert_file
    key = settings.tls_key_file
    if os.path.exists(cert) and os.path.exists(key):
        ssl_kwargs["ssl_certfile"] = cert
        ssl_kwargs["ssl_keyfile"] = key

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        **ssl_kwargs,
    )
