# Magma Calculator Service

REST API backend for the [Magma](http://magma.maths.usyd.edu.au/) online calculator. Executes Magma code inside an [nsjail](https://github.com/google/nsjail) sandbox and returns structured JSON results.

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

When warnings are present (timeout, runtime error, output truncation), `success` is `false` and an `error` field is added with the first warning:

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
  "warnings": ["Runtime error in Magma"],
  "error": "Runtime error in Magma"
}
```

**Error responses** return `{"error": "..."}` with no other fields:

| Status | Meaning | Notes |
|--------|---------|-------|
| 413 | Input too large | Exceeds `MAGMA_INPUT_KB` |
| 422 | Missing `code` field | FastAPI validation error |
| 429 | Rate limit exceeded | Includes `Retry-After: 60` header |
| 503 | All execution slots busy | Try again later |

### GET /health

```json
{"status": "ok"}
```

### GET /stats

Returns aggregated usage statistics (all-time and last 24 hours). Each successful `/execute` request is logged to the file at `USAGE_LOG_FILE`.

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

## Setup

> **First time on a fresh Ubuntu server?** See [DEPLOY.md](DEPLOY.md) for a complete walkthrough.

### Prerequisites

- **Docker** with support for `--cap-add SYS_ADMIN` (required for nsjail namespace creation)
- **Magma** installed on the host (default: `/opt/magma`)

### 1. Get the image

Every push to `main` builds the image in GitHub Actions and publishes it as `ghcr.io/magma-maths/calculator`, tagged `sha-<short>` (the first 7 hex characters of the commit) and the moving `main`. A `v*` git tag retags that commit's `sha-<short>` image as the version without rebuilding, so a release is the same bytes that already ran from `main`. The image contains no Magma; the host's copy is bind-mounted at run time.

Always run an exact tag: `sha-<short>` tags are immutable and `v*` tags are releases, so the version on a host is the one written down, and nothing changes it but you.

```bash
docker pull ghcr.io/magma-maths/calculator:v0.1.0
```

To build from source instead (the multi-stage Dockerfile compiles nsjail and installs the Python dependencies via Poetry), give the local build a tag of its own and use it in place of `v0.1.0` in the recipes below:

```bash
docker build -t ghcr.io/magma-maths/calculator:dev .
```

### 2. Configure

```bash
cp calculator.env.example calculator.env
```

Edit `calculator.env`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAGMA_ROOT` | `/opt/magma/current` | Magma version tree holding `magma.exe` and `magmapassfile`; must be under `/opt/magma` (the bind mount) |
| `MAGMA_TIMEOUT` | 120 | Wall-clock timeout (seconds) |
| `MAGMA_CPU_TIMEOUT` | 120 | CPU time limit (seconds) |
| `MAGMA_MEMORY_MB` | 400 | Memory limit (MB) |
| `MAGMA_INPUT_KB` | 50 | Max input size (KB) |
| `MAGMA_OUTPUT_KB` | 20 | Max output size (KB) |
| `MAX_CONCURRENT` | 4 | Simultaneous execution slots |
| `PORT` | 8080 | Listen port inside container |
| `RATE_LIMIT_PER_MINUTE` | 30 | Requests per IP per minute |
| `RATE_LIMIT_PER_HOUR` | 200 | Requests per IP per hour |
| `ALLOWED_ORIGIN` | `*` | CORS origins (`*` for all, or comma-separated list) |
| `USAGE_LOG_FILE` | `/data/usage.jsonl` | Path for persistent usage log (JSON lines) |

### 3a. Start Traefik (once per host)

Traefik runs as a shared reverse proxy. If you already have a Traefik instance on the host, skip this step; just make sure its Docker network is named `traefik`.

```bash
cd traefik
cp .env.example .env   # edit ACME_EMAIL
docker compose up -d
cd ..
```

This creates the `traefik` Docker network, binds ports 80/443, and handles Let's Encrypt certificates automatically. The dashboard is available on `127.0.0.1:8080`.

### 3b. Run with docker-compose (production)

```bash
cp .env.example .env   # set DOMAIN and CALCULATOR_VERSION
docker compose up -d
```

Compose runs `ghcr.io/magma-maths/calculator:$CALCULATOR_VERSION`, pulling it when it is not already on the host. There is deliberately no default: if the variable is unset, compose refuses to start and names it, so a host always states its version and a `pull` or restart never changes the running code on its own. The calculator joins the shared `traefik` network. Traefik discovers it via Docker labels and routes `https://$DOMAIN` to it. A named volume (`calculator-data`) persists usage logs across restarts.

To build from the working tree instead of pulling, add the dev override, which tags the build `calculator-dev` so a local build never takes over the pinned tag:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

`CALCULATOR_VERSION` must still be set, because compose interpolates the base file before applying the override, but its value is unused on this path.

### 3c. Run without docker-compose (testing)

```bash
docker run --rm \
  --cap-add SYS_ADMIN \
  --tmpfs /tmp:size=128m \
  -v /opt/magma:/opt/magma:ro \
  -p 8080:8080 \
  ghcr.io/magma-maths/calculator:v0.1.0
```

This runs the calculator on plain HTTP (port 8080) without Traefik or TLS.

### Running multiple instances

Start a second container with different limits for long-running computations:

```bash
docker run --rm \
  --cap-add SYS_ADMIN \
  --tmpfs /tmp:size=128m \
  -v /opt/magma:/opt/magma:ro \
  --env-file calculator-long.env \
  -p 8081:8080 \
  ghcr.io/magma-maths/calculator:v0.1.0
```

## Security

Each Magma process runs inside an nsjail sandbox with:

- **PID, mount, network, and UTS namespace isolation**: the process cannot see or interact with the host
- **No network access**: `clone_newnet` creates an empty network namespace
- **Read-only mounts**: Magma installation and system libraries are bind-mounted read-only
- **cgroup limits**: memory and CPU enforced at the kernel level
- **Magma `-w` flag**: restricted mode that disables `System()`, `Pipe()`, `Open()`, and other dangerous intrinsics at the Magma level (no keyword filtering)
- **Privilege drop**: nsjail runs as root to create namespaces, then drops to the `calculator` user for Magma execution

## Development

```bash
poetry install           # install dependencies
poetry run pytest -v     # run tests
```
