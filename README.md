# Magma Calculator Service

REST API backend for the [Magma](http://magma.maths.usyd.edu.au/) online calculator. Runs each Magma job in its own [Firecracker](https://firecracker-microvm.github.io/) microVM, through a supervisor on the host, and returns structured JSON results.

## Protocol

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

When warnings are present (timeout, runtime error, output truncation), `success` is `false`, `error` carries the first one (a stderr warning before an output warning), and `warnings` lists the rest:

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

A failure with no warning still gets an `error`: `Execution failed (exit code N)`.

**Error responses** return `{"error": "..."}` with no other fields:

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

Runs `print 1+1;` through the configured executor and, when `HEALTH_STATUS_FILE` is set, reads the problems the host checks wrote there. The result is cached for 60 seconds, so at most one probe job runs per minute. The endpoint does not count against the rate limit. When every slot is busy it answers `busy` without running a job.

```json
{"status": "ok", "probe": "ok", "problems": []}
```

`status` is `ok` or `busy` with HTTP 200, or `fail` with HTTP 503 and the reasons in `problems` (for example `probe-wrong-output`, `probe-rejected`, `host-checks-stale`). The host checks write `{"problems": [...], "valid_until": <unix time>}`; a missing, unreadable or expired file counts as a failure.

`HEALTH_REJECT_PATTERN` defaults to empty. Set it to a literal substring to reject probe stdout or stderr containing that text, reporting `probe-rejected`.

### GET /stats

Returns aggregated usage statistics (all-time and last 24 hours), computed from the JSON-lines file at `USAGE_LOG_FILE`. Each `/execute` request that is admitted for execution writes two lines there, sharing a `request_id`: an `"event": "start"` line on arrival (timestamp, client IP, input size) and an `"event": "end"` line on completion with the outcome. Only completion lines feed the statistics, so a run that never returns leaves its arrival line and no count.

Each line is also printed as JSON to the service log. A completion line carries the HTTP `status` and a `reason`: `completed`, `busy`, `unavailable`, `too_large` or `error`. A request turned away before admission prints only an `"event": "rejected"` line, with `status` and `reason` (`too_large`, `rate_limited` or `busy`), and leaves `USAGE_LOG_FILE` and the statistics untouched.

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

By default CORS is not enforced: all origins are allowed (`ALLOWED_ORIGIN=*`). To restrict, set `ALLOWED_ORIGIN` to a comma-separated list of origins (e.g. `https://magma-maths.org,http://localhost`). The special value `http://localhost` matches any port.

## Deployment

To host the service yourself, follow [INSTALL.md](INSTALL.md); see [FIRECRACKER.md](FIRECRACKER.md) for the deeper operator reference.

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

Each job runs in its own freshly booted Firecracker microVM, started by a root supervisor on the host. The API container only ever talks to the supervisor's UNIX socket (`SUPERVISOR_SOCKET`) and never runs Magma itself. Inside the guest, Magma runs with the `-w` flag, a restricted mode that disables `System()`, `Pipe()`, `Open()` and other dangerous intrinsics, under the seccomp allow-list in `firecracker/guest/seccomp_policy.py`. A job that filter kills returns `exit_code` -1 and an error saying it made a system call the sandbox does not allow.

See [FIRECRACKER.md](FIRECRACKER.md) for the worker layout, protocols, and manual checks. Operators can supply a guest site hook, per-slot boot arguments, and a kernel config fragment.

### Submission log

Separately from the usage log, setting `SUBMISSION_LOG_FILE` to a path (written with mode 0600) keeps the full code of every `/execute` request, once it reaches the executor, together with the client IP and the outcome (`exit_code`, `timed_out`, `seccomp_killed`, elapsed time, CPU time and memory, stdout and stderr size). Code is written at arrival, before execution, so a run that hangs or kills the worker still leaves it; a completion line with the same `request_id` always follows once the request is done, including a failure this service does not specifically recognize.

Rejected before reaching the executor (413 over the 50 KB input limit, 429 rate limited, or 503 with no free local slot) logs client IP, reason, and input size only, never code, and no completion line. Every other request keeps its code, including one the Firecracker worker later rejects for still being too large once framed: that is one of the things this log exists to catch.

Nothing in the service reads this file back; it exists only for a human to grep after an incident, and it never leaves the machine. The log is off by default, and rotating and deleting it is the operator's job. A write error is transient and does not stop later writes; only a startup failure disables it for the life of the process.

## Development

```bash
poetry install           # install dependencies
poetry run pytest -v     # run tests
SUPERVISOR_SOCKET=/path/to/supervisor.sock poetry run python -m app.main   # serve on port 8080
```

Without a supervisor listening on that socket, `/execute` answers 503.
