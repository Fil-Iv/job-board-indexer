# Operating JobIndex

Day-to-day tasks for a running deployment ([DEPLOY.md](DEPLOY.md)). Commands are shown
for production; locally replace the prefix with `docker compose run --rm app`.

```sh
P="docker compose -f docker-compose.prod.yml --env-file .env.prod"
CLI="$P run --rm app python -m jobindex.cli"
```

## How the pieces fit

```
worker (scheduler) ──every tick──> due sources ──> crawler ──> connector ──> HttpClient ──> job boards
                                                      │                     (robots.txt, UA, 1 req/s/host, retries)
                                                      ▼
                                       PostgreSQL: sources, fetch_runs, jobs, job_changes
                                                      │
                                   dedup (after successful crawls) fills jobs.canonical_job_id
                                                      │
app (FastAPI) ── /jobs, /jobs/{id}, /sources, /stats, / (search page) ── Caddy (HTTPS)
```

* A **source** is one board on one platform (`sources.platform` + `board_identifier`).
* Each crawl is a **fetch run** (`fetch_runs`: `running` → `success` | `failed`).
* A **failed fetch never closes jobs**: the run is `failed`, `sources.consecutive_failures`
  goes up, the source's jobs stay as they were. Jobs are closed only by a *successful*
  crawl that no longer lists them.
* **Dedup** groups postings of the same opening on different sources; the API returns one
  result per opening with all its links ([CONNECTORS.md](CONNECTORS.md#deduplication)).

## Add a source

```sh
$CLI add-source https://jobs.lever.co/spotify
$CLI add-source https://www.figma.com/careers/            # custom domain: platform auto-detected
$CLI add-source https://boards.greenhouse.io/acme --company-name "Acme Inc."
```

`add-source` accepts board URLs, job URLs, embed/API URLs and custom-domain careers pages
(it looks for platform fingerprints in the page). It prints the new source id. If the
platform has no company name in its data, set one: `$CLI set-company <id> "Acme Inc."`.
The worker crawls the new source within a minute; to see the result immediately run
`$CLI crawl <id>`.

If `add-source` says *no supported platform found*, the platform has no connector yet
(see [COVERAGE.md](COVERAGE.md) and [CONNECTORS.md](CONNECTORS.md#adding-a-connector)).

**Deactivate** a board (stops crawling, keeps its jobs as they are):

```sh
$P exec db psql -U jobindex -c "UPDATE sources SET active = false WHERE id = 42"
```

## Re-run a board

```sh
$CLI crawl 42          # one source now (also runs dedup); exit code 1 if it failed
$CLI crawl --all       # every active source now
$CLI dedup             # recompute duplicates only
```

A manual crawl while the worker is crawling the same source is harmless; the scheduler
skips sources with a `running` fetch run. Workday and generic sitemap crawls are
**resumable**: a crawl that failed half-way continues from its checkpoint
(`sources.crawl_checkpoint`) next time.

## Monitoring failures

**`GET /stats`** (JSON) — the first place to look:

* `platforms[]`: per platform `sources`, `failing_sources`, `open_jobs`, `last_success_at`;
* `failing_sources`, `running_crawls`, `last_crawl_at`;
* `recent_failures[]`: the latest 20 failed runs with their error.

**`GET /sources?failing=true`** — every source whose last crawl failed, with
`consecutive_failures` and `last_error`.

Useful SQL (`$P exec db psql -U jobindex`):

```sql
-- Sources failing for more than a day
SELECT id, platform, board_identifier, consecutive_failures, last_success_at, last_error
FROM sources WHERE active AND consecutive_failures > 0
  AND coalesce(last_success_at, 'epoch') < now() - interval '1 day'
ORDER BY consecutive_failures DESC;

-- Error types in the last 24 h
SELECT split_part(error, ':', 1) AS kind, count(*)
FROM fetch_runs WHERE status = 'failed' AND started_at > now() - interval '1 day'
GROUP BY 1 ORDER BY 2 DESC;

-- Crawl history of one source
SELECT id, started_at, status, jobs_found, jobs_new, jobs_closed, left(error, 120)
FROM fetch_runs WHERE source_id = 42 ORDER BY id DESC LIMIT 20;
```

What the common errors mean:

| Error | Meaning | Action |
|---|---|---|
| `FetchError: HTTP 404` | Board renamed or removed | Check the board URL; deactivate or re-add |
| `FetchError: HTTP 403/429` | Site refuses us or rate-limits | Raise the platform interval; record in `coverage/blocked_by_site.yaml` if permanent |
| `RobotsDisallowed…` | robots.txt forbids the path | Do not work around it; deactivate |
| `ConnectorError: …` | Response shape changed | Capture a new fixture (`scripts/capture_fixture.py`), fix the connector |
| `JobCountDropError` | Board suddenly lists far fewer jobs | Usually a broken sitemap/template upstream; jobs were *not* closed. If the drop is real, crawl again after it persists, or raise `GENERIC_MAX_JOB_DROP` temporarily |
| `CrawlCancelledError` | Worker was stopped mid-crawl (deploy/restart) | None; next tick retries |
| `abandoned: …` | Process died while crawling | None; recovered at worker start |

A failing source is retried with backoff: the interval is multiplied by 1, 2, 4 and then 8
for 1, 2, 3 and 4+ consecutive failures. One success resets it.

The worker container is `unhealthy` when its scheduler has not ticked for 5 minutes;
check `make prod-logs`.

## Scheduling and tuning

Settings (env vars in `.env.prod`, defaults in `jobindex/config.py`):

| Setting | Default | |
|---|---|---|
| `CRAWL_INTERVAL_MINUTES` | 60 | Minutes between crawls of a source |
| `CRAWL_PLATFORM_INTERVALS` | `{"workday":240,"oracle":240,"successfactors":240,"hireology":720}` | Per-platform overrides (JSON; replaces the default map) |
| `CRAWL_CONCURRENCY` | 4 | Crawls running at once |
| `CRAWL_JITTER` | 0.1 | Up to +10% random delay per source, spreads load |
| `CRAWL_STALE_AFTER_HOURS` | 12 | A `running` run older than this is considered dead |
| `RATE_LIMIT_PER_HOST_SECONDS` | 1.0 | Minimum gap between requests to one host (robots.txt `Crawl-delay` wins if larger) |
| `DEDUP_DATE_WINDOW_DAYS` | 30 | Same company + title + location further apart = new opening |
| `GENERIC_MAX_JOB_DROP` | 0.5 | Sitemap connectors fail instead of closing >50% of a board |

Restart the worker after changing them: `$P up -d worker`.

**Per-host limit is the real ceiling.** Boards of API platforms share one host
(`boards-api.greenhouse.io`, `api.lever.co`, …), and the client sends at most one request
per second per host: ≈ 3,600 requests per hour per platform. With hourly crawls and one
request per board, that is ~3,600 Greenhouse boards. Beyond that, raise that platform's
interval (e.g. `{"greenhouse": 120}`) rather than the rate. Increase `CRAWL_CONCURRENCY`
(8–16) once you have thousands of sources so slow detail-fetching crawls (Workday, sitemap
boards) do not hold up the queue; the CPU cost is small.

## Capacity and monthly cost

Estimates for planning, based on the response sizes and request patterns of the 31
connectors measured in `docs/COVERAGE.md` (Nov 2026). Prices are typical small-VPS list
prices; check the provider's current pricing.

**Requests per board per day** (hourly crawl unless the platform default is longer):

| Connector kind | Platforms | Per crawl | Per day |
|---|---|---|---|
| Single-request API | Greenhouse, Ashby, Workable, Recruitee, Personio, Teamtailor, Pinpoint, Gem, Breezy | 1 | 24 |
| Paginated API | Lever, Manatal, Trakstar, SmartRecruiters (HTML) | 1–5 | 24–120 |
| List + detail per new/changed job | Workday (4 h), Oracle (4 h), BambooHR, Rippling | list pages + ~2–5% of jobs | 10–60 |
| Sitemap + job page per new/changed job | 14 generic platforms (iCIMS, Phenom, SuccessFactors, job boards) | 1–3 sitemaps + changed pages | 30–100 (+ weekly refresh when no `<lastmod>`) |

Plus one `robots.txt` per host per hour (cached). Average ≈ **40 requests/board/day**.

| | ~1,000 boards (≈150k open jobs) | ~10,000 boards (≈1.5M open jobs) |
|---|---|---|
| Requests | ~40k/day (≈0.5/s) | ~400k/day (≈5/s, spread over many hosts) |
| Inbound traffic (gzip) | ~5 GB/day, ~150 GB/month | ~50 GB/day, ~1.5 TB/month |
| Database (jobs + history + indexes) | ~3–5 GB | ~30–50 GB |
| Backups (14 × compressed dump) | ~10 GB | ~100 GB |
| VPS | 2 vCPU / 4 GB / 80 GB | 4–8 vCPU / 16 GB / 240 GB |
| VPS price | ~€5–25/month | ~€20–100/month |

Inbound traffic is free or included at common VPS providers (e.g. Hetzner includes 20 TB;
DigitalOcean/Linode/Vultr do not bill inbound), so bandwidth is not a cost driver; API
users' outbound traffic is small (JSON pages of 20 results).

**Monthly total for ~78 platforms / ~1,000 boards: ≈ €10–35**: VPS €5–25, provider
backups/snapshots €1–5, domain ≈ €1. TLS certificates (Let's Encrypt) are free. No paid
APIs are used. At 10,000 boards: ≈ €30–120.

## External dependencies

Runtime services:

* **The crawled platforms' public endpoints** (see [CONNECTORS.md](CONNECTORS.md)) — no
  accounts, keys or contracts; their availability and terms decide what we can index.
  Undocumented endpoints (Workday CXS, BambooHR, Rippling, Breezy JSON, Pinpoint, Trakstar,
  Oracle CE) can change without notice.
* **Let's Encrypt** (ACME) via Caddy — certificate issuance/renewal; needs ports 80/443.
* **DNS** for `DOMAIN` (any registrar/DNS host).
* A **VPS** with Docker Engine and the Compose plugin (v2.24+).

Build-time:

* Docker Hub images: `python:3.12-slim`, `postgres:16`, `caddy:2`.
* PyPI packages (`pyproject.toml`): fastapi, uvicorn, sqlalchemy, alembic, psycopg,
  httpx, apscheduler, pydantic-settings, selectolax, pycountry, defusedxml, jinja2;
  dev: pytest, pytest-asyncio, ruff, mypy, pyyaml, type stubs.

Nothing else: no queue, cache, search engine or cloud service. Off-site backup storage is
the one recommended addition and needs the project owner's approval.

## Routine maintenance

* **Weekly**: glance at `/stats` (`failing_sources`, platforms whose `last_success_at` is
  old). Fix or deactivate sources failing for days.
* **Monthly**: `git pull && make prod-up`; check backups exist and are copied off-site;
  `docker system prune -f` to drop old images.
* **Disk**: `docker system df`, `du -sh backups`; Postgres autovacuum handles the tables.
* **Parser changes**: rows are re-normalised on their next crawl when
  `normalize.PARSER_VERSION` changes; those `job_changes` rows have `cause = 'parser'`.
