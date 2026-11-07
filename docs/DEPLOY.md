# Deploying JobIndex

JobIndex runs on **one small Linux VPS** with Docker Compose. Everything is in
[`docker-compose.prod.yml`](../docker-compose.prod.yml):

| Service | Image | What it does | Healthcheck |
|---|---|---|---|
| `db` | `postgres:16` | The index (jobs, sources, crawl history). Not exposed. | `pg_isready` |
| `migrate` | built from `Dockerfile` (target `prod`) | One-shot `alembic upgrade head` before `app`/`worker` start | exits 0 |
| `app` | same image | API (`/jobs`, `/stats`, `/docs`) and search page (`/`), 2 uvicorn workers | `GET /health` (checks the DB) |
| `worker` | same image | `python -m jobindex.cli schedule`: crawls every active source on its interval, then dedup | heartbeat file younger than 5 min |
| `caddy` | `caddy:2` | HTTPS on ports 80/443, automatic Let's Encrypt certificate, reverse proxy to `app` | admin endpoint |
| `backup` | `postgres:16` | Nightly `pg_dump` into `./backups`, keeps 14 days | newest dump younger than 26 h |

Only Caddy is reachable from the internet. Container logs rotate (5 × 20 MB per service).

## 1. Server

* Ubuntu 24.04 / Debian 12, **2 vCPU, 4 GB RAM, 80 GB SSD** is enough for ~1,000 boards
  (see [OPERATIONS.md](OPERATIONS.md#capacity-and-monthly-cost) for sizing).
  `docker-compose.prod.yml` tunes Postgres for 4 GB RAM; scale `shared_buffers` (25% of
  RAM) and `effective_cache_size` (75%) if you pick a bigger machine.
* A DNS `A` (and `AAAA`) record for your hostname pointing at the server.
* Firewall: allow 22, 80, 443 (TCP) and 443/UDP (HTTP/3). Nothing else.

```sh
# as root, once
apt-get update && apt-get install -y ca-certificates curl git make ufw
curl -fsSL https://get.docker.com | sh            # Docker Engine + Compose plugin
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443 && ufw --force enable
adduser --disabled-password jobindex && usermod -aG docker jobindex
```

> Docker publishes ports past `ufw`; that is fine here because only Caddy publishes any.

## 2. Install

```sh
su - jobindex
git clone <repository-url> jobindex && cd jobindex
cp .env.prod.example .env.prod && chmod 600 .env.prod
$EDITOR .env.prod      # DOMAIN, ACME_EMAIL, POSTGRES_PASSWORD (openssl rand -hex 24), HTTP_USER_AGENT
make prod-up           # build, migrate, start everything, wait until healthy
```

`make prod-up` is `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
--build --wait`. Open `https://$DOMAIN/` (search page) and `https://$DOMAIN/docs` (API).

Set `HTTP_USER_AGENT` to something that identifies you and links to a page explaining the
bot and how to contact you; site owners use it to reach you instead of blocking you.

## 3. Add sources

```sh
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm app \
    python -m jobindex.cli add-source https://jobs.lever.co/spotify
```

The worker picks new sources up on its next tick (within a minute). More in
[OPERATIONS.md](OPERATIONS.md).

## Updating

```sh
cd ~/jobindex && git pull
make prod-up           # rebuilds the image; `migrate` applies new migrations first
```

The worker gets 60 s to stop (`stop_grace_period`); crawls still running after 30 s are
cancelled and recorded as failed runs. **Jobs are never closed by an interrupted crawl**;
the source is simply crawled again on the next tick.

## Backups

* The `backup` service writes `backups/jobindex-YYYYMMDD-HHMMSS.dump` every day at
  `BACKUP_HOUR` (UTC) and deletes dumps older than `BACKUP_KEEP_DAYS`.
* Make one now: `make prod-backup`.
* **Copy backups off the server.** A dump on the same disk does not survive losing the
  VPS. Simplest: the VPS provider's snapshot/backup feature (≈20% of the server price), or
  a nightly `rsync`/`rclone` of `backups/` to another machine or storage box. Off-site
  storage is an external service: agree on it with the project owner (see CLAUDE.md).

### Restore a backup

```sh
P="docker compose -f docker-compose.prod.yml --env-file .env.prod"
$P stop app worker
$P exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
$P exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < backups/jobindex-20261107-030000.dump
make prod-up           # migrate brings an older dump up to the current schema
```

Test a restore now and then (e.g. into a local `make up` database).

## Health and monitoring

* `docker compose -f docker-compose.prod.yml --env-file .env.prod ps` shows every
  service's health.
* `GET /health` → `200 {"status":"ok","database":"ok"}` or `503`. Point an external uptime
  checker at `https://$DOMAIN/health` (any free service; optional).
* `GET /stats` → per-platform job counts, last successful crawl, failing sources, recent
  errors. See [OPERATIONS.md](OPERATIONS.md#monitoring-failures).
* Logs: `make prod-logs` (app + worker), or `docker compose ... logs caddy`.

## Local smoke test of the production stack

Set `DOMAIN=localhost` in `.env.prod`; Caddy then serves `https://localhost` with its own
local CA (`curl -k`). Use a separate project name so it does not touch the dev database:
`docker compose -p jobindex-prod -f docker-compose.prod.yml --env-file .env.prod up -d --build --wait`,
and `... down -v` afterwards.
