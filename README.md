# Magma Calculator Service

This is a self-hosted REST API for the [Magma](http://magma.maths.usyd.edu.au/) online calculator. Each job runs in a [Firecracker](https://firecracker-microvm.github.io/) microVM. An unprivileged API container sends jobs to a host supervisor over a UNIX socket.

To host the service, follow [INSTALL.md](INSTALL.md). [FIRECRACKER.md](FIRECRACKER.md) describes the worker and its security boundaries.

## API

### POST /execute

Execute Magma code.

**Request:**
```http
POST /execute HTTP/1.1
Content-Type: application/json

{"code": "print 1+1;"}
```

**Success response (200):**
```json
{
  "success": true,
  "stdout": "2\n",
  "exit_code": 0,
  "truncated": false,
  "magma": {
    "version": "2.29-4",
    "seed": 3847219456,
    "time_sec": 0.05,
    "memory": "12.34MB"
  },
  "warnings": []
}
```

When warnings occur (for example, a timeout, runtime error, or output truncation), `success` is `false`. The `error` field contains the first warning, preferring a stderr warning over an output warning; `warnings` contains the remaining warnings.

```json
{
  "success": false,
  "stdout": "...",
  "exit_code": 1,
  "truncated": false,
  "magma": {
    "version": "2.29-4",
    "seed": 3847219456,
    "time_sec": null,
    "memory": null
  },
  "warnings": [],
  "error": "Runtime error in Magma"
}
```

A failure without a warning still has an `error` field: `Execution failed (exit code N)`.

Error responses contain only `{"error": "..."}`:

| Status | Meaning | Notes |
|--------|---------|-------|
| 413 | Input too large | Exceeds `MAGMA_INPUT_KB`, or the Firecracker request frame once JSON-escaped |
| 422 | Missing `code` field | FastAPI validation error |
| 429 | Rate limit exceeded | Includes `Retry-After: 60` header, readable from browser scripts through `Access-Control-Expose-Headers` |
| 503 | All execution slots busy | Try again later; also returned while the Firecracker supervisor is unreachable |

### GET /health

```json
{"status": "ok"}
```

### GET /health/deep

Runs `print 1+1;` through the configured executor and, if `HEALTH_STATUS_FILE` is set, includes problems reported by host checks. Results are cached for 60 seconds, and the endpoint does not count against the rate limit. If all slots are busy, it returns `busy` without running a probe.

```json
{"status": "ok", "probe": "ok", "problems": []}
```

`status` is `ok` or `busy` with HTTP 200, or `fail` with HTTP 503 and reasons in `problems` (such as `probe-wrong-output`, `probe-rejected`, or `host-checks-stale`). Host checks write `{"problems": [...], "valid_until": <unix time>}`. A missing, unreadable, or expired status file counts as a failure.

`HEALTH_REJECT_PATTERN` defaults to empty. Set it to a literal substring to report `probe-rejected` when probe stdout or stderr contains it.

### GET /stats

Returns all-time and last-24-hour usage statistics from `USAGE_LOG_FILE`, a JSON-lines file. An admitted `/execute` request writes an `"event": "start"` line with its timestamp, client IP, and input size, then an `"event": "end"` line on completion. Both share a `request_id`; only completion lines count toward statistics. Each line is also printed as JSON to the service log.

Completion lines include HTTP `status` and a `reason`: `completed`, `busy`, `unavailable`, `too_large`, or `error`. A request rejected before admission prints an `"event": "rejected"` line with `status` and a reason (`too_large`, `rate_limited`, or `busy`); it does not change `USAGE_LOG_FILE` or the statistics.

```json
{
  "all_time": {
    "total_requests": 1234,
    "unique_ips": 56,
    "avg_elapsed_sec": 2.3,
    "successes": 1200,
    "failures": 34
  },
  "last_24h": {
    "total_requests": 42,
    "unique_ips": 10,
    "avg_elapsed_sec": 1.8,
    "successes": 40,
    "failures": 2
  }
}
```

### CORS

CORS allows all origins by default (`ALLOWED_ORIGIN=*`). Set `ALLOWED_ORIGIN` to a comma-separated list to restrict access (for example, `https://example.org,http://localhost`). The value `http://localhost` matches any port.

## Configuration

The service reads these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAGMA_TIMEOUT` | 120 | Wall-clock timeout (seconds) |
| `MAGMA_CPU_TIMEOUT` | 120 | CPU time limit (seconds) |
| `MAGMA_INPUT_KB` | 50 | Max input size (KB) |
| `MAGMA_OUTPUT_KB` | 20 | Max output size (KB) |
| `SUPERVISOR_SOCKET` | `/run/magma-fc/supervisor.sock` | UNIX socket of the Firecracker supervisor that runs each job |
| `MAX_CONCURRENT` | 4 | Simultaneous execution slots |
| `PORT` | 8080 | Listen port inside container |
| `FORWARDED_ALLOW_IPS` | `172.30.0.2` | Comma-separated proxy addresses whose `X-Forwarded-For` gives the client address; the default is the worker's reverse proxy |
| `RATE_LIMIT_PER_MINUTE` | 30 | Requests per IP per minute |
| `RATE_LIMIT_PER_HOUR` | 200 | Requests per IP per hour |
| `ALLOWED_ORIGIN` | `*` | CORS origins (`*` for all, or comma-separated list) |
| `USAGE_LOG_FILE` | `/data/usage.jsonl` | Path for persistent usage log (JSON lines) |

## Security

The API container sends jobs to a root-owned host supervisor through `SUPERVISOR_SOCKET`; it does not run Magma. Each job uses its own Firecracker microVM, which is torn down after that job. Inside the guest, Magma runs with `-w`, a restricted mode, and a seccomp filter. If the filter kills a job for a disallowed system call, the response has `exit_code` -1 and an explanatory error.

Each free slot has a [warm guest](FIRECRACKER.md#warm-pool), booted and connected before the supervisor hands it a job's code. After the job completes, the supervisor stops and cleans up the guest, then boots its replacement.

### Submission log

Set `SUBMISSION_LOG_FILE` to enable a mode-0600 log of requests that reach the executor. Each admitted request records its code and client IP before execution, then a completion record with the same `request_id`. The outcome includes `exit_code`, `timed_out`, `seccomp_killed`, elapsed and CPU time, memory, and output sizes.

Requests rejected before execution include 413, 429, and 503 when no local slot is free. The service log prints an `"event": "rejected"` line. The submission log stores only client IP, reason, and input size; these requests omit code and have no completion record. A worker-side rejection for an oversized framed request is recorded with its code. The service does not read this file; it is local, off by default, and the operator manages rotation and deletion. A write error does not stop later writes; a startup failure disables logging for that process.

## Development

```bash
poetry install           # install dependencies
poetry run pytest -v     # run tests
SUPERVISOR_SOCKET=/path/to/supervisor.sock poetry run python -m app.main   # serve on port 8080
```

Without a supervisor listening on that socket, `/execute` returns 503.
