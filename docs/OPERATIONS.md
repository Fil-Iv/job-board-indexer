# Operating JobForge

Day-to-day tasks for a running deployment ([DEPLOY.md](DEPLOY.md)). Commands are shown
for the VPS setup; locally replace the prefix with `docker compose run --rm app`. For the
hosted demo (Supabase + Vercel + GitHub Actions) see [Hosted demo](#hosted-demo) first;
the same CLI commands work there with `DATABASE_URL` set to the Supabase session pooler.

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

## Hosted demo

The free hosted setup ([DEPLOY.md, part A](DEPLOY.md#a-hosted-demo-supabase--vercel--github-actions))
replaces the `worker` with GitHub Actions and the `app` container with Vercel:

| Workflow | When | What |
|---|---|---|
| `crawl.yml` | every 6 h (`17 */6 * * *` UTC) + **Run workflow** | `plan` → `crawl` matrix → `finalize` (below) |
| `migrate.yml` | push to `main` touching migrations/models, + manual | `alembic upgrade head`, then `db-security` |
| `ci.yml` | every push / pull request | `make lint`, `make test` |

**crawl.yml in detail**

1. **plan**: `crawl-plan --shards 4 --recover-after-hours 5.5` marks runs left `running`
   by a killed job as failed, then splits the active sources into shards. All sources of
   a platform stay in one shard (the per-host rate limit only holds within one process);
   platforms are balanced by the duration of their latest run, longest first.
2. **crawl** (one job per shard, in parallel): `crawl --ids … --deadline-minutes 270
   --no-dedup --allow-failures`. At the deadline, crawls still running are cancelled and
   recorded as failed; Workday and sitemap boards keep their checkpoint, so the next run
   continues where this one stopped (generic pages: checkpoint valid 24 h; Workday: 2 h).
   The job timeout is 5 h, GitHub's hard limit 6 h. Failed sources are a warning, not a
   failed job; they are in the summary.
3. **finalize** (also after failed shards): `prune`, `dedup`, `check-pending`,
   `db-security` (fails if a public table has RLS off or Supabase's `anon` /
   `authenticated` roles hold any privilege), `crawl-summary` (Markdown
   job summary: runs, found/new/updated/closed per platform, failures) and `db-size`
   (warning above `DB_WARN_MB`, **fails the run above `DB_FAIL_MB`**, so GitHub e-mails
   you).

The workflow's concurrency group `crawl` guarantees two crawls never overlap; a scheduled
run waits for a manual one. With 4 shards × `DATABASE_POOL_SIZE=3` the crawl uses at most
12 connections of the free session pooler (~15). More shards → lower the pool size.

**Add a demo board**: add it to `coverage/demo_sources.yaml` (or regenerate the file with
`make demo-sources` after `make coverage`), push, then **Actions → crawl → Run workflow**
with *seed* ticked. **Many boards at once** (fast JSON platforms): add slugs to
`coverage/candidate_boards.yaml`, run `python scripts/check_boards.py --new --write`
(checks each one live through `HttpClient`: robots.txt, public list, open jobs) and the
boards with ≥ 5 open jobs go to `coverage/demo_sources_extra.yaml`; push, seed as above. **Remove one**: set `active = false` in Supabase (**Table Editor →
sources**) — jobs stay as they are.

### Supabase security alert (`rls_disabled_in_public` and similar)

Supabase e-mails when a table in `public` is reachable through its Data API. Everything
JobIndex needs is in migration 0007 ([DEPLOY.md](DEPLOY.md#database-access-lockdown)):

1. **Actions → migrate → Run workflow** (or push a migration). It runs
   `alembic upgrade head` and then `db-security`, which fails and names each problem
   (table without RLS, privilege held by `anon` / `authenticated`, extra `jobindex_web`
   right). A table created outside the migrations (e.g. in the SQL editor) shows up here.
2. Fix it in a new migration (RLS + `owner_all` / `web_read` policies, revoke from
   `anon`, `authenticated`), never by hand only; `make test` checks the same rules.
3. Verify: `curl` with the anon key returns no rows (DEPLOY.md), `/health`, `/jobs`,
   `POST /mcp` and the next crawl work, and **Advisors → Security Advisor** is clean.

## Size budget

The free Supabase database holds 500 MB; JobForge aims to stay under ~400 MB.

Measured on a copy of the local database (45,598 postings, 2026-11-17), `jobs` table
including TOAST, the full-text `tsvector` and all indexes:

| | `jobs` | per posting | database |
|---|---:|---:|---:|
| full HTML + full text (no limits) | 626.7 MB | **13.7 KB** | 670.9 MB |
| no HTML, text ≤ 1,500 chars (hosted demo since 2026-11-17) | 213.0 MB | **4.7 KB** | 256.9 MB |

What is left per posting: ~1.6 KB `tsvector`, ~1.2 KB compressed text, the GIN index and
the row. With ~25 MB of fixed overhead (catalogs, `job_changes`, `fetch_runs`), **400 MB ≈
80,000 postings**. The hosted demo's settings:

| Lever | Hosted demo value | Effect |
|---|---|---|
| `coverage/demo_sources.yaml` | ~90 boards, 1–2 per done platform, preferring 5–400 jobs | no 14k Marriott / 7k JPMorgan boards |
| `coverage/demo_sources_extra.yaml` | ~320 boards on the fast JSON platforms (Greenhouse, Ashby, Lever, SmartRecruiters, …), checked live by `scripts/check_boards.py`: ~270 tech / cybersecurity / remote-first employers from `coverage/candidate_boards.yaml`, ~50 employers hiring in Bulgaria and under-covered EU countries from `coverage/candidate_boards_europe.yaml` (each verified to have open jobs located there) | ~32k postings at the cap below |
| `coverage/demo_sources_retired.yaml` | boards taken out to make room (2026-11-20: 7 large US-only boards, ≈ 4,000 postings, for the European boards); `retire-sources` deletes them with their postings, `check_boards.py --write` never re-adds them | freed rows are reused by new postings (no `VACUUM FULL`, which would need the table's size again) |
| `MAX_JOBS_PER_SOURCE` | 1000 | ≈ 48k open postings with both files → ≈ 250 MB, + closed postings kept 14 days ≈ 280–300 MB |
| `DESCRIPTION_HTML_MAX_CHARS` | 0 | no HTML is stored (`""`); the API and pages use the text |
| `DESCRIPTION_TEXT_MAX_CHARS` | 1500 | text cut at a word boundary + "…"; the job page links to the full posting at the source. The content hash covers the stored text, so changes past the cut are not detected |
| `JOB_CHANGE_VALUE_MAX_CHARS` | 500 | `job_changes` old/new values cut; description changes are recorded without values ("text changed") |
| `CLOSED_JOB_RETENTION_DAYS` | 14 | `prune` deletes postings closed longer ago (and their history) |
| `JOB_CHANGE_RETENTION_DAYS` | 60 | `prune` deletes older `job_changes` |
| `FETCH_RUN_RETENTION_DAYS` | 30 | `prune` deletes older `fetch_runs` (keeps each source's latest success) |
| `DB_WARN_MB` / `DB_FAIL_MB` | 400 / 450 | `db-size` warning / failed workflow |

All are environment variables (defaults: no limits, keep everything — the VPS setup
keeps full history); in the hosted demo they are set in `crawl.yml` and can be overridden
with repository **variables**.

How the cap works: the connector still fetches the **complete** list (the completeness
and failure rules are unchanged); the crawler then keeps at most N postings — those
already stored first (no churn), then new ones, newest first — and fetches detail pages
only for those. A posting is closed only when it is missing from the full list, never
because the cap left it out. Lowering the cap does not delete stored postings; they stay
until they close and are pruned.

```sh
$CLI db-size                        # total + per table (sizes include indexes and TOAST)
$CLI db-size --markdown --warn-above-mb 400 --fail-above-mb 450
$CLI prune                          # uses the *_RETENTION_DAYS settings
$CLI prune --closed-days 7 --changes-days 30 --runs-days 14
```

`prune` also applies the description limits to rows stored before they were set (and
recomputes their content hash, so the next crawl sees no change), drops description
values from `job_changes`, and then runs `VACUUM FULL` once: Postgres reuses the space
of deleted rows but only gives it back to the file system (what Supabase counts) after a
`VACUUM FULL`. Approaching the limit: lower `MAX_JOBS_PER_SOURCE` or the retention
days, remove big boards, or move to a paid tier ([below](#paid-upgrade-path)).

## Add a source

```sh
$CLI add-source https://jobs.lever.co/spotify
$CLI add-source https://www.figma.com/careers/            # custom domain: platform auto-detected
$CLI add-source https://boards.greenhouse.io/acme --company-name "Acme Inc."
```

`add-source` accepts board URLs, job URLs, embed/API URLs and custom-domain careers pages
(it looks for platform fingerprints in the page). It prints the new source id and the
employer name it found on the board page (for platforms whose postings name no company;
the crawler retries this while the name is empty). If none was found, set one:
`$CLI set-company <id> "Acme Inc."` (needed for HiBob, ADP and PageUp, whose data never
names the employer).

A fresh install can be filled with real data in one step:

```sh
$CLI seed-demo                      # every example board of every done platform (~105)
$CLI seed-demo --platform lever     # only some platforms
$CLI crawl --all                    # first crawl: ~2-3 h (detail pages at 1 req/s per host)
```

`seed-demo` reads `coverage/platforms.yaml` (`examples`, plus `example_companies` for
platforms without a company name in their data) and is idempotent.
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

**`$CLI fill-rates`** (`make fill-rates`, `--markdown` for a table) — per platform, the
share of open postings that have each field (title, company, description, location,
source/apply URL, salary, employment type, remote, posting date). `--strict` exits 1 when a
required field has gaps, for a cron check.

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

## Scaling

**The hosted demo is capped by the free database tier, not by the software.** The same
code, schema and crawler run in all three setups; only the database size (and so the
storage limits in the [size budget](#size-budget)) and the crawl cadence change.
Current demo (2026-11-17): **9,132 open jobs from 101 boards, 57 MB**, as of the last
finished crawl; the crawl now adding the 278 boards of `demo_sources_extra.yaml` takes it
to ≈ 48,000 jobs from ≈ 380 employers, ≈ 300 MB.

| | Free tier (hosted demo) | Supabase Pro | One VPS |
|---|---|---|---|
| Database size | 500 MB (budget ≤ 400 MB) | 8 GB included, more per GB | disk of the VPS (80–240 GB) |
| Approx. job capacity | ≈ 80,000 postings with the demo limits (4.7 KB each: text ≤ 1,500 chars, no HTML, 14-day closed retention); demo runs ≈ 48,000 | ≈ 1.5 M with the demo limits, ≈ 500,000 with full descriptions and full history (≈ 14 KB each) | ≈ 150,000 open jobs (~1,000 boards) in 3–5 GB, ≈ 1.5 M (~10,000 boards) in 30–50 GB, full text and history |
| Monthly cost | €0 (Supabase Free + Vercel Hobby + GitHub Actions on a public repo) | $25 (≈ €23) + usage; Vercel Hobby/Pro; crawler on Actions (€0) or a small VPS (€5–10) | ≈ €10–35 (~1,000 boards), ≈ €30–120 (~10,000 boards) |
| Crawl frequency | every 6 h (GitHub Actions, ≤ 5 h per run, 4 shards) | every 6 h on Actions; hourly with the `worker` on a small VPS | continuous: hourly per board (Workday/Oracle 4 h), per-platform intervals |

Moving up a tier: raise or unset `MAX_JOBS_PER_SOURCE`, `DESCRIPTION_*_MAX_CHARS` and the
`*_RETENTION_DAYS` limits, raise `DB_WARN_MB` / `DB_FAIL_MB`, and seed more boards
(`make seed-demo` registers every example board). Details and prices:
[below](#capacity-and-monthly-cost).

## Capacity and monthly cost

### Hosted demo: €0/month

Supabase Free + Vercel Hobby + GitHub Actions on a public repository cost nothing. The
limits that matter (as published by the providers in 2026; check their pricing pages):

| Service | Free tier | What it means for JobForge |
|---|---|---|
| Supabase Free | **500 MB database**, 5 GB egress/month, shared pooler (~15 session connections), 2 free projects per account, no downloadable backups | The [size budget](#size-budget) keeps the demo at ~300 MB; `crawl.yml` fails loudly above 450 MB |
| | **Paused after 7 days without activity** | The 6-hourly crawl keeps it active. If the crawl stops (e.g. disabled workflow), restore the project in the dashboard |
| Vercel Hobby | Free, **non-commercial use only**; 100 GB transfer/month; function limits (duration, invocations) far above a demo's traffic | Fine for a demo; a commercial service needs Vercel Pro |
| GitHub Actions | **Free and unlimited on standard runners for public repositories**; job max 6 h; ~20 concurrent jobs | 4 shards × ≤ 5 h every 6 h fits. A **private** repository gets only 2,000 free minutes/month — the crawl alone uses far more (see below) |
| | Scheduled workflows are **disabled after 60 days without repository activity** (public repos); cron runs can start late under load | Push a commit (or re-enable the workflow in the Actions tab) at least every 60 days, otherwise crawling stops and Supabase pauses a week later |

### Paid upgrade path

When the demo outgrows the free tiers, upgrade only the part that hits a limit:

| Need | Upgrade | Price (2026 list) |
|---|---|---|
| More than ~400 MB of postings, no pausing, daily backups | Supabase **Pro** (8 GB disk included, then per GB; daily backups 7 days) | $25/month (≈ €23) + usage |
| Commercial use of the website, team members | Vercel **Pro** | $20/user/month |
| Private repository | GitHub Actions minutes cost $0.008/min (Linux) beyond 2,000/month: a 4-shard crawl at ~1 h each, 4×/day ≈ 29k min/month ≈ $220/month — run the crawler on a VPS instead | VPS ≈ €5–10/month |
| Full index (~1,000+ boards, continuous crawling, full history) | [One VPS](DEPLOY.md#b-alternative-one-vps-docker-compose) for everything (below) | ≈ €10–35/month |

A sensible middle step is Supabase Pro + Vercel (Hobby or Pro) + the crawler `worker` on a
small VPS pointed at Supabase (`DATABASE_URL` = session pooler, or the direct connection if
the VPS has IPv6): continuous crawling without the 6-hour cadence.

### VPS: estimates for planning

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

* Hosted demo only (approved by the project owner, see CLAUDE.md): **Supabase** (Postgres,
  free tier), **Vercel** (API + search page), **GitHub Actions** (crawler, migrations, CI).
* **The crawled platforms' public endpoints** (see [CONNECTORS.md](CONNECTORS.md)) — no
  accounts, keys or contracts; their availability and terms decide what we can index.
  Undocumented endpoints (Workday CXS, BambooHR, Rippling, Breezy JSON, Pinpoint, Trakstar,
  Oracle CE) can change without notice.
* VPS setup: **Let's Encrypt** (ACME) via Caddy — certificate issuance/renewal; needs
  ports 80/443; **DNS** for `DOMAIN` (any registrar/DNS host); a **VPS** with Docker Engine
  and the Compose plugin (v2.24+).

Build-time:

* Docker Hub images: `python:3.12-slim`, `postgres:16`, `caddy:2`.
* PyPI packages (`pyproject.toml`): fastapi, uvicorn, sqlalchemy, alembic, psycopg,
  httpx, apscheduler, pydantic-settings, selectolax, pycountry, defusedxml, jinja2, pyyaml;
  dev: pytest, pytest-asyncio, ruff, mypy, type stubs. Vercel installs `requirements.txt`
  (the same list without uvicorn, APScheduler and Alembic).
* Optional extra `browser`: **Playwright** + its headless Chromium (approved by the project
  owner 2026-11-11 for browser-rendered connectors only: jobBoardHQ, ...). Not installed by
  Vercel, CI or the default dev image. The crawl workflow installs it only in shards that
  contain such a platform (`uses_browser` connectors). Locally:
  `docker compose build --build-arg INSTALL_BROWSER=1 app` (or
  `pip install -e ".[browser]" && playwright install chromium`). Without it those
  sources fail with "Playwright is not installed" and their jobs are left untouched.
* GitHub Actions: `actions/checkout`, `actions/setup-python`.

Nothing else: no queue, cache, search engine or other cloud service. Off-site backup
storage is the one recommended addition and needs the project owner's approval.

## Routine maintenance

Hosted demo: glance at the latest **crawl** run summary weekly (failures, database size);
make sure the repository has activity at least every 60 days (scheduled workflows stop
otherwise); `make demo-sources` after `make coverage` when boards change.

VPS:

* **Weekly**: glance at `/stats` (`failing_sources`, platforms whose `last_success_at` is
  old). Fix or deactivate sources failing for days.
* **Monthly**: `git pull && make prod-up`; check backups exist and are copied off-site;
  `docker system prune -f` to drop old images.
* **Disk**: `docker system df`, `du -sh backups`; Postgres autovacuum handles the tables.
* **Parser changes**: rows are re-normalised on their next crawl when
  `normalize.PARSER_VERSION` changes; those `job_changes` rows have `cause = 'parser'`.
