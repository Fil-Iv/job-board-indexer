# Connectors

A connector turns one platform's public job data into `RawJob`s. There are 49 today
(29 API/feed/HTML connectors + 20 on the generic sitemap + JSON-LD base);
[COVERAGE.md](COVERAGE.md) lists all 78 target platforms, live job counts for two real
employers per done platform, and the plan for the remaining 29.

Every connector module starts with a docstring giving its exact endpoints, response shape,
URL formats it recognises and the quirks found while building it. That docstring is the
reference; this page is the overview.

## The contract

`jobindex/connectors/base.py`:

```python
@register
class AcmeConnector(BaseConnector):
    platform = "acme"  # sources.platform
    html_fingerprints = (re.compile(...),)  # optional: spot the platform on custom domains
    needs_detail = False  # True if the list lacks descriptions

    @classmethod
    def detect(cls, url: str) -> str | None: ...  # URL -> board identifier
    @classmethod
    def board_url(cls, board_identifier: str) -> str: ...
    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]: ...
    async def fetch_job_detail(self, board_identifier, job) -> RawJob | None: ...  # if needs_detail
    async def fetch_company_name(self, board_identifier) -> str | None: ...  # optional
    def max_job_drop(self) -> float | None: ...  # optional guard
```

Rules (from [CLAUDE.md](../CLAUDE.md)):

1. **`fetch_jobs()` returns the complete list of open postings, or raises.** Never a
   partial list: if page 3 of 5 fails, raise. A missing posting is read as "closed".
2. **All HTTP goes through `self.http`** (`jobindex.http.HttpClient`): User-Agent,
   robots.txt, 1 request/second per host, timeouts, retries with backoff. Never create an
   `httpx` client.
3. Prefer official/public JSON → HTML with `selectolax` → headless browser (last resort,
   needs justification; none use one today).
4. Raise `ConnectorError` for responses you cannot understand; `FetchError` comes from the
   client. Both become a failed fetch run; **no job is touched**.
5. Fill what the platform gives in `RawJob` (title, company, location parts, remote type,
   employment type, salary, dates, URLs). `RawJob.normalized()` derives the rest
   (location parsing, country codes, salary from text, HTML → text) in
   `jobindex/normalize.py`.

`fetch_job_detail()` is only called for **new or changed** postings: the crawler compares
`detail_cache_key` fields (or `list_version`, e.g. a sitemap `<lastmod>`) with the stored
job and reuses stored details otherwise. Return `None` when the detail is gone (posting
closed). Long fetches can save progress through `self.checkpoint`
(`jobindex/checkpoint.py`) so a failed crawl resumes.

## How each connector works

### API, feed and HTML connectors

| Platform | Module | Board identifier | Requests per crawl | Notes |
|---|---|---|---|---|
| ADP Workforce Now | `adp` | `cid` (+ `:lang` outside `en_US`) | 1 per 100 jobs + detail per new/changed | Career-center JSON; `$skip` is the 1-based start, count checked against `totalNumber` |
| Ashby | `ashby` | job-board name | 1 | Posting API with `includeCompensation=true` |
| BambooHR | `bamboohr` | subdomain | 1 list + detail per new/changed job | `/careers/list`, `/careers/{id}/detail` (404 = closed) |
| Breezy HR | `breezy` | subdomain | 1 | `/json?verbose=true` (descriptions only with `verbose`) |
| CareerPlug | `careerplug` | account host (`fastsigns-careers.careerplug.com`, white-label `*.otrecruit.com` etc.) | 1 per 30 jobs (×2 when > 1 page) | **List only, no descriptions**: server-rendered `/jobs?page=N`; job pages redirect into the robots-disallowed `/apps/` and are never fetched. No total count, so multi-page boards are read twice and must match |
| Comeet | `comeet` | `{slug}/{uid}` (`safefleet/95.001`) | 1 | Board page embeds `COMPANY_POSITIONS_DATA` (all positions + descriptions); the token API is not used |
| Freshteam | `freshteam` | subdomain | 1 + detail per new/changed | Server-rendered `/jobs` list (robots.txt allows only `/jobs`), distinct job links checked against the "#N Jobs" count in `og:description`; description and company from the job page; unknown tenants' 200 "We couldn't find" page is an error |
| Gem | `gem` | slug | 1 | `api.gem.com/job_board/v0/{slug}/job_posts/` |
| Greenhouse | `greenhouse` | board token | 1 | `content=true&pay_transparency=true`: descriptions + pay ranges |
| HiBob | `hibob` | subdomain | 1 | `/api/job-ad` with header `companyIdentifier: {subdomain}` (401 without it) |
| HireBridge | `hirebridge` | cid | 1 + detail per new/changed | ASP.NET list, links per location grouped by `jid`; detail elements by id suffix; job-drop guard |
| HireHive | `hirehive` | subdomain | 1 | `/api/v1/jobs`; `nextPage` (take/skip) followed, `publishedJobsCount` checked; dates in Unix seconds |
| JazzHR | `jazzhr` | subdomain | 1 + detail per new/changed | `/apply` HTML list (no count: job-drop guard) + `#job-description` from the job page |
| Jobvite | `jobvite` | company slug | 1 per 50 jobs + detail per new/changed | Paged `/search/?p=` HTML checked against "1-50 of N" (`/jobs` is incomplete); description from page HTML, JSON-LD only on some tenants |
| Lever | `lever` | site (`eu:site` for EU) | 1 per 100 jobs | `skip`/`limit` pagination; EU instance separate |
| Manatal | `manatal` | slug | 1 per 100 jobs | `ordering=created_at` required (default order skips jobs) |
| Oracle Recruiting | `oracle` | `host/siteNumber` (`eeho.fa.us2.oraclecloud.com/CX_45001`) | 1 per 200 jobs + detail per new/changed | Candidate Experience REST API |
| Paylocity | `paylocity` | module guid | 1 + detail per new/changed | `window.pageData.Jobs` list; description/type/remote label from the job page HTML (JSON-LD only on some tenants) |
| PageUp | `pageup` | `{id}/{portal}/{lang}` (`851/ci/en-us`) | 1 | Portal RSS (`job:` namespace, defusedxml) with descriptions; complete vs. the listing |
| Personio | `personio` | subdomain | 1 | XML feed (parsed with `defusedxml`); never pass `?language=` |
| Pinpoint | `pinpoint` | subdomain | 1 | `postings.json`; robots.txt differs per tenant |
| Polymer | `polymer` | slug | 1 per page + detail per new/changed | `meta.count` = jobs, `meta.total` = pages; count must match |
| Recruitee | `recruitee` | subdomain | 1 | `/api/offers/` (not served on custom domains) |
| Rippling | `rippling` | slug | 1 list + detail per new/changed | One list row per job *location*; grouped into one posting |
| SmartRecruiters | `smartrecruiters` | company | page 0 + `api/more` pages (~24 jobs each) | API is refused by robots.txt, so the public career pages are used (API path behind `SMARTRECRUITERS_USE_API`, only with permission) |
| Teamtailor | `teamtailor` | career-site host | 1 (+ HTML cross-check on big feeds) | `/jobs.rss`; works on custom domains |
| TriNet Hire | `trinet` | company id | 1 + detail per new/changed | `table.job-list` + `div.job-descr`/`ul.job-meta`; job-drop guard |
| Trakstar Hire | `trakstar` | account | 1 per 100 jobs | `jsapi.recruiterbox.com` (former Recruiterbox); no posting date |
| Workable | `workable` | account | 1 | Widget API with `details=true` |
| Workday | `workday` | `host/tenant/site` | 1 per 20 jobs + detail per new/changed | CXS API; 2,000-result cap split by facets; **resumable** (checkpoint) |
| WP Job Manager | `wpjobmanager` | WordPress site root (`startjob.dk`, `example.org/careers`) | 1 types + 1 per 100 jobs | WordPress REST `wp/v2/job-listings`; any domain, recognised from page HTML only; sites with REST disabled fail |

### Generic sitemap + JSON-LD connectors

Platforms without a usable API publish every job page in a sitemap (for search engines)
and describe each page with schema.org `JobPosting`. `GenericJsonLdConnector`
(`jobindex/connectors/generic_jsonld.py`) does the work; each platform is a 25–50-line
subclass saying where the sitemap is, which URLs are job pages, and how to recognise the
platform on custom domains.

1. `fetch_jobs` reads the sitemap(s) (following index files) and keeps job URLs, with
   `<lastmod>` as `list_version`.
2. The crawler requests a job page only when it is new or its `<lastmod>` changed (no
   `<lastmod>`: re-fetched every `GENERIC_REFRESH_DAYS`, staggered).
3. The page's `JobPosting` (JSON-LD or microdata, `jobindex/schemaorg.py`) becomes a
   `RawJob`. `validThrough` in the past, a "closed/expired" page, or 404/410 → stored
   **closed** and not re-fetched until `<lastmod>` changes.
4. Safety: more than `GENERIC_MAX_JOB_DROP` (50%) fewer open jobs than the last
   successful crawl fails the run instead of closing them; progress is checkpointed every
   100 pages so a failed first crawl of a big board resumes.
5. Gaps: `adjust(job, item)` fixes the mapped JobPosting, `adjust_page(job, html)` fills
   fields the JobPosting omits from the rest of the page (SuccessFactors: title from
   `og:title`, location from the job URL slug), and `parse_page_html(html, stub)` maps an
   *open* job page that has no JobPosting at all (GoHire and Rival drop it for older
   postings that are still listed). Pages that are neither are counted as "unexplained";
   more than 20% of them fails the crawl (the template probably changed).

| Platform | Module | Job list | Board identifier |
|---|---|---|---|
| ApplicantPro, iSolved Hire | `applicantpro`, `isolvedhire` | `{tenant}.{domain}/job_site_map.xml` | tenant |
| JOIN.com | `join` | paged company page `join.com/companies/{company}?page={n}` (`__NEXT_DATA__`, 5/page) checked against `total` (overrides `fetch_jobs`; external id = job `id`, not the ad number in the URL; > 100 jobs refused: the site serves 20 pages) | company slug |
| GoHire | `gohire` | paged board `jobs.gohire.io/{board}/?page={n}` checked against "Total N" (overrides `listed_pages`; `validThrough` ignored) | board slug |
| HiringThing | `hiringthing` | `{company}.hiringthing.com/api/rss.xml` (RSS feed; no per-tenant sitemap) | host |
| Deel, Kula | `deel`, `kula` | `jobs.deel.com/{org}/sitemap.xml`, `careers.kula.ai/{company}/sitemap.xml` | slug |
| JobBoard.io | `jobboardio` | `{board}/sitemap.xml` -> `/sitemaps/jobs/` chunks | host (custom domains) |
| Rival (SilkRoad) | `rival` | paged board `?page={n}` (overrides `listed_pages`; Crawl-delay 10) | `company/portal` |
| Wiley Madgex | `madgex` | paged listing `{board}/{n}/` checked against "Found N jobs" (overrides `listed_pages`) | listing URL (`jobs.sciencecareers.org/jobs`) |
| Eightfold AI | `eightfold` | `{site}/careers/sitemap_index.xml?domain=` (from robots.txt); the JSON API answers 403 | host (`{tenant}.eightfold.ai` or custom domain) |
| Hireology | `hireology` | `careers.hireology.com/sitemap_index.xml` (all tenants, ~8 MB) | slug |
| iCIMS | `icims` | `{host}/sitemap.xml` | host |
| Jobboardly, JBoard, Niceboard, SmartJobBoard | `jobboardly`, `jboard`, `niceboard`, `smartjobboard` | `{board}/sitemap.xml` | host |
| Jobiqo, JobMount | `jobiqo`, `jobmount` | `{board}/sitemap.xml` | host (custom domains only) |
| ejobsitesoftware.com | `ejobsitesoftware` | `{board}/rss/job.php` (overrides `listed_pages`; capped at 200 items: a full feed fails the crawl); a job page answering 5xx keeps the feed's fields | host (custom domains only) |
| Dayforce | `dayforce` | page rendered with `jobindex.browser`; the `jobposting/search` responses the page receives are read page by page (pager clicks) until `maxCount`; the API itself answers 403 outside the page and is never called directly | `namespace/site`; **browser**; no detail requests |
| jobBoardHQ | `jobboardhq` | `{board}/search` rendered with `jobindex.browser`: "100 Rows", then the pager's in-place postbacks until the last page the pager shows (overrides `listed_pages`; full pages, no repeats, `Only the top 1000` boards refused); job pages over plain HTTP | host (custom domains only); **browser** |
| Nodex | `nodex` | `{board}/sitemap.xml` (`/find-jobs?*` is robots-disallowed); title columns "Title \| Location \| Salary \| Type" cut to the title | host (custom domains only) |
| CareerSiteCloud | `careersitecloud` | paged list `{site}/{view}[/{n}]` or `/search?qs_search_job=page%3d{n}`, pages requested by number up to the stated total (overrides `listed_pages`); a job on two pages fails the crawl (the site's order is not stable) | `host[/view]`; partial (one customer site) |
| Phenom | `phenom` | `{site}/{cc}/{lang}/sitemap.xml` | `host/cc/lang` |
| Recooty, WeRecruit | `recooty`, `werecruit` | one sitemap for all tenants | slug |
| SuccessFactors (RMK) | `successfactors` | `{site}/sitemap.xml` | host |

### Browser-rendered connectors

A few platforms only show their job list to a page running JavaScript. Their connectors set
`uses_browser = True` and render that page with `jobindex.browser.PoliteBrowser` (Playwright,
optional extra `browser`; approved by the project owner 2026-11-11 for robots-allowed pages
only). The browser is held to the same rules as `HttpClient`, whose robots.txt cache and
per-host throttle it shares:

* only the board's own hosts and the assets it needs are contacted (analytics, ads, fonts
  and images are not requested); every request waits for its host's turn (1/s or
  `Crawl-delay`) and is checked against its own host's robots.txt;
* a disallowed request is not sent and **fails the crawl** (the list would be incomplete);
  so do HTTP 401/403/429 and challenge/CAPTCHA pages. They are never solved;
* nothing is read out of the page to be sent elsewhere (no token replay); the connector
  reads what the page renders or the job-list responses the page itself received;
* User-Agent `Mozilla/5.0 (compatible; JobIndexBot/0.1; +url)`: still the bot's name, in the
  form large crawlers use (ASP.NET sites send their pager script only to such agents).

Job detail pages are fetched over plain HTTP where they allow it (jobBoardHQ: JSON-LD).

### URL resolution

`registry.resolve_url(url)` (used by `add-source`) first asks every connector's `detect()`
(hosted board, job, embed and API URLs). If none matches, it downloads the page (obeying
robots.txt) and looks for `html_fingerprints` and embedded board URLs, so custom-domain
careers pages such as `www.figma.com/careers/` resolve to their ATS board.

## Adding a connector

1. **Research.** Find the platform's public endpoint (official API → JSON behind the
   hosted board → sitemap + JSON-LD). Check the API host's `robots.txt`
   (`scripts/check_access.py URL`). If the site refuses us, record it in
   `coverage/blocked_by_site.yaml` instead of working around it.
2. **Sitemap platform?** Subclass `GenericJsonLdConnector`; copy `jobindex/connectors/niceboard.py`
   (host-based) or `recooty.py` (shared sitemap). JSON-LD job pages but no usable sitemap:
   override `listed_pages` with the platform's own listing (`gohire.py`). `scripts/evaluate_generic.py` checks a
   board's sitemap and pages before you write any code.
3. **API platform?** Create `jobindex/connectors/<platform>.py`, decorate the class with
   `@register`, implement the contract above. `gem.py` (one request) and `bamboohr.py`
   (list + detail) are good templates. Paginate until the reported total is reached and
   raise if pages are missing or the count does not add up.
4. **Fixture.** Save a real response:
   `docker compose run --rm app python scripts/capture_fixture.py URL tests/fixtures/<platform>/<name>`
   (goes through the polite client). Trim it to a few postings; keep the odd cases.
5. **Test.** `tests/connectors/test_<platform>.py`: a URL-detection table, a fixture test
   via `tests.fakes.mock_http` (no network), a failure test (an error page or a missing
   page must raise, not return fewer jobs) and a `@pytest.mark.live` test against two
   real employers. Note the fixture's URL and capture date in the test docstring.
6. **Coverage.** Mark the platform `done` in `coverage/platforms.yaml` (module + two
   example boards) and run `make coverage` (live crawl, regenerates `COVERAGE.md`).
7. **Company name.** If postings carry no employer (single-employer ATS boards), set
   `company_name_sources` on the class: `"og:site_name"` and/or a regex on the board
   page's `<title>` with a `name` group (`lever.py`, `ashby.py`). `add-source` and the
   crawler (when `sources.company_name` is empty) use it; `make fill-rates` shows gaps.
8. `make lint && make test`. If you changed `jobindex/normalize.py` behaviour, bump
   `normalize.PARSER_VERSION` and run `make parser-fingerprint`.

## Deduplication

The same opening often appears on several sources (an employer's ATS board and job boards
that re-list it). `jobindex/dedup/` groups them after every crawl and sets
`jobs.canonical_job_id` on every posting (pointing at itself when it has no duplicate).

Two postings are the same opening when they are on **different sources** and:

* **strong signal**: they share an apply URL or source URL (ignoring scheme, `www.`,
  trailing slash and tracking parameters such as `utm_*`, `gh_src`; a URL one source
  uses for several postings — a generic "apply here" page — is ignored); or
* **fuzzy match**: same normalised company (`"Acme GmbH"` = `"ACME"`), same normalised
  title (`"Sr. Engineer (m/w/d)"` = `"Senior Engineer"`), compatible location (same city;
  or both remote with the same or an unspecified country; a city never matches another
  city or "remote") and close in time (posting dates within `DEDUP_DATE_WINDOW_DAYS`, or —
  without dates — overlapping periods in which we saw them).

Postings from one source are never merged (two postings on one board are two openings).
The canonical posting is an open one, the earliest seen. The API returns one result per
group, with every source's link; filters match if any posting of the group matches.
Tests with realistic cases: `tests/test_dedup.py`.
