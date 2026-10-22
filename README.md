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
| 429 | Too many simultaneous jobs from this IP | Retry after a running job finishes; no fixed retry delay |
| 503 | All execution slots busy | Try again later; also returned while the Firecracker supervisor is unreachable |

Each IP can run two jobs simultaneously by default. `MAX_CONCURRENT_PER_IP`
sets a positive cap per API process; clients sharing a public IP share that
cap. `MAX_CONCURRENT` still limits total jobs, with no waiting queue.
Attempts rejected by either concurrency limit still count toward the rate
limits. Configure `FORWARDED_ALLOW_IPS` for your trusted reverse proxy so
these limits use the client address.

Closing the HTTP connection cancels the job. Capacity stays occupied until
the supervisor finishes guest teardown. Run matching API and supervisor
versions to support prompt cancellation.

### GET /health

```json
{"status": "ok"}
```

### GET /version

Returns the calculator build revision and the latest Magma version observed in an executor reply, including deep-health probes. Reading `/version` does not run a job.
`calculator` defaults to `"unknown"`; `magma` is `null` until a reply carries a version. Replies without a version preserve the last observed value.
Like `/health`, this endpoint has no rate limit or usage log entry and follows `ALLOWED_ORIGIN` for browser access.

```json
{"calculator": "abc123", "magma": "2.29-4"}
```

### GET /info

Returns `schema_version: 1`, `limits`, and `retention`, with `Cache-Control: no-store` and the same CORS policy as `/version`.
It consumes no job slot, runs no computation, and does not count toward rate limits or usage statistics.

Every field below is present. `null` means unknown or unconfirmed, never unlimited.

| Field in `limits` | Meaning |
| --- | --- |
| `wall_time_seconds`, `cpu_time_seconds` | Configured execution ceilings, confirmed against the supervisor's accepted timeout |
| `guest_memory_bytes` | Enforced guest RAM, including the guest OS and agent |
| `input_bytes`, `output_bytes` | UTF-8 input and captured stdout caps, in bytes |
| `requests_per_minute`, `requests_per_hour` | Rolling per-IP attempt limits, per API process; zero rejects every attempt |
| `max_concurrent_per_ip` | Simultaneous running jobs per IP, from `MAX_CONCURRENT_PER_IP` |
| `queue_capacity` | Always `0`: busy jobs are rejected without a waiting queue |

Memory and timeout ceilings come from a small read-only supervisor operation.
The API sends it at most once every 10 seconds and reuses the reply, or the failure, in between.
An unreachable or older supervisor leaves the affected fields `null`.
A timeout above the supervisor's ceiling, or an output cap above its accepted maximum, also reports `null` for that field.
`input_bytes` remains available because the API enforces it directly.
JSON escaping and the worker's frame size can reject some inputs below this cap.
The stdout cap includes the Magma banner and footer; truncation does not stop computation.
Ceilings describe limits, not guaranteed computing time or available heap space.

The `retention` fields describe logging policy:

- `code_logged`: whether `SUBMISSION_LOG_FILE` is configured.
- `ip_logged`: `true`, because request metadata also goes to the service log, even when file logging is disabled.
- `submission_min_days`: `LOG_RETENTION_DAYS` after a successful cleanup of both file logs, or `null` if unset, cleanup failed, or code logging is disabled.

Logging booleans do not guarantee that every write succeeds.
Days mean 24 hours. Backups and separately managed service logs may retain data longer.
See [record retention](#record-retention) for cleanup scope and timing.

### GET /health/deep

Runs `print 1+1;` through the configured executor and, if `HEALTH_STATUS_FILE` is set, includes problems reported by host checks. Results are cached for 60 seconds, and the endpoint does not count against the rate limit. If all slots are busy, it returns `busy` without running a probe.

```json
{"status": "ok", "probe": "ok", "problems": []}
```

`status` is `ok` or `busy` with HTTP 200, or `fail` with HTTP 503 and reasons in `problems` (such as `probe-wrong-output`, `probe-rejected`, or `host-checks-stale`). Host checks write `{"problems": [...], "valid_until": <unix time>}`. A missing, unreadable, or expired status file counts as a failure.

`HEALTH_REJECT_PATTERN` defaults to empty. Set it to a literal substring to report `probe-rejected` when probe stdout or stderr contains it.

### GET /stats

Returns usage statistics for the retained window and the last 24 hours.
The service replays completion records from `USAGE_LOG_FILE` at startup and updates both windows as requests complete.
An admitted `/execute` request writes an `"event": "start"` line with its timestamp, client IP, and input size, then an `"event": "end"` line on completion.
Both share a `request_id`; only completion lines count toward statistics. Each line is also printed as JSON to the service log.

`retained.days` is `LOG_RETENTION_DAYS`; records older than that window drop out of statistics, including unique-IP counts.
With retention unset, `days` is `null` and the retained window is unbounded, covering every valid completion in the file.
`last_24h` covers the last 24 hours within the retained records. Both windows include records exactly at their cutoff.
Each window reports its own `unique_ips` count; no IP list is exposed.
The service keeps timestamp, elapsed time, success, and IP in memory only for retained completions.
It discards expired completions on logging, statistics reads, and periodic cleanup, even if a file rewrite fails.

Completion lines include HTTP `status` and a `reason`: `completed`, `busy`, `unavailable`, `too_large`, `error`, or `disconnected`. A disconnected request records status 499. A request rejected before admission prints an `"event": "rejected"` line with `status` and a reason (`too_large`, `rate_limited`, `ip_busy`, or `busy`); it does not change `USAGE_LOG_FILE` or the statistics.

```json
{
  "retained": {
    "days": 90,
    "total_requests": 1234,
    "unique_ips": 123,
    "avg_elapsed_sec": 2.3,
    "successes": 1200,
    "failures": 34
  },
  "last_24h": {
    "total_requests": 42,
    "unique_ips": 12,
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
| `CALCULATOR_VERSION` | `unknown` | Calculator build revision returned by `/version` |
| `MAX_CONCURRENT` | 4 | Simultaneous execution slots |
| `MAX_CONCURRENT_PER_IP` | 2 | Simultaneous jobs per IP per API process; positive integer |
| `PORT` | 8080 | Listen port inside container |
| `FORWARDED_ALLOW_IPS` | `172.30.0.2` | Comma-separated proxy addresses whose `X-Forwarded-For` gives the client address; the default is the worker's reverse proxy |
| `RATE_LIMIT_PER_MINUTE` | 30 | Requests per IP per minute |
| `RATE_LIMIT_PER_HOUR` | 200 | Requests per IP per hour |
| `ALLOWED_ORIGIN` | `*` | CORS origins (`*` for all, or comma-separated list) |
| `USAGE_LOG_FILE` | `/data/usage.jsonl` | Path for persistent usage log (JSON lines) |
| `SUBMISSION_LOG_FILE` | empty | Optional path for submitted source and request metadata |
| `LOG_RETENTION_DAYS` | unset | Positive integer days to retain both raw file logs; unset disables automatic deletion |

## Security

The API container sends jobs to a root-owned host supervisor through `SUPERVISOR_SOCKET`; it does not run Magma. Each job uses its own Firecracker microVM, which is torn down after that job. Inside the guest, Magma runs with `-w`, a restricted mode, and a seccomp filter. If the filter kills a job for a disallowed system call, the response has `exit_code` -1 and an explanatory error.

Each free slot has a [warm guest](FIRECRACKER.md#warm-pool), booted and connected before the supervisor hands it a job's code. After the job completes, the supervisor stops and cleans up the guest, then boots its replacement.

### Submission log

Set `SUBMISSION_LOG_FILE` to enable a mode-0600 log of requests that reach the executor. Each admitted request records its code and client IP before execution, then a completion record with the same `request_id`. The outcome includes `exit_code`, `timed_out`, `seccomp_killed`, elapsed and CPU time, memory, and output sizes.

Requests rejected before execution include 413, 429, and 503 when no local slot is free. The service log prints an `"event": "rejected"` line. The submission log stores only client IP, reason, and input size; these requests omit code and have no completion record. A worker-side rejection for an oversized framed request is recorded with its code. The file is local and off by default. A write error does not stop later writes; a startup failure disables logging for that process.

### Record retention

Set `LOG_RETENTION_DAYS=N` to expire raw records from both `USAGE_LOG_FILE` and `SUBMISSION_LOG_FILE` after N days.
The service streams and atomically rewrites these files at startup and every five minutes, including during idle periods.
Each sweep removes records older than N days, along with malformed records or records without a valid UTC timestamp.
Recent records remain, with mode 0600. Cleanup and appends share a lock; appends wait while their file is rewritten.
Logging and statistics reads wait in worker threads so other endpoints remain responsive during rewrites.
Each sweep takes time proportional to the log size and needs space for one rewritten file.
Failed sweeps report `submission_min_days: null` and retry at the next interval.
Unconfigured retention reports `null` and leaves raw file deletion to the operator.

Standard numbered and date-suffixed archives in the same directory also receive cleanup:
`submissions.jsonl.1`, `submissions.jsonl.2.gz`, and `submissions.jsonl-20261016.gz`, for example.
Cleanup removes temporary files left by interrupted sweeps.
Give the service ownership of the log directory and access to existing archives.
Remove external rotation and deletion rules for these files when enabling service retention.
Rotation by rename between sweeps remains supported; concurrent external rotation can make a sweep fail and retry.
Archives in other directories or with custom names require separate operator cleanup.

Statistics use the active usage file, with no separate persistent store.
Only completions with a valid UTC timestamp count; arrival records and malformed lines do not.
Rotated archives receive cleanup but are not replayed into statistics.
Renaming or deleting the active file can therefore reduce totals after a restart.
An empty `USAGE_LOG_FILE` disables that raw file; statistics then cover only completions observed by the current process.
A failed raw write still counts in that process but cannot be recovered after restart.

If cleanup fails, `/info` reports `submission_min_days: null` until a later sweep succeeds.
`/stats` continues to apply its configured window in memory; `retained.days` describes that window, not successful disk deletion.
With retention unset, raw files and retained statistics have no automatic age limit.
Memory use grows with the number of retained completions, and each statistics read scans them.
Existing aggregate database files are unused and can be removed by the operator.

Run one API process per log directory so its append locks cover every writer.
This policy covers the two file logs and their standard archives.
Configure retention for service stdout, proxy logs, and backups separately.

## Development

Include the checkout revision when building the image: `docker build --build-arg CALCULATOR_VERSION="$(git rev-parse HEAD)" .`.

```bash
poetry install           # install dependencies
poetry run pytest -v     # run tests
SUPERVISOR_SOCKET=/path/to/supervisor.sock poetry run python -m app.main   # serve on port 8080
```

Without a supervisor listening on that socket, `/execute` returns 503.
