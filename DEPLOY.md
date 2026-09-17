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
cp calculator.env.example calculator.env
```

Edit `calculator.env` if you want to change any defaults (timeouts, memory limits, rate limits, CORS). The defaults are fine for most setups.

## 7. Choose a version

```bash
cp .env.example .env
```

Edit `.env`. Set your domain, and set `CALCULATOR_VERSION` to the image tag this server will run:

```
DOMAIN=calc.magma-maths.org
CALCULATOR_VERSION=v0.1.0
```

Every commit on `main` is published as `sha-<short>`, the first 7 hex characters of the commit; those tags are immutable. A release is a `v*` tag such as `v0.1.0`, the same image as its commit's `sha-<short>` under a second name. The available tags are listed on the [package page](https://github.com/orgs/Magma-Maths/packages/container/package/calculator).

There is deliberately no default. With a moving tag, a routine `docker compose pull` or even a restart could change the running code without anyone choosing it, and during an incident nobody could answer "what version is running" from the box. With the pin, `.env` is that answer. If you skip this step, compose refuses to start and names the variable:

```
error while interpolating services.calculator.image: required variable CALCULATOR_VERSION is missing a value: set CALCULATOR_VERSION in .env, e.g. sha-abc1234 or v1.2.0
```

## 8. Pull and start

```bash
docker compose pull
docker compose up -d
```

This downloads the image (the package is public, so no registry login is needed) and starts the calculator. To build the image on the server instead, for example to try a local change, add the dev override. The version variable is still required, because compose checks it in the base file before applying the override, but any local name will do since nothing is pulled; this compiles nsjail and takes a few minutes:

```bash
CALCULATOR_VERSION=dev docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

## 9. Verify

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

### Update to a new version

Pick the tag to move to, a release `vX.Y.Z` or the `sha-<short>` of the `main` commit you want, and write it into `.env`:

```
CALCULATOR_VERSION=v0.2.0
```

Then:

```bash
git pull
docker compose pull
docker compose up -d
```

`git pull` brings in compose and env changes, `docker compose pull` fetches the pinned image, and `up -d` recreates the container if either changed. Nothing else moves the server: with the version pinned, a `pull` or a restart on its own never changes the running code, and `.env` is the record of what the box runs. Keep it under whatever change tracking you use for the host.

### Roll back

A rollback is the previous tag written into `.env` again, followed by the same two compose commands. Images already on the server are not downloaded twice, so it takes seconds. In-flight computations are killed by the restart.

### TLS certificates

Traefik handles Let's Encrypt certificate issuance and renewal automatically. No cron jobs or manual renewal needed. Certificates are stored in the `acme` Docker volume.

To check certificate status, visit the Traefik dashboard (only accessible from the server itself):

```bash
curl -s http://127.0.0.1:8080/api/http/routers | python3 -m json.tool
```
