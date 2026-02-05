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

### CORS

By default CORS is not enforced — all origins are allowed (`ALLOWED_ORIGIN=*`). To restrict, set `ALLOWED_ORIGIN` to a comma-separated list of origins (e.g. `https://magma-maths.org,http://localhost`). The special value `http://localhost` matches any port.

## Setup

### Prerequisites

- **Docker** with support for `--cap-add SYS_ADMIN` (required for nsjail namespace creation)
- **Magma** installed on the host (default: `/opt/magma`)

### 1. Build the image

```bash
docker build -t magma-calculator .
```

The multi-stage Dockerfile compiles nsjail from source, installs Python dependencies via Poetry, and produces a slim runtime image.

### 2. Configure

```bash
cp calculator.env.example calculator.env
```

Edit `calculator.env`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
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
| `TLS_CERT_FILE` | `/certs/live/calc.magma-maths.org/fullchain.pem` | TLS certificate path |
| `TLS_KEY_FILE` | `/certs/live/calc.magma-maths.org/privkey.pem` | TLS private key path |

### 3a. Run with docker-compose (production)

```bash
docker compose up -d
```

This maps port 443 to the container, mounts `/opt/magma` read-only, and mounts `/etc/letsencrypt` for TLS certificates. See `docker-compose.yml` for details.

### 3b. Run without TLS (testing)

```bash
docker run --rm \
  --cap-add SYS_ADMIN \
  --tmpfs /tmp:size=128m \
  -v /opt/magma:/opt/magma:ro \
  -e TLS_CERT_FILE=/nonexistent \
  -e TLS_KEY_FILE=/nonexistent \
  -p 8080:8080 \
  magma-calculator
```

Setting `TLS_CERT_FILE` to a nonexistent path disables TLS — uvicorn will serve plain HTTP.

### 4. TLS certificate renewal

The host runs Certbot for Let's Encrypt certificates. The `/etc/letsencrypt/` directory is mounted into the container as `/certs:ro`, and uvicorn reads the cert files directly.

After renewal, the container must restart to pick up new certs. Install the post-renewal hook:

```bash
cp deploy/certbot-renew-hook.sh /etc/letsencrypt/renewal-hooks/post/restart-calculator.sh
chmod +x /etc/letsencrypt/renewal-hooks/post/restart-calculator.sh
```

### Running multiple instances

Start a second container with different limits for long-running computations:

```bash
docker run --rm \
  --cap-add SYS_ADMIN \
  --tmpfs /tmp:size=128m \
  -v /opt/magma:/opt/magma:ro \
  --env-file calculator-long.env \
  -p 8081:8080 \
  magma-calculator
```

## Security

Each Magma process runs inside an nsjail sandbox with:

- **PID, mount, network, and UTS namespace isolation** — the process cannot see or interact with the host
- **No network access** — `clone_newnet` creates an empty network namespace
- **Read-only mounts** — Magma installation and system libraries are bind-mounted read-only
- **cgroup limits** — memory and CPU enforced at the kernel level
- **Magma `-w` flag** — restricted mode that disables `System()`, `Pipe()`, `Open()`, and other dangerous intrinsics at the Magma level (no keyword filtering)
- **Privilege drop** — nsjail runs as root to create namespaces, then drops to the `calculator` user for Magma execution

## Development

```bash
poetry install           # install dependencies
poetry run pytest -v     # run tests
```
