# JobIndex

A job board indexer: pulls postings from ATS platforms (Greenhouse, Lever, Workday, …) and
job-board platforms into one searchable, continuously updated PostgreSQL index.

Project rules (stack, connector conventions, crawling etiquette) are in [CLAUDE.md](CLAUDE.md).

## Quick start

Requirements: Docker with Compose v2.24+ and `make`. Everything runs in containers
(Python 3.12), so no local Python setup is needed.

```sh
make up        # build + start postgres and the API (http://localhost:8000/docs)
make migrate   # apply database migrations
make test      # offline test suite (fixtures, jobindex_test database)
make test-live # live tests against real employer boards (network)
make lint      # ruff + mypy
make coverage  # live-crawl done platforms' examples, regenerate docs/COVERAGE.md
```

Platform coverage and the work plan for the remaining platforms live in
[docs/COVERAGE.md](docs/COVERAGE.md), generated from `coverage/platforms.yaml`.

Register and crawl a board:

```sh
docker compose run --rm app python -m jobindex.cli add-source https://jobs.lever.co/spotify
docker compose run --rm app python -m jobindex.cli add-source https://www.figma.com/careers/
docker compose run --rm app python -m jobindex.cli crawl 1      # or: make crawl SOURCE=1
make crawl                                                     # all active sources
```

`add-source` accepts hosted board URLs, job URLs, embed URLs, API URLs, and custom-domain
careers pages. For the last kind it downloads the page (respecting robots.txt) and looks for
platform fingerprints (embed scripts, iframes, links to the hosted board).

## Supported platforms

| Platform | Endpoint | Notes |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true` | One request; descriptions + pay ranges |
| Lever | `api.lever.co/v0/postings/{site}?mode=json` | Paginated (`skip`/`limit`); EU boards as `eu:{site}` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | One request; structured compensation |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` (+ detail per job) | **Blocked by robots.txt** (see below) |

**SmartRecruiters:** `api.smartrecruiters.com/robots.txt` disallows every user agent except
LinkedInBot. The connector is implemented and tested against fixtures, but real crawls are
refused by the HTTP client and recorded as fetch failures. Enabling it needs a decision:
obtain permission/API access from SmartRecruiters, or switch to another permitted source.

## Layout

| Path | Purpose |
|---|---|
| `jobindex/models/` | SQLAlchemy models: `sources`, `jobs`, `fetch_runs`, `job_changes` |
| `jobindex/connectors/base.py` | `RawJob`, the `BaseConnector` interface and its contract |
| `jobindex/connectors/registry.py` | `@register`, `resolve_url()` (URL patterns + custom-domain HTML fingerprints) |
| `jobindex/connectors/<platform>.py` | One connector per platform |
| `jobindex/http.py` | Shared polite HTTP client: User-Agent, robots.txt, per-host rate limit + Crawl-delay, timeouts, retry/backoff |
| `jobindex/robots.py` | RFC 9309 robots.txt parser (wildcards, longest match) |
| `jobindex/normalize.py` | Location / country / remote / salary / employment-type parsing, HTML→text, `content_hash` |
| `jobindex/crawler.py` | `crawl_source()`: runs a connector and reconciles results with the DB |
| `jobindex/dedup/` | Cross-source dedup (`canonical_job_id`) goes here |
| `jobindex/api/` | FastAPI app: `/health`, `/jobs?q=…` (full-text), `/jobs/{id}`, `/sources` |
| `jobindex/scheduler/` | APScheduler job that crawls all active sources (enable with `SCHEDULER_ENABLED=true`) |
| `jobindex/cli/` | `python -m jobindex.cli add-source <url>` / `crawl <id>\|--all` |
| `jobindex/coverage.py` | Coverage report logic (`scripts/coverage_report.py`, `make coverage`) |
| `coverage/platforms.yaml` | All 78 target platforms: status, tier, endpoints, URL patterns, examples |
| `migrations/` | Alembic migrations |
| `tests/fixtures/<platform>/` | Saved real responses used by connector tests |

## How a crawl works

1. A `fetch_runs` row is created (`running`).
2. The source's connector returns the complete list of open postings — or raises.
   Platforms without descriptions in their list endpoint (SmartRecruiters) get one detail
   request per *new or changed* posting; unchanged postings reuse stored details.
   Postings are then normalised (`RawJob.normalized()`).
3. **Failure**: the run is marked `failed`, `sources.consecutive_failures` is incremented,
   and no job is modified. A failed fetch is never interpreted as "all jobs closed".
4. **Success**: new postings are inserted; postings whose `content_hash` changed are updated
   with one `job_changes` row per changed meaningful field (`normalize.HASH_FIELDS`);
   cosmetic changes (URLs, HTML markup, dates) are applied silently; postings that
   disappeared are closed; closed postings that reappear are reopened.

Full-text search uses a generated, weighted `tsvector` column (title > company >
description) with a GIN index, queried via `websearch_to_tsquery`.

## Adding a connector

1. Create `jobindex/connectors/<platform>.py` with a `@register`-decorated `BaseConnector`
   subclass implementing `detect`, `board_url` and `fetch_jobs` (plus `fetch_job_detail`
   if the list lacks descriptions). Use `self.http` for every request. Check the API
   host's robots.txt first.
2. Save a real response (trimmed) into `tests/fixtures/<platform>/`.
3. Add `tests/connectors/test_<platform>.py`: URL-detection table, fixture test via
   `tests.fakes.mock_http`, and a `@pytest.mark.live` test against 2 real employers.
   `tests/connectors/test_lever.py` is a good template.

## Configuration

See [.env.example](.env.example). `make up` copies it to `.env` if missing. Set
`HTTP_USER_AGENT` to include a real contact URL before crawling for real.
