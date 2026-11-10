# Deploying JobIndex

Two supported setups. Both run the same code, migrations and CLI.

| | [A. Hosted demo (free)](#a-hosted-demo-supabase--vercel--github-actions) | [B. One VPS](#b-alternative-one-vps-docker-compose) |
|---|---|---|
| Database | Supabase Postgres, free tier (500 MB) | Postgres 16 container |
| API + search page | Vercel (Python function) | uvicorn behind Caddy (HTTPS) |
| Crawler | GitHub Actions, every 6 h | `worker` container, continuous scheduler |
| Size | ~90 small demo boards, capped ([size budget](OPERATIONS.md#size-budget)) | ~1,000+ boards |
| Cost | €0/month ([limits](OPERATIONS.md#hosted-demo-0month)) | ≈ €10–35/month |

Local development always uses `docker compose` (README, *Quick start*).

---

## A. Hosted demo: Supabase + Vercel + GitHub Actions

Running instance of exactly this setup: <https://job-board-indexer.vercel.app>
(walkthrough: [DEMO.md](DEMO.md)).

```
GitHub Actions ──crawl.yml (every 6 h)──> job boards
      │  migrate.yml (push to main)
      ▼
Supabase Postgres ◄── session pooler :5432 (crawler, migrations; role postgres)
      ▲
      └────────────── transaction pooler :6543 (role jobindex_web, read-only) ◄── Vercel: API, /, /docs
```

**Why the pooler everywhere:** Supabase free *direct* connections (`db.<ref>.supabase.co`)
are IPv6-only and GitHub-hosted runners have no IPv6, so every connection goes through the
Supabase pooler (Supavisor), which has IPv4:

* **Session pooler, port 5432** — migrations and the crawler (long sessions).
* **Transaction pooler, port 6543** — Vercel (many short-lived function instances).

JobIndex turns server-side prepared statements off automatically for pooler URLs
(`*.pooler.supabase.com`, port 6543; psycopg `prepare_threshold=None` — the asyncpg
equivalent would be `statement_cache_size=0`), see `jobindex/db.py`. No URL flags needed.

You need: a GitHub account (the repository must be **public** for free Actions minutes), a
Supabase account and a Vercel account (both can sign in with GitHub). Dashboards change
from time to time; if a label below differs, look for the same setting nearby.

### A1. Supabase: create the database

1. Go to <https://supabase.com/dashboard> → **New project**.
2. **Organization**: your personal org (Free plan). **Project name**: `jobindex`.
3. **Database password**: click **Generate a password**, then **copy it into your password
   manager now** (you need it for the connection string; it is not shown again).
4. **Region**: pick one close to where Vercel will run the API, e.g. *Central EU
   (Frankfurt)* `eu-central-1` ↔ Vercel `fra1`, or *East US (N. Virginia)* `us-east-1` ↔
   Vercel `iad1`. Note it; you set the Vercel region to match in A3.
5. **Create new project** and wait until it shows as healthy (1–2 minutes).
6. **Turn off the public Data API** (JobIndex does not use it, and tables in `public`
   would otherwise be reachable with the project's API keys): **Project Settings → Data
   API** → disable **Enable Data API** (or, if your dashboard only offers it, remove
   `public` from **Exposed schemas**) → **Save**.
7. **Create the read-only role for the website**: **SQL Editor → New query**, paste,
   replace `CHANGE-ME` with a new long random password (e.g. `openssl rand -hex 24`;
   keep it in your password manager), **Run**:

   ```sql
   -- Read-only login used by Vercel. The crawler and migrations use `postgres`.
   create role jobindex_web with login password 'CHANGE-ME';
   grant usage on schema public to jobindex_web;
   grant select on all tables in schema public to jobindex_web;
   alter default privileges for role postgres in schema public
     grant select on tables to jobindex_web;

   -- Supabase's API roles get nothing on JobIndex tables (defence in depth).
   revoke all on all tables in schema public from anon, authenticated;
   alter default privileges for role postgres in schema public
     revoke all on tables from anon, authenticated;
   alter default privileges for role postgres in schema public
     revoke all on sequences from anon, authenticated;
   ```

   Running it before the first migration is fine: the default privileges apply to the
   tables the migrations create later.
8. **Copy the two connection strings**: click **Connect** (top of the project page) →
   **Connection String** → type **URI**:
   * **Session pooler** → copy. It looks like
     `postgresql://postgres.<ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`.
     Replace `[YOUR-PASSWORD]` with the database password from step 3. This is
     **`DATABASE_URL` for GitHub** (A2).
   * **Transaction pooler** → copy (same host, port **6543**). Replace the user
     `postgres.<ref>` with **`jobindex_web.<ref>`** and the password with the
     `jobindex_web` password from step 7. This is **`DATABASE_URL` for Vercel** (A3).

   If a password contains characters like `@ : / ? #`, URL-encode them (`@` → `%40`), or
   generate one with only letters and digits.

Do **not** use the "Direct connection" string: it is IPv6-only on the free tier.

### A2. GitHub: secrets, migrations, first crawl

1. Push the repository to GitHub and make it **public** (Settings → General → Danger
   Zone → Change visibility). Run the checks in [A4. Before going public](#a4-before-going-public)
   first.
2. **Settings → Secrets and variables → Actions → Secrets → New repository secret**:
   * Name `DATABASE_URL`, value: the **session pooler** URL from A1 step 8. **Add secret**.
3. Optional, same page, tab **Variables → New repository variable** (defaults in
   `.github/workflows/crawl.yml`):
   * `HTTP_USER_AGENT` — e.g. `JobIndexBot/0.1 (+https://github.com/<you>/<repo>)`
     (default: exactly that, built from the repository name). Must identify you honestly.
   * `MAX_JOBS_PER_SOURCE` (200), `DB_WARN_MB` (400), `DB_FAIL_MB` (450), and the
     retention settings, see [OPERATIONS.md](OPERATIONS.md#size-budget).
4. **Settings → Actions → General**: *Actions permissions* = **Allow all actions and
   reusable workflows**; *Workflow permissions* = **Read repository contents** → **Save**.
5. **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable
   them**.
6. **Actions → migrate → Run workflow → Run workflow** (branch `main`). It runs
   `alembic upgrade head` through the session pooler. Later it runs by itself on every push
   to `main` that touches `migrations/` or the models.
7. **Actions → crawl → Run workflow**, tick **First register the boards of
   coverage/demo_sources.yaml**, **Run workflow**. The first run registers ~90 boards and
   crawls them (expect 30–90 min; Jobiqo boards are slow on purpose: `Crawl-delay: 10`).
   The run page shows a **summary** with counts per platform and the database size.
8. From now on `crawl` runs every 6 hours (cron `17 */6 * * *`, UTC). The two runs never
   overlap (concurrency group `crawl`).
9. **CI**: `ci` runs `make lint` and `make test` on every push and pull request; no
   secrets needed.

### A3. Vercel: API, search page and /docs

1. Go to <https://vercel.com/new> → **Import Git Repository** → **Continue with GitHub**
   (install the Vercel app for this repository if asked) → **Import** next to the repo.
2. **Configure Project**:
   * **Project Name**: e.g. `jobindex`.
   * **Framework Preset**: **FastAPI** (Vercel pre-selects it).
   * **Root Directory**: `./`. Leave **Build and Output Settings** empty.
   * **Environment Variables**: Key `DATABASE_URL`, value: the **transaction pooler** URL
     with the `jobindex_web` role from A1 step 8 → **Add**.
3. **Deploy**. Vercel installs `requirements.txt` and serves `api/index.py`;
   `pyproject.toml` (`[tool.vercel] entrypoint = "api.index:app"`) points the preset at it.
4. **Settings → Functions → Function Region**: choose the region matching Supabase (e.g.
   `Frankfurt, Germany (fra1)`) → **Save**, then **Deployments → ⋯ → Redeploy**. Every
   query crosses this distance, so it matters.
5. Check (replace the host with yours):
   * `https://jobindex.vercel.app/health` → `{"status":"ok","database":"ok"}`
   * `https://jobindex.vercel.app/` → search page; `/docs` → API docs; `/stats`.

On Vercel (`VERCEL=1`) the app automatically uses no connection pool of its own (the
transaction pooler pools), runs every transaction `READ ONLY`, and lets the CDN cache API
responses for 60 s (`API_CACHE_SECONDS`; `/health` is never cached). It never crawls or
schedules anything; the API has no write endpoints. Optional variables: `LOG_LEVEL`,
`API_CACHE_SECONDS`.

Vercel redeploys on every push to `main`. A push with a new migration also starts
`migrate`; for a minute the new API code may meet the old schema, so keep migrations
backwards-compatible (add columns before using them).

### A4. Before going public

Checked on 2026-11-08 (`git log --all -p` and the working tree) for tokens, keys,
passwords, `.env` files, personal paths and e-mail addresses:

* Ignored, never committed: `.env`, `.env.*` (except the two examples), `.vercel/`,
  database dumps, `backups/`, research scratch output (`.research/*` except the
  request-level evidence cited by `coverage/blocked_by_site.yaml`).
* The only credentials in the repository are the local-development defaults
  (`jobindex`/`jobindex`, docker-compose only) and placeholders (`change-me`, `CHANGE-ME`).
* Commits carry the author's name and e-mail address (git metadata). Test fixtures are
  saved public job postings and contain the employers' published contact addresses.

Re-check before publishing new history, e.g.
`git log --all -p | grep -nEi 'password=|secret|token|api[_-]?key|/Users/|/home/'`.

### A5. Day-2

* **Status**: Actions → crawl → latest run → summary; `GET /stats` and `/sources?failing=true`.
* **Failures**: GitHub e-mails you when a workflow fails. `crawl` fails when the database
  exceeds `DB_FAIL_MB` or a step crashes; sources that fail to fetch only produce a warning
  (they are recorded in `fetch_runs` and never close jobs).
* **Run something by hand** against the hosted database from your machine:
  `DATABASE_URL='<session pooler URL>' docker compose run --rm -e DATABASE_URL app python -m jobindex.cli db-size`.
* **Backups**: the free tier has no downloadable backups. For a copy:
  `pg_dump --no-owner -Fc '<session pooler URL>' > jobindex.dump` (needs `pg_dump` 16+).

More in [OPERATIONS.md](OPERATIONS.md).

---

## B. Alternative: one VPS (Docker Compose)

The whole stack runs on **one small Linux VPS** with Docker Compose. Everything is in
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

### 1. Server

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

### 2. Install

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

### 3. Add sources

```sh
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm app \
    python -m jobindex.cli add-source https://jobs.lever.co/spotify
```

The worker picks new sources up on its next tick (within a minute). More in
[OPERATIONS.md](OPERATIONS.md).

### Updating

```sh
cd ~/jobindex && git pull
make prod-up           # rebuilds the image; `migrate` applies new migrations first
```

The worker gets 60 s to stop (`stop_grace_period`); crawls still running after 30 s are
cancelled and recorded as failed runs. **Jobs are never closed by an interrupted crawl**;
the source is simply crawled again on the next tick.

### Backups

* The `backup` service writes `backups/jobindex-YYYYMMDD-HHMMSS.dump` every day at
  `BACKUP_HOUR` (UTC) and deletes dumps older than `BACKUP_KEEP_DAYS`.
* Make one now: `make prod-backup`.
* **Copy backups off the server.** A dump on the same disk does not survive losing the
  VPS. Simplest: the VPS provider's snapshot/backup feature (≈20% of the server price), or
  a nightly `rsync`/`rclone` of `backups/` to another machine or storage box. Off-site
  storage is an external service: agree on it with the project owner (see CLAUDE.md).

#### Restore a backup

```sh
P="docker compose -f docker-compose.prod.yml --env-file .env.prod"
$P stop app worker
$P exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
$P exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < backups/jobindex-20261107-030000.dump
make prod-up           # migrate brings an older dump up to the current schema
```

Test a restore now and then (e.g. into a local `make up` database).

### Health and monitoring

* `docker compose -f docker-compose.prod.yml --env-file .env.prod ps` shows every
  service's health.
* `GET /health` → `200 {"status":"ok","database":"ok"}` or `503`. Point an external uptime
  checker at `https://$DOMAIN/health` (any free service; optional).
* `GET /stats` → per-platform job counts, last successful crawl, failing sources, recent
  errors. See [OPERATIONS.md](OPERATIONS.md#monitoring-failures).
* Logs: `make prod-logs` (app + worker), or `docker compose ... logs caddy`.

### Local smoke test of the production stack

Set `DOMAIN=localhost` in `.env.prod`; Caddy then serves `https://localhost` with its own
local CA (`curl -k`). Use a separate project name so it does not touch the dev database:
`docker compose -p jobindex-prod -f docker-compose.prod.yml --env-file .env.prod up -d --build --wait`,
and `... down -v` afterwards. Ports 80 and 443 must be free. Check with
`curl -k https://localhost/health`. The `make prod-*` targets do not pass `-p`, so call
compose directly here, e.g. `... exec backup /scripts/backup.sh now` for a backup.
(Verified from a fresh clone on 2026-11-10: all six services healthy in ~45 s.)
