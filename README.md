# Magma Calculator Service

REST API backend for the [Magma](http://magma.maths.usyd.edu.au/) online calculator. Executes Magma code inside an [nsjail](https://github.com/google/nsjail) sandbox and returns structured JSON results.

Designed to be called from a static frontend (frozen-flask on GitHub Pages) via JavaScript.

## Quick start

### Prerequisites

- Docker with `--cap-add SYS_ADMIN` support
- Magma installation directory (mounted read-only at runtime)

### Run with docker-compose

```bash
cp calculator.env.example calculator.env
# Edit calculator.env as needed

docker compose up -d
```

This expects:
- Magma installed at `/opt/magma` on the host
- TLS certificates at `/etc/letsencrypt/` (via Let's Encrypt)

### Run without TLS (for testing)

```bash
docker build -t magma-calculator .

docker run --rm \
  --cap-add SYS_ADMIN \
  --tmpfs /tmp:size=128m \
  -v /opt/magma:/opt/magma:ro \
  -e TLS_CERT_FILE=/nonexistent \
  -e TLS_KEY_FILE=/nonexistent \
  -p 8080:8080 \
  magma-calculator
```

## API

### POST /execute

```bash
curl -X POST http://localhost:8080/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "print 1+1;"}'
```

Response:
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

HTTP error codes:
- **413** -- input too large
- **422** -- missing `code` field
- **429** -- rate limit exceeded (includes `Retry-After` header)
- **503** -- all execution slots busy

### GET /health

```json
{"status": "ok"}
```

## Configuration

All settings are read from environment variables with sensible defaults. See `calculator.env.example` for the full list.

| Variable | Default | Description |
|----------|---------|-------------|
| `MAGMA_TIMEOUT` | 120 | Wall-clock timeout in seconds |
| `MAGMA_CPU_TIMEOUT` | 120 | CPU time limit in seconds |
| `MAGMA_MEMORY_MB` | 400 | Memory limit in MB |
| `MAGMA_INPUT_KB` | 50 | Max input size in KB |
| `MAGMA_OUTPUT_KB` | 20 | Max output size in KB |
| `MAX_CONCURRENT` | 4 | Max simultaneous executions |
| `PORT` | 8080 | Listen port |
| `TLS_CERT_FILE` | `/certs/live/calc.magma-maths.org/fullchain.pem` | TLS certificate path |
| `TLS_KEY_FILE` | `/certs/live/calc.magma-maths.org/privkey.pem` | TLS private key path |
| `RATE_LIMIT_PER_MINUTE` | 30 | Requests per IP per minute |
| `RATE_LIMIT_PER_HOUR` | 200 | Requests per IP per hour |
| `ALLOWED_ORIGIN` | `https://magma-maths.org,http://localhost` | Comma-separated CORS origins |

To run a "long" calculator, start a second container with different limits:
```bash
docker run --env-file calculator-long.env -p 8081:8080 magma-calculator
```

## Architecture

```
GitHub Pages (frozen-flask)
        |
        | HTTPS (CORS)
        v
Docker Container
  FastAPI (uvicorn + TLS)
        |
        | per request (stdin pipe)
        v
  nsjail -> magma -w -n
```

Each request:
1. Validates input size and rate limit
2. Acquires a concurrency slot (or returns 503)
3. Wraps code with `Alarm(TIMEOUT-1)`, `SetIgnorePrompt(true)`, and `quit;`
4. Pipes wrapped code via stdin to nsjail, which spawns Magma in an isolated sandbox
5. Parses stdout (banner/body/footer) and stderr (timeout/crash signals)
6. Returns structured JSON

### Security

- **nsjail sandbox**: each Magma process runs in its own PID, mount, network, and UTS namespaces with cgroup memory and CPU limits
- **Magma `-w` flag**: restricted mode that disables `System()`, `Pipe()`, `Open()`, and other dangerous intrinsics
- **No network access**: `clone_newnet` isolates the sandbox from the network
- **Read-only mounts**: Magma installation and system libraries are mounted read-only
- **No keyword filtering**: relies on Magma's built-in `-w` enforcement rather than fragile keyword blocklists

## Development

### Setup

```bash
# Install Poetry if needed: https://python-poetry.org/docs/#installation
poetry install
```

### Run tests

```bash
poetry run pytest -v
```

### Project structure

```
app/
  config.py       # Settings from env vars (pydantic-settings)
  parser.py       # Magma stdout/stderr parsing
  executor.py     # nsjail subprocess management
  ratelimit.py    # In-memory IP rate limiting
  main.py         # FastAPI app, endpoints, CORS middleware
tests/
  test_config.py
  test_parser.py
  test_executor.py
  test_main.py
deploy/
  certbot-renew-hook.sh   # Restarts container after cert renewal
nsjail.cfg                # nsjail sandbox configuration
Dockerfile                # Multi-stage build (Python deps + nsjail + runtime)
docker-compose.yml
calculator.env.example
```

## TLS and certificate renewal

The host machine runs Certbot to obtain Let's Encrypt certificates. The entire `/etc/letsencrypt/` directory is mounted into the container as `/certs:ro`. Uvicorn reads the cert and key files directly.

After renewal, a post-hook restarts the container to pick up new certs. Install the hook:

```bash
cp deploy/certbot-renew-hook.sh /etc/letsencrypt/renewal-hooks/post/restart-calculator.sh
chmod +x /etc/letsencrypt/renewal-hooks/post/restart-calculator.sh
```
