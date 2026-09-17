# Deploy on Ubuntu

Step-by-step guide for deploying the Magma Calculator on a fresh Ubuntu server (22.04 or 24.04).

The calculator runs from a prebuilt image, `ghcr.io/magma-maths/calculator`, that GitHub Actions publishes on every push to `main`. The server only pulls it: nothing is compiled there, and the image contains no Magma. Your licensed copy stays on the host and is bind-mounted into the container.

## Prerequisites

- A server with a public IP
- A domain (e.g. `calc.magma-maths.org`) with an A record pointing to that IP
- Magma binaries (the server needs a licensed copy)

## 1. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Add your user to the `docker` group so you don't need `sudo` for every command:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

## 2. Open firewall ports

If `ufw` is enabled:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

## 3. Install Magma

Copy or install Magma to `/opt/magma` on the host. The directory should contain the `magma` binary at `/opt/magma/magma`. Verify:

```bash
/opt/magma/magma -version
```

## 4. Clone the repository

The clone provides the compose files, the env templates and the Traefik stack; the calculator itself comes from the published image, so the server needs no build tooling.

```bash
git clone https://github.com/Magma-Maths/calculator.git
cd calculator
```

## 5. Start Traefik

Traefik is the shared reverse proxy that handles HTTPS. You only set it up once per server; it can serve multiple apps.

```bash
cd traefik
cp .env.example .env
```

Edit `traefik/.env` and set your email for Let's Encrypt notifications:

```
ACME_EMAIL=you@example.com
```

Start Traefik:

```bash
docker compose up -d
cd ..
```

Verify it's running:

```bash
docker ps --filter name=traefik
curl -s http://127.0.0.1:8080/api/overview | head -c 200
```

## 6. Configure the calculator

```bash
cp .env.example .env
cp calculator.env.example calculator.env
```

Edit `.env` and set your domain:

```
DOMAIN=calc.magma-maths.org
```

`CALCULATOR_VERSION` in the same file selects the image tag. Every commit on `main` is published as `sha-<short>` (the first 7 hex characters of the commit) and as the moving `main`; a release tag such as `v1.2.3` names the same image as its commit's `sha-<short>`. Leave it empty to run the latest `main` build, or pin an exact tag so the server keeps running the same bytes until you change it:

```
CALCULATOR_VERSION=v1.2.3
```

Edit `calculator.env` if you want to change any defaults (timeouts, memory limits, rate limits, CORS). The defaults are fine for most setups.

## 7. Pull and start

```bash
docker compose pull
docker compose up -d
```

This downloads the image (the package is public, so no registry login is needed) and starts the calculator. To build the image on the server instead, for example to try a local change, add the dev override; this compiles nsjail and takes a few minutes:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

## 8. Verify

Check that the container is running:

```bash
docker ps --filter name=magma-calculator
```

Test the health endpoint over HTTPS (replace with your domain):

```bash
curl -i https://calc.magma-maths.org/health
```

Expected response:

```
HTTP/2 200
content-type: application/json
...

{"status":"ok"}
```

Test a computation:

```bash
curl -s https://calc.magma-maths.org/execute \
  -H 'Content-Type: application/json' \
  -d '{"code": "print 1+1;"}' | python3 -m json.tool
```

## Maintenance

### View logs

```bash
docker compose logs -f calculator    # calculator logs
docker compose -f traefik/docker-compose.yml logs -f   # traefik logs
```

### Restart after config change

```bash
docker compose restart
```

### Update to the latest main

```bash
git pull
docker compose pull
docker compose up -d
```

`git pull` brings in compose and env changes, `docker compose pull` fetches the image that `CALCULATOR_VERSION` selects, and `up -d` recreates the container if either changed. Without the `pull`, `up -d` keeps whatever image is already on the server.

### Pin or roll back a version

Set `CALCULATOR_VERSION` in `.env` to the tag you want, then run the same two commands:

```bash
docker compose pull
docker compose up -d
```

A rollback is the previous tag pinned again. Images already on the server are not downloaded twice, so it takes seconds. In-flight computations are killed by the restart.

### TLS certificates

Traefik handles Let's Encrypt certificate issuance and renewal automatically. No cron jobs or manual renewal needed. Certificates are stored in the `acme` Docker volume.

To check certificate status, visit the Traefik dashboard (only accessible from the server itself):

```bash
curl -s http://127.0.0.1:8080/api/http/routers | python3 -m json.tool
```
