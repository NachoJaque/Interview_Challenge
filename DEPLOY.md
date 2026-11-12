# Deploying to Oracle Cloud

One virtual machine runs the same Docker Compose stack as local development, with Caddy in front for HTTPS.

```
Internet ──443──▶ Caddy (automatic HTTPS)
                    ▼
                  nginx (web app, login rate limit)
                    ▼ /api
                  FastAPI ──▶ PostgreSQL
```

Only Caddy is reachable from outside (ports 80 and 443). nginx, the API and the database stay on Docker's internal network.

## 1. Create the instance

In the Oracle Cloud console: **Compute → Instances → Create instance**.

| Setting | Value |
|---------|-------|
| Image | Ubuntu 24.04 |
| Shape | Ampere `VM.Standard.A1.Flex`, 1 OCPU, 6 GB memory |
| Networking | A public subnet, with a public IPv4 address |
| SSH keys | Upload your public key |

If the console answers "out of capacity", retry later or pick another availability domain.

## 2. Open the ports

**Networking → Virtual cloud networks → your VCN → Security Lists → Default** and add ingress rules:

| Source | Protocol | Port | Purpose |
|--------|----------|------|---------|
| `0.0.0.0/0` | TCP | 80 | Certificate issuance and redirect to HTTPS |
| `0.0.0.0/0` | TCP | 443 | The site |
| Your own IP `/32` | TCP | 22 | SSH (replace the default rule that allows everyone) |

## 3. Point a host name at the instance

Caddy needs a host name to obtain a certificate; an IP address alone will not work.

- With your own domain: create an `A` record pointing to the instance's public IP.
- Without one: create a free subdomain at https://www.duckdns.org and set it to the public IP.

Check it resolves before continuing:

```bash
nslookup tickets.example.com
```

## 4. Install Docker on the instance

```bash
ssh ubuntu@<public-ip>
```

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker ubuntu && exit
```

Log in again so the group change takes effect.

## 5. Get the code and configure it

```bash
git clone https://github.com/<you>/<repo>.git tickets && cd tickets
```

```bash
cp .env.example .env && nano .env
```

Set these values in `.env`:

| Variable | Value |
|----------|-------|
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.prod.yml` |
| `DOMAIN` | The host name from step 3 |
| `ENVIRONMENT` | `production` |
| `JWT_SECRET` | Output of `openssl rand -base64 48` |
| `POSTGRES_PASSWORD` | Output of `openssl rand -hex 24` |
| `ADMIN_PASSWORD`, `DEMO_PASSWORD` | Passwords of your choice for the two seeded accounts. Never keep the values from `.env.example`: they are public |
| `SEED_DEMO_DATA` | `true` for a demo site (demo customer and demo events). Leave it out for a real deployment, which then starts with only the admin account |

`COMPOSE_FILE` makes every `docker compose` command below use the production overlay.

The API refuses to start in production with the default `JWT_SECRET`.

## 6. Start

```bash
docker compose up -d --build
```

The first start builds the images, applies the migrations, creates the admin account (plus the demo data if `SEED_DEMO_DATA=true`) and obtains the certificate. After a minute or two the site is live at `https://<DOMAIN>`.

```bash
docker compose ps
```

```bash
docker compose logs -f caddy
```

## 7. Verify

- `https://<DOMAIN>` shows the events and the padlock.
- `http://<DOMAIN>` redirects to HTTPS.
- `https://<DOMAIN>/api/docs` shows the interactive API docs.
- Logging in with the demo account works.

## Operations

Update to the latest code:

```bash
git pull && docker compose up -d --build
```

Reset the demo data (deletes every account and purchase, then seeds again; needs `SEED_DEMO_DATA=true` in `.env`):

```bash
docker compose down -v && docker compose up -d --build
```

Event dates are relative to the moment the data is seeded. Reset on the day of a demo so each refund rule falls on the intended event.

Logs:

```bash
docker compose logs -f api
```

The audit trail (one JSON line per reservation, confirmation, refund, login…):

```bash
docker compose logs api | grep " audit "
```

## Troubleshooting

**The site does not load.** Check the security list (step 2) and that the host name resolves to the instance. Oracle's Ubuntu images also ship a host firewall; Docker's published ports normally pass through it, but if they do not:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp -m multiport --dports 80,443 -j ACCEPT && sudo netfilter-persistent save
```

**Caddy cannot obtain a certificate.** Port 80 must be reachable from the internet and `DOMAIN` must resolve to this instance. Details are in `docker compose logs caddy`.

**The API container keeps restarting.** `docker compose logs api` shows why; the usual cause is a missing or default `JWT_SECRET`.
