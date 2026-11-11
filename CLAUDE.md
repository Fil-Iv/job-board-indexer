# JobIndex — permanent project rules

JobIndex indexes job postings from 58 ATS platforms and 20 job-board platforms into one
searchable, continuously updated PostgreSQL index. The code is going to be handed off to
another team, so it has to be clean, documented and easy to run.

## Stack (fixed)

- Python 3.12, FastAPI, PostgreSQL 16 (full-text search via `tsvector`),
  SQLAlchemy 2.0 + Alembic, httpx (async), APScheduler, pytest, Docker Compose.
- **No other infrastructure** (queues, caches, search engines, cloud services, etc.)
  without explicit approval from the project owner. Plain Python libraries that support
  the stack are fine; mention new dependencies when you add them.
- **Approved hosting (2026-11-08)** for the public demo — nothing else without approval:
  Supabase Postgres (free tier, 500 MB) as the database, Vercel for the API + search page
  (Python function, read-only DB use, no crawling/scheduling there), GitHub Actions for
  the crawler, migrations and CI. All connections go through the Supabase pooler
  (session :5432 for crawler/migrations, transaction :6543 for Vercel; prepared
  statements off). The repository is **public**: never commit secrets, `.env` files,
  dumps or personal data. docker-compose stays the local dev setup; the single-VPS
  deployment stays documented as the alternative (docs/DEPLOY.md).
- The demo must stay under ~400 MB (docs/OPERATIONS.md#size-budget).

## Connectors

- Every connector lives in `jobindex/connectors/<platform>.py`, subclasses
  `BaseConnector` (`jobindex/connectors/base.py`) and registers itself with `@register`.
- Every connector has a test in `tests/connectors/test_<platform>.py` that runs against a
  **saved real response** (JSON/HTML) stored in `tests/fixtures/<platform>/`.
  Tests never hit the network.
- `fetch_jobs()` returns the **complete** list of open postings for a board or raises.
  Never return a partial list (e.g. if page 3 of 5 fails, raise) — a missing posting is
  interpreted as "closed".

## Crawling etiquette

- Respect `robots.txt`, send a clear User-Agent, rate-limit per host
  (default 1 request/second), retry transient errors with exponential backoff.
- All HTTP goes through `jobindex.http.HttpClient`, which enforces the above.
  Connectors must not create their own `httpx` clients.
- Browser-rendered connectors (Playwright, owner approval 2026-11-11, robots-allowed
  pages only) go through `jobindex.browser.PoliteBrowser`, which applies the same rules
  to every request the page makes and fails on disallowed URLs, 401/403/429 and
  challenges. Never read tokens out of a page to replay them elsewhere.

## Failure semantics

- **A failed fetch is NEVER treated as "all jobs closed".** It is recorded as a fetch
  failure (`fetch_runs.status = 'failed'`, `sources.consecutive_failures += 1`) and the
  source's jobs are left untouched. Jobs are only closed after a *successful* fetch that
  no longer contains them.

## Data sources, in order of preference

1. Official public JSON endpoints / APIs.
2. HTML parsing with `selectolax` — only when no API exists.
3. Headless browser (Playwright) — last resort only, requires justification.

## Code quality

- Type hints everywhere; `ruff check`, `ruff format --check` and `mypy` (strict) must pass.
- Run `make lint` and `make test` before considering a change done.
- Any change to `jobindex/normalize.py` behaviour bumps `normalize.PARSER_VERSION` (and
  its history comment), then `make parser-fingerprint`. `tests/test_parser_version.py`
  enforces this. It lets `job_changes.cause` separate parser noise from real changes.
- Schema changes go through an Alembic migration; `tests/test_migrations.py` fails if the
  models and migrations drift apart.
