# Challenge checklist

**Live demo: <https://job-board-indexer.vercel.app>** (search page; API docs at
[/docs](https://job-board-indexer.vercel.app/docs)). Reviewer walkthrough:
[DEMO.md](DEMO.md).

Audit of JobIndex against the six challenge completion criteria. Full audit run
end-to-end on real data on **2026-11-08** (UTC), locally: `make up` → `make migrate` →
`make seed-demo` (every example board in `coverage/platforms.yaml`) → `make crawl` (two
passes, a third for 8 sources after fixes) → dedup → API, UI and DB queries. **Re-verified
on 2026-11-10** with fresh evidence from the live demo, two GitHub Actions crawls, a new
`make coverage` run and a fresh clone (next section). Every claim names the command,
endpoint, test or file that shows it.

**Status legend:** ✅ met · 🟡 partially met (gap stated) · ❌ not met

| # | Criterion | Status | Summary |
|---|---|---|---|
| 1 | Working coverage across all 78 platforms | 🟡 | 60 platforms done: 58 work on ≥ 2 real employers (BrassRing only for sites with ≤ 50 jobs), FirstStage and JobKit are partial (only one live board exists); 13 are blocked (refused by the site, or talentReef: only an internal host; evidence in `blocked_by_site.yaml`); 5 are not built, each with the exact reason in `platforms.yaml`. 2026-11-10: all 117 example boards crawled OK |
| 2 | Consistent job records | 🟡 | Local audit DB (43,203 open postings): title, source URL, apply URL 100%; company, description 100% (rounded), location 99%. Live demo (7,132): title, source URL, apply URL 100%, company 99.8%, location 99.0%. Every remaining gap is a field empty at the source (listed). Fixed 2026-11-10: BrassRing lost location/apply URL on re-crawls; "Bengaluru, KA,IN, IN" locations |
| 3 | Automatic updates | 🟡 | Scheduler worker (VPS), change log, closing, failure ≠ closure: shown on live data and in tests. Hosted demo: 3 GitHub Actions crawls (all manually started; failures recorded, jobs kept); the 6-hourly **schedule** has not fired yet (repository public since 2026-11-10 16:48 UTC; first slot after that, 18:17, produced no run) |
| 4 | Deduplication | ✅ | Local: 16 real cross-source duplicates merged (43,203 postings → 43,187 openings), 2,924 same-employer and 667 cross-employer title groups and 4 near misses kept apart, each checked. Live (2026-11-10 19:45 UTC): 7,314 postings → 7,307 openings; 2 cross-platform merges (jobs 5630, 5632: Homeless Link `jobboardcom` + Love Local Jobs `jobiqo`) and 5 across the two Exenzo boards (e.g. job 7173) |
| 5 | Searchable output | ✅ | `/` search page + `/jobs` API with keyword, company, location (+ 7 more) filters, pagination; every OpenAPI parameter documented with an example; all re-checked on the live demo |
| 6 | Complete handoff | ✅ | Source, deployment (re-run from a fresh clone), connector docs, reviewer walkthrough, monthly cost, external dependencies |

---

## Re-verification 2026-11-10 (fresh evidence)

| # | Check | Result |
|---|---|---|
| 1 | `make coverage` (live crawl of every example board's job list), 18:39–18:46 UTC | **120 of 120** current example boards of the 60 done platforms OK: 117 in this run, plus the 3 jobboardsoftware.com (Exenzo) boards added and crawled at 19:22 UTC (`coverage/results.json`, `docs/COVERAGE.md` "Generated 2026-11-10 19:22 UTC"). The 4 older entries without a fresh run are retired examples (ADP `d4d64eb4…`, PageUp `851/ci`, Bosch, inDrive). |
| 1 | GitHub Actions crawl [run 42877398454](https://github.com/Fil-Iv/job-board-indexer/actions/runs/42877398454) (seed + crawl, 4 shards, 18:39–18:49 UTC) | **88 of 88 sources OK, 0 failed**, 56 platforms with indexed jobs; Avature/Bloomberg (failing in the run before: "sitemap lists 334 jobs but the board says 337", fixed by `540ee72`) now 200 postings. Database 121.4 MB (budget: warn 400, fail 450). |
| 2 | Live index paged through `/jobs?size=100` (all 7,128 open openings / 7,130 postings) | title, source URL 100%; company 99.8% (WP Job Manager startjob.dk, Paradox); location 98.7%; apply URL 100% before the second crawl. Location gaps: BrassRing/Halifax (the job detail has no location field, only `latitude 0.0`), remote-only postings on JobKit (10 of 11), Zoho (4 of 11), SmartJobBoard/EdSurge (9 of 22). |
| 2 | Posting vs apply link | No posting in either database lacks `apply_url` (0 of 43,203 locally, 0 of 6,727 live before the second crawl). 4,185 live postings have apply URL = posting URL (the platform's apply form is on the posting page): the search page now shows one "posting · apply" link for them (`test_search_page_link_labels`). |
| 2 | Bug found by this check | After the second hosted crawl BrassRing `apply_url` fell to 6% and location to 0%: fields set by `fetch_job_detail` but missing from `detail_fields` are dropped when stored details are reused. Fixed for BrassRing (+ Polymer `employment_type`), one-time page re-read via `list_version`; guard `tests.fakes.assert_detail_fields_cover`. |
| 2 | Location format | Eightfold sent `addressRegion: "KA,IN"` → "Bengaluru, KA,IN, IN"; JBoard the whole "City, Region, Country" in `addressLocality`. Fixed in `jobindex/schemaorg.py` + `normalize` (`PARSER_VERSION` 4: "Bengaluru, KA, IN" is India, not Indiana); stored pages re-read once (`detail_version`). Live after the crawl: 182 Eightfold postings updated, **0** regions containing a comma (was 183). |
| 3 | Updates on the live demo | Second crawl (run 42877398454): per source `new` / `updated` / `closed` counts in the shard logs, e.g. source 14 (Eightfold/PayPal) new 3, updated 182, closed 1; source 39 new 3, closed 3. `/jobs?status=closed` → 37 closed postings. |
| 3 | Failure ≠ closure on the live demo | First crawl ([run 42860937557](https://github.com/Fil-Iv/job-board-indexer/actions/runs/42860937557)): Avature/Bloomberg failed → recorded (`/sources?failing=true`: `consecutive_failures` 1, `last_error`), 0 jobs touched; the next successful crawl reset it (`failing_sources` 0 in `/stats`). |
| 3 | Failure ≠ closure, third crawl ([run 42880033330](https://github.com/Fil-Iv/job-board-indexer/actions/runs/42880033330), 18:55 UTC) | 86 of 88 OK. Two real failures, both recorded with jobs untouched (`/sources?failing=true`): Workday/NVIDIA "got 2797 of 2798 postings; refusing a partial result" (**200 open jobs kept**), JobMount/jobnet.wacities.org robots.txt unreachable during the run → disallow-all (RFC 9309; the file allows the sitemap when it answers) (**90 open jobs kept**). The same run restored BrassRing (apply URL 100%, location 31%: the rest has no location at the source); live apply URL 100%, location 99.0%, `job_changes` 481 rows, DB 120.5 MB. |
| 3 | Schedule | `crawl.yml` cron `17 */6 * * *`; **no scheduled run yet** (see summary). Not verifiable before 2026-11-11 00:17 UTC. |
| 4 | Live dedup | `/jobs/5632` and `/jobs/5630`: YMCA DownsLink Group, "(Bank) Supported Housing Support Worker", Crawley / Horsham — each one opening with two listings (JobBoard.com Homeless Link + Jobiqo Love Local Jobs); dedup log "7167 postings -> 7165 openings". |
| 5 | Live search and API | `/health` ok; `q=engineer` 2,822 openings (page 2 of size 5 works); `company=gitlab` 200; `location=Berlin` 39; `location=Austin, TX` 11; `q=python&remote_type=remote` 96; `employment_type=full_time&salary_min=100000&salary_currency=USD` 922; `posted_since=2026-11-01` 2,167; `status=closed` 13 → 37 after the second crawl; `size=500` → 422. Search page `/?q=engineer&location=Berlin` → "9 openings". All [DEMO.md](DEMO.md) commands run. |
| 6 | Fresh clone (`git clone` into an empty folder, only README + DEPLOY.md) | `make up` (59 s), `make migrate`, `make test` (882 passed), `make lint`, `add-source` Lever/Spotify + Figma (custom domain → Greenhouse), `make crawl SOURCE=…`, search page + API + `/docs`, `seed-demo FILE=…`, `db-size`, `fill-rates`, `dedup-report`: all OK. DEPLOY part B smoke test (`DOMAIN=localhost`): all 6 services healthy in 45 s, `https://localhost/health` ok, backup written. Fixed in the docs: ports 8000/5432 must be free (`APP_PORT`/`POSTGRES_PORT`), open the search page after `make migrate`, `make prod-*` targets don't take `-p`. DEPLOY part A was not repeated with new accounts; it is what runs the live demo. |
| all | `make lint`, `make test`; CI | ruff, ruff format, mypy strict pass; 890 tests pass; CI green on every push. |

---

## 1. Working coverage across all 78 platforms — 🟡

**Evidence**

* Plan and status of all 78 platforms (58 ATS, 20 job boards): `coverage/platforms.yaml`,
  rendered to [`docs/COVERAGE.md`](COVERAGE.md) by `make coverage`, which live-crawls every
  example board's job list and records the job count and last successful run.
* Every `done` platform has ≥ 2 example boards from **different employers** (enforced by
  `jobindex/coverage.py`, `tests/test_coverage.py`), a connector module and a fixture
  test (`tests/connectors/test_<platform>.py`, saved real responses in
  `tests/fixtures/<platform>/`), plus a `@pytest.mark.live` test against both examples
  (`make test-live`).
* Live runs into the database (`make seed-demo && make crawl`, 110 active sources): see the table
  at the end of this section (from `sources` / `fetch_runs` / `jobs`).

**Status by platform (78)**

| Group | Count | Platforms |
|---|---|---|
| Working (connector + 2 live employers) | **58** | ADP, ApplicantPro, Ashby, **Avature** (new), BambooHR, **BrassRing** (new; sites with ≤ 50 jobs only), Breezy HR, Comeet, Deel, Eightfold AI, Gem, GoHire, Greenhouse, HiBob, HireBridge, HireHive, Hireology, HiringThing, iCIMS, iSolved Hire, JazzHR, Jobvite, Kula, Lever, Manatal, Oracle Recruiting, PageUp, **Paradox** (new), Paylocity, Personio, Phenom, Pinpoint, Polymer, Recooty, Recruitee, Rippling, Rival, SAP SuccessFactors, SmartRecruiters, **Taleo** (new), Teamtailor, Trakstar, TriNet, WeRecruit, Workable, Workday, **Zoho Recruit** (new) (ATS); JBoard, **JobBoard.com** (new), JobBoard.io, **jobboardsoftware.com** (Exenzo; new 2026-11-10), Jobboardly, Jobiqo, JobMount, Niceboard, SmartJobBoard, Wiley Madgex, WP Job Manager (job boards) |
| Blocked — no allowed route | 7 | **Artha** (2026-11-10: job pages 403 with a Cloudflare challenge; the job API picks the tenant from the browser `Origin` header, not faked), Paycor (`Disallow: /`), UltiPro (`Disallow: */JobBoardView` + sitemap), Freshteam (jobs widget disallowed), JobBoardFire (403 incl. robots.txt), CareerSiteCloud (403 incl. robots.txt; no customer board found either), **talentReef** (only route is an internal search service, no public listing: career pages are JS shells, sitemap 403; owner decision: the `prod-kong.internal.talentreef.com` Elasticsearch proxy is not used) |
| Blocked by the site — job API refused, no other complete source | 6 | **CSOD** (search API 401 without the page's bearer token; pages are JS shells), Paycom (401, page-issued token), Dover (`Disallow: /api/`), CareerPlug (`Disallow: /jobs.js`, job pages under disallowed `/apps/`), JOIN.com (jobs sitemap 403), Dayforce (search API 403) |
| Done, partial (connector + the only live board found) | **2** | **FirstStage** ("80 jobs open", 79 linked; up to 1 unlinked job tolerated, a larger gap fails the crawl), **JobKit** (jobs.rubyonrails.org; the 8 other `*.jobkit.io` boards found are all empty) |
| Not built | 5 | Route needs page-issued state: jobBoardHQ (paging only via `__VIEWSTATE` postback); listing not complete or countable: ejobsitesoftware.com (feeds capped at 200/20, POST search); no allowed customer board and no public vendor demo board (re-checked 2026-11-10): eJobBoard (domain parked), Nodex (demo = screenshots), Talenetic (announced customers gone or `Disallow: /`) |

Blocked-platform evidence: `coverage/blocked_by_site.yaml` (19 entries; each is a real
request with URL, timestamp, HTTP status and the deciding robots.txt rule, or — for
talentReef's internal host — a `policy` reason why an open route is not used, rendered in the
"Blocked by site" section of `COVERAGE.md`). Tenant-level blocks are listed there too
(SmartRecruiters/Bosch opted out, Pinpoint/dazn, Wiley Madgex/Guardian, Economist,
Campaign, BMJ, PageUp internal `/ci/` portals).

**What was fixed in this audit**

* **Wiley Madgex** — new connector `jobindex/connectors/madgex.py` (paged listing checked
  against "Found N jobs" + JSON-LD job pages); examples jobs.sciencecareers.org (522 jobs)
  and timeshighereducation.com/unijobs (1,203). Earlier examples were dead (New Scientist
  Jobs closed) or 403. Tests: `tests/connectors/test_madgex.py` (fixtures, missing page
  fails, count mismatch fails, live).
* **Eightfold AI** — new connector `jobindex/connectors/eightfold.py`: the JSON API is 403
  (still recorded as blocked), but robots.txt allows `/careers` and declares a complete
  sitemap; job pages carry JSON-LD. PayPal 303 jobs, Vodafone 1,022. Tests:
  `tests/connectors/test_eightfold.py`.
* **GoHire / Rival** — live crawls failed ("4 of 15 job pages had no JobPosting"): both
  platforms drop JSON-LD for older postings that are still listed. New generic hook
  `parse_page_html` maps those pages from HTML (`gohire.py`, `rival.py`); tests
  `test_listed_job_without_jsonld_is_parsed_from_html` in both test modules.
* **SmartRecruiters** — example BoschGroup opted out (robots.txt); replaced by Wise (427
  jobs); stale notes ("live crawls are refused") corrected.
* **PageUp** — robots.txt now disallows `/*/ci/` (internal-candidate portals); example
  `851/ci/en-us` replaced by the external portal `851/cw/en-us` (115 jobs); evidence added
  to `blocked_by_site.yaml`.
* **JazzHR** — boards on the legacy "resumator" theme failed ("no job list"); now parsed
  (`test_legacy_resumator_theme`).
* **`make seed-demo`** (`python -m jobindex.cli seed-demo`) — registers every example
  board (idempotent; `tests/test_cli.py::test_seed_demo_registers_examples`).
* **`crawl --all` crashed on real data** (`QueuePool limit of size 5 overflow 10 reached`)
  with `CRAWL_CONCURRENCY` > 15: each crawl held a pooled connection idle in a transaction
  for its whole fetch, and one crash cancelled every other crawl. Fixed in
  `jobindex/crawler.py` / `jobindex/db.py`; tests
  `test_no_db_connection_held_during_fetch`, `test_crashing_source_does_not_cancel_the_others`.

**Second pass (2026-11-08, later the same day): the 17 platforms that were not built**

* **Taleo** — `jobindex/connectors/taleo.py`: Taleo's own career-section sitemap
  (`/careersection/sitemap.jss?portalCode={section}`) + `jobdetail.ftl` pages (posting parsed
  from the page's `fillList` data; JSON-LD where the tenant enables Google markup). An
  empty sitemap of a switched-off section fails the crawl. Live: WSP, Stantec (HDR too).
* **Paradox** — `jobindex/connectors/paradox.py`: employer-domain career sites (CNAME
  `*.sites.paradox.ai`) via per-brand/per-locale sitemaps + JSON-LD; translations folded into
  the default-language page. Live: FedEx (~4,735 jobs), Marriott (~14,188).
* **Avature** — `jobindex/connectors/avature.py`: portal sitemap, checked against the
  search page's "N results"; the paged search is not used because its order is unstable
  (Bloomberg: 9 of 323 jobs on two pages, 9 on none). Live: Bloomberg (323), UCLA Health (739).
* **Zoho Recruit** — `jobindex/connectors/zoho.py`: job list embedded in the careers page,
  full posting in the job page's inline JSON (the earlier "no descriptions" finding came
  from a truncated read of the 1.7 MB page). Live: Cause Capacity, Zoho USA.
* **JobBoard.com** — `jobindex/connectors/jobboardcom.py`: customer boards found in the
  vendor's `example-sites.js`; generic sitemap + JSON-LD. Live: ACEVO, Homeless Link.
* **BrassRing** — `jobindex/connectors/brassring.py`: the server-rendered preload page
  (first 50 jobs + the true `JobsCount`) and the job-detail preload; sites with more than 50
  jobs fail with an explicit error (no partial list). Live: Halifax Regional Municipality
  (36), Fairfax County Public Schools leadership site (17).
* **CSOD** — added to `blocked_by_site.yaml` (search API 401 without the page token).
* **talentReef** — added to `blocked_by_site.yaml` (scope platform; owner decision: the
  internal Elasticsearch proxy is not used). Evidence entries may now carry a `policy`
  reason instead of a refusal (`jobindex/coverage.py`, `tests/test_coverage.py`).
* **CareerSiteCloud** — rechecked: vendor site 403 on every page incl. its blog; no
  customer board named anywhere (reason recorded in `platforms.yaml`).
* Every other not-built platform has its exact reason in `platforms.yaml` (table above).
* `HttpClient` now retries `httpx.DecodingError` (corrupt gzip bodies seen on Taleo);
  `GenericJsonLdConnector.is_closed_html` lets a platform recognise a closed page by markup.

**Third pass (2026-11-10): leads for the job-board vendors without a board**

* **jobboardsoftware.com (Exenzo)** — `jobindex/connectors/jobboardsoftware.py`: the vendor
  page names its customer boards; they run on a token-free public JSON API per tenant
  (`{tenant}.api.exenzo.com/api/public/v1`: configuration + paged job list with full
  texts, checked against `total`). Live: Greenjobs (230), Vacaturebijdeoverheid (38),
  Binnenlands Bestuur (140). Tests: `tests/connectors/test_jobboardsoftware.py`.
* **Artha** — leads checked: `artha.linkx` does not resolve (`.linkx` is not a TLD).
  arthajobboard.com answers again; the vendor demo board `demo.arthajobboard.com` and a
  customer board (`medskil`) exist, but job pages answer 403 (Cloudflare challenge) and the
  listing API needs a faked browser `Origin` header → `blocked_by_site.yaml`.
* **Talenetic** — britishmedicaljobs.com now redirects to an unrelated casino site;
  jobs.seattletimes.com is `Disallow: /` for us, and the Seattle Times employer site now runs
  on Recruitology; fantastic.jobs names no boards; demo.talenetic.com is 404.
* **Nodex, eJobBoard** — no public demo board (Nodex shows screenshots; demo.nodex.co.uk
  serves a self-signed certificate; ejobboard.com is parked). Reasons in `platforms.yaml`.

**Live database runs** (`make seed-demo`, `make crawl`; snapshot at the end of the audit)

| Platform | Active sources | Crawled OK | Employers (open jobs) | Open jobs | Last success (UTC) |
|---|---|---|---|---|---|
| adp | 2 | 2 | 1 | 10 | 2026-11-08 05:25 |
| applicantpro | 2 | 2 | 2 | 38 | 2026-11-08 05:24 |
| ashby | 2 | 2 | 2 | 201 | 2026-11-08 05:24 |
| bamboohr | 3 | 3 | 3 | 5 | 2026-11-08 05:25 |
| breezy | 2 | 2 | 2 | 866 | 2026-11-08 05:25 |
| comeet | 2 | 2 | 2 | 320 | 2026-11-08 05:24 |
| deel | 2 | 2 | 2 | 8 | 2026-11-08 05:24 |
| eightfold | 2 | 2 | 2 | 1325 | 2026-11-08 05:26 |
| gem | 2 | 2 | 2 | 81 | 2026-11-08 05:25 |
| gohire | 2 | 2 | 2 | 34 | 2026-11-08 05:25 |
| greenhouse | 3 | 3 | 3 | 420 | 2026-11-08 05:25 |
| hibob | 2 | 2 | 2 | 112 | 2026-11-08 05:24 |
| hirebridge | 2 | 2 | 2 | 915 | 2026-11-08 10:59 |
| hirehive | 2 | 2 | 2 | 47 | 2026-11-08 05:24 |
| hireology | 2 | 2 | 2 | 8 | 2026-11-08 05:25 |
| hiringthing | 2 | 2 | 2 | 53 | 2026-11-08 05:25 |
| icims | 3 | 3 | 99 | 1865 | 2026-11-08 05:25 |
| isolvedhire | 2 | 2 | 2 | 37 | 2026-11-08 05:24 |
| jazzhr | 4 | 4 | 4 | 64 | 2026-11-08 05:27 |
| jboard | 2 | 2 | 112 | 3609 | 2026-11-08 05:47 |
| jobboardio | 2 | 2 | 27 | 30 | 2026-11-08 05:25 |
| jobboardly | 2 | 2 | 397 | 1466 | 2026-11-08 05:25 |
| jobiqo | 2 | 1 | 523 | 1497 | 2026-11-08 10:14 |
| jobmount | 2 | 2 | 397 | 1926 | 2026-11-08 05:29 |
| jobvite | 2 | 2 | 2 | 314 | 2026-11-08 05:26 |
| kula | 2 | 2 | 2 | 16 | 2026-11-08 05:25 |
| lever | 2 | 2 | 2 | 390 | 2026-11-08 05:25 |
| madgex | 2 | 2 | 480 | 1725 | 2026-11-08 05:41 |
| manatal | 2 | 2 | 2 | 2476 | 2026-11-08 05:25 |
| niceboard | 2 | 2 | 104 | 926 | 2026-11-08 05:25 |
| oracle | 2 | 2 | 2 | 9949 | 2026-11-08 09:15 |
| pageup | 2 | 2 | 2 | 168 | 2026-11-08 05:25 |
| paylocity | 2 | 2 | 2 | 505 | 2026-11-08 05:25 |
| personio | 2 | 2 | 3 | 198 | 2026-11-08 09:13 |
| phenom | 2 | 2 | 2 | 117 | 2026-11-08 05:25 |
| pinpoint | 2 | 2 | 2 | 304 | 2026-11-08 05:25 |
| polymer | 2 | 2 | 2 | 10 | 2026-11-08 05:25 |
| recooty | 2 | 2 | 2 | 105 | 2026-11-08 05:25 |
| recruitee | 2 | 2 | 2 | 35 | 2026-11-08 05:25 |
| rippling | 2 | 2 | 2 | 341 | 2026-11-08 05:25 |
| rival | 2 | 2 | 2 | 239 | 2026-11-08 06:02 |
| smartjobboard | 2 | 2 | 639 | 1312 | 2026-11-08 05:25 |
| smartrecruiters | 3 | 2 | 2 | 725 | 2026-11-08 09:32 |
| successfactors | 3 | 3 | 3 | 690 | 2026-11-08 05:55 |
| teamtailor | 2 | 2 | 2 | 41 | 2026-11-08 05:25 |
| trakstar | 2 | 2 | 2 | 35 | 2026-11-08 05:25 |
| trinet | 2 | 2 | 2 | 35 | 2026-11-08 05:25 |
| werecruit | 2 | 2 | 61 | 579 | 2026-11-08 05:25 |
| workable | 2 | 2 | 2 | 16 | 2026-11-08 05:25 |
| workday | 3 | 3 | 3 | 6609 | 2026-11-08 09:32 |
| wpjobmanager | 2 | 2 | 3 | 406 | 2026-11-08 05:25 |
| **51 platforms** | 110 | 108 | | **43,203** | |

Not yet successful in the DB at the time of writing, with the reason:

* **Jobiqo / www.thejobnetwork.com** — first crawl still running: 4,725 job pages at the
  site's `Crawl-delay: 10` ≈ 13 h (checkpointed; the other Jobiqo board, lovelocaljobs.com,
  is still on its first crawl too). The job list of both boards was crawled successfully
  by `make coverage` (`docs/COVERAGE.md`: 1,498 and 4,725 jobs).
* **SmartRecruiters / BoschGroup** — Bosch opted out in robots.txt (`Disallow:
  /BoschGroup/*`); every crawl is a recorded failure, no job touched. The example was
  replaced by **Wise** (427 jobs). Source kept active as live evidence of the failure path.

Every example board of all 60 done platforms has a successful live run in
`docs/COVERAGE.md` (job count + timestamp, regenerated 2026-11-08 14:08–14:17 UTC; new:
Taleo/WSP 160 + Stantec, Paradox/FedEx 4,735 + Marriott 14,188, Avature/Bloomberg 323 + UCLA
Health, Zoho Recruit/Cause Capacity 6 + Zoho USA, JobBoard.com/ACEVO 15 + Homeless Link,
ADP/nou Systems 43; BrassRing/Halifax 36 + FCPS 17; rerun 2026-11-08 16:28–16:37 UTC: all
117 current example boards OK). The database table above is the earlier audit snapshot: the new
connectors were verified by `make coverage` and `make test-live`, not by a full DB crawl.

**Gap:** 18 of 78 platforms have no working connector (13 blocked, 5 not
built), FirstStage and JobKit have only one live board each, and BrassRing covers only sites with ≤ 50 jobs (14 of 21 sites checked are
bigger: their further pages need page-issued tokens). None of the 5 needs a headless browser: they need page-issued state (policy: not
used), a complete/countable listing the site does not
offer, or a live customer board to build against. Exact reasons: `notes` in
`coverage/platforms.yaml`.

---

## 2. Consistent job records — 🟡

**Evidence:** `make fill-rates` (`python -m jobindex.cli fill-rates --markdown`, new,
`jobindex/quality.py`, tests `tests/test_quality.py`) — share of **open** postings in the
database with each field, per platform. Bold = a required field with gaps.
`first_seen_at` / `last_checked_at` are NOT NULL columns set by the crawler
(`jobindex/models/job.py`), so they are always 100%. Salary, employment type, remote and
posting date are optional ("when provided"); connectors map them whenever the platform
publishes them (structured fields first, then `normalize.find_salary` /
`detect_remote_type` on the text).

| Platform | Open jobs | title | company | description | location | source_url | apply_url | salary | employment_type | remote | posted_at | first_seen_at | last_checked_at |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| adp | 10 | 100% | **30%** | 100% | 100% | 100% | 100% | 50% | 30% | 0% | 100% | 100% | 100% |
| applicantpro | 38 | 100% | 100% | 100% | 100% | 100% | 100% | 50% | 97% | 0% | 100% | 100% | 100% |
| ashby | 201 | 100% | 100% | 100% | 100% | 100% | 100% | 89% | 100% | 100% | 100% | 100% | 100% |
| bamboohr | 5 | 100% | 100% | 100% | 100% | 100% | 100% | 60% | 100% | 100% | 100% | 100% | 100% |
| breezy | 866 | 100% | 100% | 100% | 100% | 100% | 100% | 55% | 100% | 4% | 100% | 100% | 100% |
| comeet | 320 | 100% | 100% | 100% | 100% | 100% | 100% | 6% | 81% | 100% | 0% | 100% | 100% |
| deel | 8 | 100% | 100% | 100% | 100% | 100% | 100% | 12% | 100% | 25% | 100% | 100% | 100% |
| eightfold | 1325 | 100% | 100% | 100% | **99%** | 100% | 100% | 19% | 100% | 2% | 100% | 100% | 100% |
| gem | 81 | 100% | 100% | 100% | 100% | 100% | 100% | 2% | 100% | 100% | 100% | 100% | 100% |
| gohire | 34 | 100% | 100% | 100% | 100% | 100% | 100% | 85% | 100% | 65% | 100% | 100% | 100% |
| greenhouse | 420 | 100% | 100% | 100% | 100% | 100% | 100% | 56% | 0% | 55% | 100% | 100% | 100% |
| hibob | 112 | 100% | 100% | 100% | 100% | 100% | 100% | 2% | 82% | 100% | 100% | 100% | 100% |
| hirebridge | 915 | 100% | 100% | 100% | 100% | 100% | 100% | 5% | 83% | 3% | 0% | 100% | 100% |
| hirehive | 47 | 100% | 100% | 100% | 100% | 100% | 100% | 11% | 98% | 0% | 100% | 100% | 100% |
| hireology | 8 | 100% | 100% | 100% | 100% | 100% | 100% | 88% | 100% | 0% | 100% | 100% | 100% |
| hiringthing | 53 | 100% | 100% | 100% | 100% | 100% | 100% | 15% | 0% | 0% | 100% | 100% | 100% |
| icims | 1865 | 100% | 100% | 100% | 100% | 100% | 100% | 37% | 94% | 2% | 100% | 100% | 100% |
| isolvedhire | 37 | 100% | 100% | 100% | 100% | 100% | 100% | 11% | 100% | 0% | 100% | 100% | 100% |
| jazzhr | 64 | 100% | 100% | 100% | 100% | 100% | 100% | 53% | 100% | 34% | 0% | 100% | 100% |
| jboard | 3609 | 100% | 100% | 100% | **100%** | 100% | 100% | 56% | 89% | 1% | 100% | 100% | 100% |
| jobboardio | 30 | 100% | 100% | 100% | 100% | 100% | 100% | 60% | 100% | 0% | 100% | 100% | 100% |
| jobboardly | 1466 | 100% | 100% | 100% | 100% | 100% | 100% | 38% | 77% | 0% | 100% | 100% | 100% |
| jobiqo | 1497 | 100% | 100% | 100% | 100% | 100% | 100% | 71% | 100% | 0% | 100% | 100% | 100% |
| jobmount | 1926 | 100% | 100% | 100% | 100% | 100% | 100% | 48% | 99% | 1% | 98% | 100% | 100% |
| jobvite | 314 | 100% | 100% | 100% | 100% | 100% | 100% | 53% | 21% | 2% | 21% | 100% | 100% |
| kula | 16 | 100% | 100% | 100% | 100% | 100% | 100% | 38% | 100% | 38% | 100% | 100% | 100% |
| lever | 390 | 100% | 100% | 100% | 100% | 100% | 100% | 66% | 99% | 100% | 100% | 100% | 100% |
| madgex | 1725 | 100% | 100% | 100% | 100% | 100% | 100% | 18% | 92% | 3% | 100% | 100% | 100% |
| manatal | 2476 | 100% | 100% | **100%** | **90%** | 100% | 100% | 0% | 100% | 0% | 0% | 100% | 100% |
| niceboard | 926 | 100% | 100% | **100%** | **100%** | 100% | 100% | 57% | 91% | 12% | 100% | 100% | 100% |
| oracle | 9949 | 100% | 100% | 100% | 100% | 100% | 100% | 21% | 72% | 1% | 100% | 100% | 100% |
| pageup | 168 | 100% | 100% | 100% | 100% | 100% | 100% | 34% | 100% | 4% | 100% | 100% | 100% |
| paylocity | 505 | 100% | 100% | 100% | 100% | 100% | 100% | 16% | 40% | 13% | 100% | 100% | 100% |
| personio | 198 | 100% | 100% | **99%** | 100% | 100% | 100% | 28% | 100% | 1% | 100% | 100% | 100% |
| phenom | 117 | 100% | 100% | 100% | 100% | 100% | 100% | 1% | 100% | 2% | 100% | 100% | 100% |
| pinpoint | 304 | 100% | 100% | 100% | 100% | 100% | 100% | 31% | 100% | 100% | 0% | 100% | 100% |
| polymer | 10 | 100% | 100% | 100% | 100% | 100% | 100% | 40% | 100% | 100% | 100% | 100% | 100% |
| recooty | 105 | 100% | 100% | 100% | 100% | 100% | 100% | 8% | 90% | 44% | 100% | 100% | 100% |
| recruitee | 35 | 100% | 100% | 100% | 100% | 100% | 100% | 26% | 100% | 100% | 100% | 100% | 100% |
| rippling | 341 | 100% | 100% | 100% | 100% | 100% | 100% | 46% | 84% | 2% | 100% | 100% | 100% |
| rival | 239 | 100% | 100% | 100% | 100% | 100% | 100% | 5% | 100% | 0% | 100% | 100% | 100% |
| smartjobboard | 1312 | 100% | 100% | **100%** | **100%** | 100% | 100% | 49% | 23% | 3% | 100% | 100% | 100% |
| smartrecruiters | 725 | 100% | 100% | **100%** | 100% | 100% | 100% | 14% | 100% | 100% | 100% | 100% | 100% |
| successfactors | 690 | 100% | 100% | **99%** | **98%** | 100% | 100% | 1% | 0% | 0% | 50% | 100% | 100% |
| teamtailor | 41 | 100% | 100% | 100% | 100% | 100% | 100% | 20% | 0% | 44% | 100% | 100% | 100% |
| trakstar | 35 | 100% | 100% | 100% | 100% | 100% | 100% | 97% | 100% | 91% | 0% | 100% | 100% |
| trinet | 35 | 100% | 100% | 100% | 100% | 100% | 100% | 71% | 100% | 66% | 0% | 100% | 100% |
| werecruit | 579 | 100% | 100% | 100% | 100% | 100% | 100% | 1% | 100% | 0% | 100% | 100% | 100% |
| workable | 16 | 100% | 100% | 100% | 100% | 100% | 100% | 19% | 94% | 12% | 100% | 100% | 100% |
| workday | 6609 | 100% | 100% | 100% | **99%** | 100% | 100% | 25% | 100% | 9% | 100% | 100% | 100% |
| wpjobmanager | 406 | 100% | **97%** | 100% | **99%** | 100% | 100% | 0% | 97% | 0% | 100% | 100% | 100% |
| **all** | 43203 | 100% | 100% | 100% | 99% | 100% | 100% | 30% | 84% | 9% | 89% | 100% | 100% |

**What was fixed in this audit**

* **Company missing on 20 platforms** (Lever, Ashby, Gem, iCIMS, Jobvite, Teamtailor,
  Pinpoint, …: 0% before). Single-employer boards rely on `sources.company_name`, which
  `add-source` only set for 9 platforms. New declarative `company_name_sources` on
  `BaseConnector` (`og:site_name` or a `<title>` regex, `company_name_from_page`), set on 14
  connectors, and the crawler now fills `sources.company_name` when empty
  (`tests/test_crawler.py::test_missing_company_name_is_fetched_once`,
  `tests/test_company_name.py`). For platforms whose data never names the employer (HiBob,
  ADP, PageUp, Workday) `platforms.yaml` gives `example_companies` and `seed-demo` applies
  them; operators use `set-company`.
* **SuccessFactors**: 134 open NTT DATA postings had an empty title and no location
  (tenant template marks up only the description). New generic hook `adjust_page`: title
  from `og:title`, location from the job URL slug (`location_from_slug`); `detail_version`
  forces a one-time re-read of stored pages after such a parser change. Tests:
  `test_template_without_title_microdata`, `test_location_from_slug`,
  `test_detail_version_forces_one_refetch_of_stored_pages`.
* `apply_url` was already 100% on every platform (the posting page itself when the
  platform has no separate apply link).

**Remaining gaps** (open postings missing a required field; every one checked):

| Platform | Field | Postings | Why |
|---|---|---|---|
| ADP | company | 7 of 10 | The old US example (`cid=d4d64eb4…`, a Miami Beach restaurant) never names its employer: not in the API (no organization fields), not on the page (title "Recruitment"), not in the descriptions. **Fixed 2026-11-08:** the example is now nou Systems (`cid=9e26f425…`, 43 jobs, every description names the employer; `example_companies` sets it). The old source is still active in the audit database (no CLI command deactivates a source); `set-company` or removing it closes this gap there. |
| WP Job Manager | company | 11 of 406 | startjob.dk listings whose `_company_name` is empty in the WordPress data (multi-employer board, so no fallback). |
| Manatal | location | 239 of 2,476 | MR DIY store jobs with empty `city`/`state`/`country`/`location_display` in the API (the store is only in the title). |
| Workday | location | 58 of 6,609 | PwC requisitions whose detail endpoint returns `location: ""` (checked live). |
| Eightfold, SuccessFactors, JBoard, Niceboard, SmartJobBoard, WP Job Manager | location | 15 / 12 / 2 / 1 / 2 / 4 | No location in the JobPosting nor on the page. |
| SuccessFactors, Manatal, SmartJobBoard, Niceboard, Personio, SmartRecruiters | description | 4 / 2 / 2 / 1 / 1 / 1 | Empty description at the source. |

Also fixed: **HireBridge** location was 5% (Menard leaves the list's location column empty):
taken from the job page's `_jobloc` (`test_location_from_job_page_when_the_list_has_none`);
**JobMount** regions "Texas - TX" broke the `location=Austin, TX` filter: now "TX".

---

## 3. Automatic updates — 🟡

| Requirement | Evidence |
|---|---|
| Scheduled refresh | `python -m jobindex.cli schedule` = the `worker` service in `docker-compose.prod.yml` (healthcheck on the scheduler heartbeat). APScheduler tick every 60 s; per-platform intervals (`CRAWL_PLATFORM_INTERVALS`), jitter, failure backoff, concurrency limit, skip-if-running (`jobindex/scheduler/__init__.py`). Tests: `tests/test_scheduler.py` (`test_tick_crawls_due_sources_only`, `test_next_due_interval_jitter_and_backoff`, `test_concurrency_limit`, `test_abandoned_runs_are_recovered_as_failed`). |
| Hosted demo schedule | `.github/workflows/crawl.yml`: cron `17 */6 * * *`, 4 shards, never two runs at once. **Gap:** three runs so far (2026-11-10 17:03, 18:39, 18:55 UTC), all started by hand; the first scheduled slot after the repository went public (18:17 UTC) produced no run. GitHub runs schedules best-effort only. Check the next one at Actions → crawl (event `schedule`) after 2026-11-11 00:17 UTC. |
| Change detection | `content_hash` over `normalize.HASH_FIELDS`; one `job_changes` row per changed field with `cause` = upstream / parser / local. Live (second crawl pass, 86 sources): 1 new posting (Gem/Nominal, job 15609 "Engagement Manager"), 1 upstream change, 5,150 `local` changes (company names filled in by this audit). Third pass on 8 large sources: NVIDIA Workday 2 new / 4 closed, Oracle (eeho) 2 closed. Tests: `test_recrawl_tracks_changes_closes_and_reopens`, `test_change_causes_upstream_parser_local`, `test_cosmetic_changes_update_silently`. |
| Closed listings marked | Live: job 8514 "Land Survey Technician" (jobnet.wacities.org) open after pass 1, `status open → closed` (`job_changes`, cause upstream) in pass 2 when the successful fetch no longer listed it. Generic connectors also close on `validThrough`, closed notices, 404/410 (190 Eightfold postings stored closed on first crawl). |
| Failed request ≠ closure | Live: Lever/Spotify crawled through an unreachable proxy → `fetch_runs` 174 `failed`, `sources.consecutive_failures` 0 → 1, **85 open / 0 closed before and after**, `last_checked_at` unchanged; the next normal crawl (run 175) succeeded and reset the counter. Same for the real failures seen in this audit (THE HTTP 502, Bosch robots opt-out). Tests: `test_failed_fetch_never_closes_jobs`, `test_failed_scheduled_crawl_keeps_jobs_open`, `test_database_error_is_recorded_as_failure`, `test_cancelled_crawl_is_recorded_as_failure`, job-drop guard tests in `tests/connectors/test_generic_jsonld.py`; `fetch_jobs` contract "complete or raise" tested per connector (missing page → raises). |

---

## 4. Deduplication — ✅

**Evidence:** `make dedup-report` (new, `scripts/dedup_report.py`) and
`GET /jobs` (one result per opening, `sources[]` lists every listing).

Real duplicates found in the DB (all checked by hand; each opening is returned once by
the API with both links). To have overlap at all, five employer boards that Science
Careers / Nonprofit Talent / EdSurge postings link to were added as sources (106–110);
the other merges are between the job boards themselves (Science Careers, THE,
IMDiversity, ALA) and Oracle:

| Opening | Listings (platform / board) | Company | Title | City | Job ids |
|---|---|---|---|---|---|
| 6681 | niceboard (jobs.nonprofittalent.com) + jazzhr (nonprofittalent) | Nonprofit Talent | Chief Executive Officer | Pittsburgh | 6681, 18205 |
| 7241 | smartjobboard (jobsboard.edsurge.com) + bamboohr (ceporg) | Center for Effective Philanthropy | Analyst – YouthTruth | San Francisco | 7241, 12650 |
| 12135 | madgex (jobs.sciencecareers.org/jobs) + jobmount (jobs.imdiversity.com) | University of Massachusetts Amherst | Assistant Professor - Quantum Experimentalist | Amherst | 12135, 18284 |
| 12186 | madgex (jobs.sciencecareers.org/jobs) + madgex (www.timeshighereducation.com/unijobs/listings) | THE PIRBRIGHT INSTITUTE | Director & Chief Executive Officer | Surrey, GB | 12186, 23772 |
| 12252 | madgex (jobs.sciencecareers.org/jobs) + workday (umd.wd1.myworkdayjobs.com/umd/UMCES) | University of MD Center For Environmental Science | Assistant Professor-Microbiome and Symbiosis | UMCES Institute of Marine and Environmental Technology | 12252, 12652 |
| 12261 | madgex (jobs.sciencecareers.org/jobs) + jobmount (jobs.imdiversity.com) | Massachusetts Institute of Technology | Assistant Professor, Tenure Track | Cambridge | 12261, 18595 |
| 12340 | madgex (jobs.sciencecareers.org/jobs) + madgex (www.timeshighereducation.com/unijobs/listings) | VIN UNIVERSITY | [AY26-27] Faculty of Economics | VN | 12340, 23426 |
| 12341 | madgex (jobs.sciencecareers.org/jobs) + madgex (www.timeshighereducation.com/unijobs/listings) | VIN UNIVERSITY | [AY26-27] Faculty of Psychology | VN | 12341, 23427 |
| 12343 | madgex (jobs.sciencecareers.org/jobs) + jobmount (jobs.imdiversity.com) | University of Massachusetts Amherst | Riccio Endowed Chair - Biomedical Engineering | Amherst | 12343, 18819 |
| 12378 | madgex (jobs.sciencecareers.org/jobs) + madgex (www.timeshighereducation.com/unijobs/listings) | VIN UNIVERSITY | [AY26 - 27] Associate Professor/ Full Professor in New Media and Digit | VN | 12378, 23428 |
| 12379 | madgex (jobs.sciencecareers.org/jobs) + madgex (www.timeshighereducation.com/unijobs/listings) | VIN UNIVERSITY | [AY26 - 27] Associate Professor/ Full Professor in Computational Commu | VN | 12379, 23425 |
| 12624 | madgex (jobs.sciencecareers.org/jobs) + icims (faculty-childrensmercykc.icims.com) | Children's Mercy KC | Doctoral Research Faculty - Molecular Oncology and Cellular Therapeuti | Kansas City | 12624, 15536 |
| 12625 | madgex (jobs.sciencecareers.org/jobs) + icims (faculty-childrensmercykc.icims.com) | Children's Mercy KC | Senior Doctoral Research Faculty & Endowed Chair - Molecular Oncology  | Kansas City | 12625, 15537 |
| 15118 | smartjobboard (alanet.mysmartjobboard.com) + oracle (jpmc.fa.oraclecloud.com/CX_1001) | JPMorgan Chase & Co. | Senior Counsel - Control Functions Legal - Assistant Vice President | Plano | 15118, 40663 |
| 15322 | smartjobboard (alanet.mysmartjobboard.com) + oracle (jpmc.fa.oraclecloud.com/CX_1001) | JPMorgan Chase & Co. | U.S. Private Bank - Private Banker - VP/ED - Law Firm Group | New York | 15322, 35740 |
| 18541 | jobmount (jobs.imdiversity.com) + madgex (www.timeshighereducation.com/unijobs/listings) | NEW YORK UNIVERSITY | Clinical Assistant Professor of Irish Language, Literature, and Cultur | New York City | 18541, 23421 |

Rules that produced them: shared URL / posting key (none here: job boards link to their
own pages), **description link** (UMCES: Science Careers links to Workday JR105179 —
different title, city and company spelling), and **fuzzy** (company + title + city + date +
description overlap ≥ 0.6) for the rest; e.g. JPMorgan's Oracle requisition (posted
2026-08-07, still open) re-listed by the ALA board on 2026-11-02. Description overlap of
every merged pair: 0.78–1.0 (`scripts/dedup_report.py` data, 2026-11-08).

False-merge checks (from the same report, on the same data):

* **Near misses across sources** (same company + title keys, different sources, kept
  apart — all correct):
  * *East West Bank* "Credit Analyst" (iCIMS, New York / Houston, US) vs *EastWest Bank*
    (Manatal, Taguig, PH): two different banks whose names normalise alike — kept apart by
    location.
  * *Columbia University* "Lecturer in Discipline" (Science Careers: Psychology vs
    IMDiversity: Center for the Study of Ethnicity and Race) and *University of Virginia*
    "Assistant or Associate Professor of Cell Biology" (Academic Investigator track vs
    tenure line): same title, city and period, **different jobs**. Both were merged by an
    intermediate version of this audit's rules and are now kept apart by the description
    check (overlap 0.49 and 0.32 < 0.6; test
    `test_same_generic_title_with_different_descriptions_stays_separate`).
  * *University of Waterloo* teaching-stream posting: IMDiversity writes "Waterloo,
    Ontario, United States" (country US), Science Careers says CA — kept apart by the
    country conflict (conservative; the source data is wrong).
* **Same company + title on one source — kept separate:** 2,924 titles / 13,506 postings,
  all 2,924 kept as separate openings (e.g. AdaptHealth "Medical Equipment Delivery Driver"
  in Oak Forest, Knoxville, Kingsport, Athens; Déclic Immo "Conseiller Immobilier" per
  town).
* **Same title at different employers — kept separate:** 667 titles (e.g. "legal
  assistant" at 177 employers, "project manager" at 30).
* **Round-up ad not merged:** Science Careers' single "summer teaching opportunity" ad by
  SSP International links to several of SSP's JazzHR postings; it is kept apart from all
  of them (rule: a link counts only when it points at exactly one posting of a source).

**What was fixed in this audit**

* Dedup found **0** duplicates on the first real data: job boards re-list ATS postings under a
  different URL shape (Workday `/details/` vs `/job/{location}/`) or only link to them in
  the description. New `posting_keys` (`jobindex/dedup/keys.py`: Workday, Lever,
  Greenhouse, JazzHR, iCIMS) and a new strong rule "the description links to exactly one
  posting of another source" (`jobindex/dedup/cluster.py`; URLs extracted in Postgres,
  descriptions are not loaded). Tests: `test_posting_keys_match_url_variants_of_one_posting`,
  `test_posting_keys_keep_different_postings_apart`,
  `test_description_link_to_another_sources_posting_merges` (the real UMCES case),
  `test_description_linking_several_postings_of_a_source_proves_nothing`,
  `test_run_dedup_follows_links_in_descriptions`; existing rule tests in
  `tests/test_dedup.py` (different city, remote rules, same-source never merged, re-post
  months later, shared generic apply URL).
* Missed duplicates found by the near-miss report: "New York" vs "New York City" (city
  aliases in `keys.city_key`) and a job board re-listing a still-open requisition months
  later (`dates_compatible`: the earlier listing still live counts as the same period).
  Tests: `test_new_york_city_is_new_york`,
  `test_board_relisting_a_still_open_requisition_months_later_merges`. Generic academic
  titles then produced 2 false merges, fixed by the description-overlap check
  (`MIN_DESCRIPTION_OVERLAP`, descriptions loaded only for fuzzy candidates).

---

## 5. Searchable output — ✅

| Requirement | Evidence |
|---|---|
| Search UI | `GET /` (server-rendered, `jobindex/api/ui.py`, `templates/index.html`): keyword, company, location, remote, employment type, minimum salary, posted since, platform, status; pagination; one card per opening with every source link; header with live totals (open jobs, platforms, last crawl); optional "Dark Fantasy" theme (header toggle, remembered in `localStorage`; pure CSS, no build step, all text WCAG AA, works at 375 px). Live: `/?q=engineer&location=Berlin` → "9 openings". Tests: `test_search_page`, `test_search_page_drops_unsafe_links`, `test_search_page_header_totals_and_platform_filter`, `test_search_page_themes`. |
| API with pagination | `GET /jobs?page=&size=` (max 100; `size=500` → 422), response `total` / `page` / `size` / `pages` / `items`. Live: `q=engineer` → 3,038 openings, 1,519 pages at size 2. Test: `test_pagination`. |
| Keyword filter | `q` (Postgres `websearch_to_tsquery` over a weighted `tsvector`: title > company > description, GIN index). Live: `q=engineer`. |
| Company filter | `company` (case-insensitive contains). Live: `company=spotify&page=2&size=3` → 86 total, page 2. |
| Location filter | `location` (city / region / country name or ISO code, comma-combined). Live: `location=Berlin, Germany` → 16 openings, all in Berlin, DE (city or region); `location=Austin, TX` → 28. |
| More filters | `remote_type`, `employment_type`, `salary_min`, `salary_currency`, `posted_since`, `status`, `platform`, `source_id`. Live: `q=python&location=US&remote_type=remote` → 48. Test: `test_search_filters`. |
| OpenAPI documents every parameter with examples | `/docs`, `/openapi.json`: all 16 parameters of `/jobs`, `/jobs/{job_id}`, `/sources` have a description and an example (fixed in this audit: 9 had neither). Test: `tests/test_api.py::test_openapi_is_documented` now asserts description + example for every parameter of every operation. |
| One result per opening | Live: `/jobs/5630` "Supported Housing Support Worker" (YMCA DownsLink, Horsham) → 1 opening whose `sources` are `jobboardcom` (jobs.homeless.org.uk) and `jobiqo` (lovelocaljobs.com). `/stats`: 7,314 open postings, 7,307 open openings. (Local full crawl: 43,203 postings → 43,187 openings.) |
| Other endpoints | `/jobs/{id}` (any listing's id resolves to its opening), `/sources?failing=true`, `/stats` (per platform counts, last crawl, latest 20 failures), `/health`. |

---

## 6. Complete handoff — ✅

| Item | Where |
|---|---|
| Source code | This repository; layout table in [`README.md`](../README.md#layout); `make lint` (ruff, ruff format, mypy strict) and `make test` (offline, fixtures) pass. |
| Deployment instructions | [`docs/DEPLOY.md`](DEPLOY.md): one VPS, `docker-compose.prod.yml` (app, worker, Postgres, Caddy HTTPS, nightly backups), update, backup/restore, health checks, local smoke test. Quick start in `README.md`. |
| Connector docs | [`docs/CONNECTORS.md`](CONNECTORS.md): contract, every connector's endpoint, generic sitemap/JSON-LD machinery and hooks, adding a connector (step by step), dedup rules; each connector module's docstring documents its endpoints, limits and verification date. |
| Operating costs | [`docs/OPERATIONS.md`](OPERATIONS.md#capacity-and-monthly-cost): ≈ €10–35/month for ~1,000 boards (VPS, backups, domain), ≈ €30–120 at 10,000 boards; no paid APIs. |
| External dependencies | [`docs/OPERATIONS.md`](OPERATIONS.md#external-dependencies): the crawled platforms themselves (and their robots.txt), Docker images, PyPI packages (`pyproject.toml`), Let's Encrypt; no queues/caches/cloud services. |
| Operations | `docs/OPERATIONS.md`: add a source, `seed-demo`, re-run, monitoring (`/stats`, `/sources?failing=true`, `fill-rates`), tuning, maintenance. |

---

## How to reproduce

```sh
make up && make migrate
make seed-demo                 # ~105 sources from coverage/platforms.yaml
make crawl                     # first pass: 2–3 h (big boards take longer, Crawl-delay 10 hosts ~hours)
make crawl                     # second pass: change detection, closures
make fill-rates ARGS=--markdown
make dedup-report
open http://localhost:8000/    # search UI;  http://localhost:8000/docs  for the API
make lint && make test
```
