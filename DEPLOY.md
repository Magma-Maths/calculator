# Deploy on Ubuntu

Step-by-step guide for deploying the Magma Calculator on a fresh Ubuntu server (22.04 or 24.04).

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

```bash
git clone https://github.com/Magma-Maths/calculator.git
cd calculator
```

## 5. Start Traefik

Traefik is the shared reverse proxy that handles HTTPS. You only set it up once per server — it can serve multiple apps.

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

Edit `calculator.env` if you want to change any defaults (timeouts, memory limits, rate limits, CORS). The defaults are fine for most setups.

## 7. Build and start

```bash
docker compose up -d --build
```

This builds the image (compiles nsjail, installs Python dependencies) and starts the calculator. The first build takes a few minutes.

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

### Rebuild after code update

```bash
git pull
docker compose up -d --build
```

### TLS certificates

Traefik handles Let's Encrypt certificate issuance and renewal automatically. No cron jobs or manual renewal needed. Certificates are stored in the `acme` Docker volume.

To check certificate status, visit the Traefik dashboard (only accessible from the server itself):

```bash
curl -s http://127.0.0.1:8080/api/http/routers | python3 -m json.tool
```
