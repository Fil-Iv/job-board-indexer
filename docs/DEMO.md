# Reviewer walkthrough

Ten steps through the running system, about 15 minutes. Steps 1–8 need only a browser or
`curl`; steps 9–10 need Docker and `make` (README, *Quick start*).

**Live demo: <https://job-board-indexer.vercel.app>** (Vercel + Supabase Postgres,
refreshed by GitHub Actions every 6 h; see [DEPLOY.md](DEPLOY.md), part A). The hosted demo
indexes ~90 small boards with at most 200 postings each, so it stays inside the free
tier ([size budget](OPERATIONS.md#size-budget)); the full audit ran on a local database
with 43k postings ([CHALLENGE_CHECKLIST.md](CHALLENGE_CHECKLIST.md)).

Set the host once for the `curl` examples:

```sh
B=https://job-board-indexer.vercel.app
```

## 1. Search page

Open <https://job-board-indexer.vercel.app/?q=engineer&location=Berlin>.

Each card is one **opening**. Under it, every listing of that opening is shown with its
platform and links: "posting" (the page on the original site) and "apply". When the
platform's apply link *is* the posting page, one link reads "posting · apply". Filters:
keyword, company, location, remote, employment type, minimum salary, posted since, status;
pagination at the bottom.

## 2. Is it alive?

```sh
curl -s $B/health                 # {"status":"ok","database":"ok"}
curl -s $B/stats | python3 -m json.tool | head -30
```

`/stats` has the totals (sources, open postings, open openings), `last_crawl_at`, and
per platform: sources, open jobs, last success and last failure.

## 3. API: keyword, company, location

```sh
curl -s "$B/jobs?q=engineer&size=2"                         # full-text (title > company > description)
curl -s "$B/jobs?company=gitlab&size=2"                     # case-insensitive contains
curl -s "$B/jobs?location=Austin,%20TX&size=2"              # city / region / country, combined
curl -s "$B/jobs?location=DE&q=sales&size=2"                # ISO country code
```

## 4. API: more filters and pagination

```sh
curl -s "$B/jobs?q=python&remote_type=remote&size=5"
curl -s "$B/jobs?employment_type=full_time&salary_min=100000&salary_currency=USD&size=5"
curl -s "$B/jobs?posted_since=2026-11-01&platform=greenhouse&size=5"
curl -s "$B/jobs?status=closed&size=5"                      # postings that disappeared / expired
curl -s "$B/jobs?q=engineer&page=2&size=5"                  # response: total, page, size, pages, items
curl -s "$B/jobs?size=500"                                  # 422: size is capped at 100
```

All parameters, with descriptions and examples, are in the OpenAPI docs:
<https://job-board-indexer.vercel.app/docs>.

## 5. One record shape for every platform

```sh
curl -s "$B/jobs?platform=workday&size=1" | python3 -m json.tool
curl -s "$B/jobs?platform=personio&size=1" | python3 -m json.tool
```

Same fields everywhere: title, company, `location_raw` + `city`/`region`/`country`
(ISO code), `remote_type`, `employment_type`, salary min/max/currency/period, `posted_at`,
`first_seen_at`, `last_seen_at`, `status`, and per listing `source_url` / `apply_url`.
`GET /jobs/{id}` adds the description. Field fill rates per platform:
[CHALLENGE_CHECKLIST.md §2](CHALLENGE_CHECKLIST.md#2-consistent-job-records--).

## 6. Deduplication

```sh
curl -s "$B/jobs/5632" | python3 -m json.tool
```

One opening ("Bank Supported Housing Support Worker", YMCA DownsLink Group, Crawley)
listed on two job boards on two different platforms (JobBoard.com: Homeless Link, Jobiqo:
Love Local Jobs); the API returns it once, with both listings in `sources`. Rules and the
checked merges / near misses: [CONNECTORS.md](CONNECTORS.md#deduplication),
[CHALLENGE_CHECKLIST.md §4](CHALLENGE_CHECKLIST.md#4-deduplication--).

## 7. Automatic updates and failures

* Crawl runs: <https://github.com/Fil-Iv/job-board-indexer/actions/workflows/crawl.yml>.
  Open a run → **Summary**: per-platform counts (found / new / updated / closed / failed)
  and the database size.
* Failing boards: `curl -s "$B/sources?failing=true"`. A failed fetch is recorded
  (`consecutive_failures`, `last_error`) and **never closes the board's jobs**; jobs are
  closed only by a successful fetch that no longer lists them (`status=closed` above).
* Locally the same runs continuously: `python -m jobindex.cli schedule` (the `worker`
  service of the VPS setup, DEPLOY.md part B).

## 8. Coverage evidence (78 platforms)

| What | Where |
|---|---|
| Status of every platform (done / blocked / not built), endpoints, examples | [`coverage/platforms.yaml`](../coverage/platforms.yaml) → generated [COVERAGE.md](COVERAGE.md) |
| Live job count + last successful run of every example board (2 employers per platform) | [COVERAGE.md](COVERAGE.md) (`make coverage`) |
| Sites that refuse us: request-level evidence (URL, time, HTTP status, robots.txt rule) | [`coverage/blocked_by_site.yaml`](../coverage/blocked_by_site.yaml) |
| Connector tests on saved real responses | `tests/connectors/test_<platform>.py`, `tests/fixtures/<platform>/` |
| Audit of all six criteria with evidence | [CHALLENGE_CHECKLIST.md](CHALLENGE_CHECKLIST.md) |

On the live demo: `curl -s $B/stats` lists the platforms with indexed jobs, and
`curl -s "$B/sources"` lists every board.

## 9. Add a source (locally)

```sh
make up && make migrate
docker compose run --rm app python -m jobindex.cli add-source https://jobs.lever.co/spotify
docker compose run --rm app python -m jobindex.cli add-source https://www.figma.com/careers/   # custom domain → Greenhouse
make crawl SOURCE=1
open http://localhost:8000/?company=spotify
```

`add-source` recognises board URLs, job URLs, embed/API URLs and custom-domain career
pages (platform fingerprints in the page). **Hosted demo:** add a `- platform: …, url: …`
entry to [`coverage/demo_sources.yaml`](../coverage/demo_sources.yaml), push, then
**Actions → crawl → Run workflow** with *First register the boards…* ticked. More:
[OPERATIONS.md](OPERATIONS.md#add-a-source).

A new **platform** = one connector module + a test on a saved real response:
[CONNECTORS.md](CONNECTORS.md#adding-a-connector).

## 10. Tests and code quality

```sh
make test       # ~890 offline tests (fixtures; no network), separate test database
make lint       # ruff check, ruff format --check, mypy --strict
make test-live  # optional: live tests against 2 real employers per platform
```

CI runs `make lint` and `make test` on every push:
<https://github.com/Fil-Iv/job-board-indexer/actions/workflows/ci.yml>.
