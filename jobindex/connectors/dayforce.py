"""Dayforce (Ceridian) career sites: the public job board rendered in a headless browser.

Board: ``jobs.dayforcehcm.com/{locale}/{clientNamespace}/{careerSiteXRefCode}`` (a Next.js
app; robots.txt has no rules, only content-signal comments: everything allowed).
List:  the page itself calls ``POST /api/geo/{namespace}/jobposting/search`` with its own
       session (NextAuth ``/api/auth/session`` + ``/api/auth/csrf``) and renders 25 jobs a
       page. Called directly, without that session, the API answers 403, and the session
       is page-issued, so the API is never called outside the page. The connector opens
       the board with the polite browser (``jobindex.browser``) and reads the search
       responses **the page received**, clicking "Go to next page" until it holds the
       site's own total (``maxCount``). Checked: ``maxCount`` never changes between
       pages, each page starts at the number of jobs already read (``offset``), ``count``
       = jobs in the page, no posting twice, and the final number = ``maxCount``. If the
       total changes during the read (a job posted meanwhile: Costa, 446 -> 447), the list
       is read once more from page 1; a second change fails the crawl.
       The page also loads Cloudflare's bot-check script (``/cdn-cgi/challenge-platform``);
       the browser never loads it, and the job list works without it (2026-11-11).
Job:   the search response has everything: title, full description (plain text with
       HTML entities), locations (city, state code, ISO country, address), posting start
       and expiry. No detail requests.

Board identifier: ``{namespace}/{careerSiteXRefCode}`` (e.g. ``costa/CANDIDATEPORTAL``).
The page does not name the employer (title "Job Board | Dayforce Jobs"); set it with
``--company-name`` / ``company:`` in demo_sources.yaml. Verified on El Pollo Loco
(loco, 17 jobs, one page) and Costa Coffee (costa, 444 jobs, 18 pages), 2026-11-11.
"""

import html
import json
import logging
import re
from datetime import datetime
from typing import Any, ClassVar, Protocol

from jobindex.browser import BrowserPage, PoliteBrowser, settle
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import HttpClient

log = logging.getLogger(__name__)

HOST = "jobs.dayforcehcm.com"
_BOARD_URL = re.compile(
    r"^https?://jobs\.dayforcehcm\.com/(?:[a-z]{2}(?:-[A-Za-z]{2,4})?/)?"
    r"(?P<ns>[A-Za-z0-9_-]+)/(?P<site>[A-Za-z0-9_-]+)(?:/|$|\?|#)",
    re.I,
)
_RESERVED = {"api", "_next", "cdn-cgi", "static"}
SEARCH_API = re.compile(r"/api/geo/[^/]+/jobposting/search(?:\?|$)")
#: Safety stop (25 jobs a page: 5,000 jobs).
MAX_PAGES = 200
#: How long one page of results may take (each request waits for its turn: 1/second).
PAGE_TIMEOUT_MS = 90_000


class TotalChangedError(ConnectorError):
    """A job was posted or closed while the pages were read (the order shifted)."""


#: Reads of the whole list when the total changes during one (a busy board posts jobs
#: during the minutes a read of hundreds of jobs takes).
READ_ATTEMPTS = 2


class SearchResults(Protocol):
    """The board page as a visitor uses it (a real browser page, or a fake)."""

    async def first(self) -> dict[str, Any]: ...
    async def next(self) -> dict[str, Any]: ...


class _BrowserResults:
    def __init__(self, browser: PoliteBrowser, url: str) -> None:
        self._browser = browser
        self._url = url
        self._bp: BrowserPage | None = None
        self._read = 0  # search responses consumed

    async def first(self) -> dict[str, Any]:
        self._bp = await self._browser.open(self._url, hosts=[HOST], capture=SEARCH_API)
        return await self._wait_for_response()

    async def next(self) -> dict[str, Any]:
        assert self._bp is not None
        # Ant Design pager: <li class="ant-pagination-next" aria-disabled="...">
        button = self._bp.page.locator("li.ant-pagination-next")
        if await button.count() != 1 or await button.get_attribute("aria-disabled") == "true":
            raise ConnectorError(f"{self._url}: no 'next page' button before the total")
        await button.click()
        return await self._wait_for_response()

    async def _wait_for_response(self) -> dict[str, Any]:
        assert self._bp is not None
        bp = self._bp
        waited = 0
        while len(bp.responses) <= self._read:
            bp.check()
            if waited >= PAGE_TIMEOUT_MS:
                raise ConnectorError(f"{self._url}: the page did not load its job list")
            await bp.page.wait_for_timeout(250)
            waited += 250
        await settle(bp, quiet_ms=300)
        bp.check()
        url, status, body = bp.responses[self._read]
        self._read = len(bp.responses)  # a re-sent search replaces nothing: take the first
        if status != 200:
            raise ConnectorError(f"{url} answered {status} in the page")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ConnectorError(f"{url}: invalid JSON in the page") from exc
        if not isinstance(data, dict):
            raise ConnectorError(f"{url}: unexpected search response")
        return data


async def read_results(results: SearchResults, url: str) -> list[dict[str, Any]]:
    """Every posting of the board, page by page, checked against the site's total."""
    postings: list[dict[str, Any]] = []
    total: int | None = None
    data = await results.first()
    for _ in range(MAX_PAGES):
        page = data.get("jobPostings")
        max_count, offset, count = data.get("maxCount"), data.get("offset"), data.get("count")
        if (
            not isinstance(page, list)
            or not isinstance(max_count, int)
            or not isinstance(offset, int)
            or not isinstance(count, int)
        ):
            raise ConnectorError(f"{url}: unexpected search response {list(data)}")
        if total is None:
            total = max_count
        if max_count != total:
            raise TotalChangedError(
                f"{url}: total changed from {total} to {max_count} while paging"
            )
        if offset != len(postings) or count != len(page):
            raise ConnectorError(
                f"{url}: page at offset {offset} with {count}/{len(page)} jobs, "
                f"expected offset {len(postings)}"
            )
        postings += page
        if len(postings) >= max_count or not page:
            break
        data = await results.next()
    else:
        raise ConnectorError(f"{url}: more than {MAX_PAGES} pages")
    ids = [p.get("jobPostingId") for p in postings]
    if len(set(ids)) != len(ids):
        raise ConnectorError(f"{url}: {len(ids) - len(set(ids))} posting(s) listed twice")
    if len(postings) != total:
        raise ConnectorError(f"{url}: read {len(postings)} jobs, the site says {total}")
    log.info("dayforce %s: %d jobs", url, len(postings))
    return postings


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _region(state_code: str | None, country: str | None) -> str | None:
    if not state_code:
        return None
    if country and state_code.upper().startswith(country.upper() + "-"):
        return state_code[len(country) + 1 :]
    return state_code


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = html.unescape(value).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip() or None


@register
class DayforceConnector(BaseConnector):
    platform = "dayforce"
    uses_browser = True
    #: Replaced in tests by a fake that serves saved responses.
    browser_class: ClassVar[type[PoliteBrowser]] = PoliteBrowser

    @classmethod
    def detect(cls, url: str) -> str | None:
        match = _BOARD_URL.match(url)
        if match is None or match.group("ns").lower() in _RESERVED:
            return None
        return f"{match.group('ns')}/{match.group('site')}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/en-US/{board_identifier}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = self.board_url(board_identifier)
        postings = await self.read_list(self.http, url)
        return [self._to_job(board_identifier, p) for p in postings]

    async def read_list(self, http: HttpClient, url: str) -> list[dict[str, Any]]:
        async with self.browser_class(http) as browser:
            for attempt in range(1, READ_ATTEMPTS + 1):
                try:
                    return await read_results(_BrowserResults(browser, url), url)
                except TotalChangedError as exc:
                    if attempt == READ_ATTEMPTS:
                        raise
                    log.info("%s; reading the list again from page 1", exc)
        raise AssertionError("unreachable")

    def _to_job(self, board_identifier: str, posting: dict[str, Any]) -> RawJob:
        job_id = posting.get("jobPostingId")
        title = posting.get("jobTitle")
        if not isinstance(job_id, int) or not isinstance(title, str) or not title.strip():
            raise ConnectorError(f"dayforce {board_identifier}: posting without id/title")
        locations = [loc for loc in posting.get("postingLocations") or [] if isinstance(loc, dict)]
        first = locations[0] if locations else {}
        country = first.get("isoCountryCode") or None
        addresses = [loc.get("formattedAddress") for loc in locations]
        url = f"{self.board_url(board_identifier)}/jobs/{job_id}"
        return RawJob(
            external_id=str(job_id),
            title=" ".join(title.split()),
            description_text=_text(posting.get("jobDescription")),
            location_raw="; ".join(a for a in addresses if a) or None,
            city=first.get("cityName") or None,
            region=_region(first.get("stateCode"), country),
            country=country,
            posted_at=_parse_time(posting.get("postingStartTimestampUTC")),
            expires_at=_parse_time(posting.get("postingExpiryTimestampUTC")),
            source_url=url,
            apply_url=url,
        )
