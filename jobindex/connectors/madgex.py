"""Wiley Madgex job boards (custom domains only): paged HTML listing + JSON-LD job pages.

List: GET {board}/            then {board}/{n}/   (n = 2, 3, ...)
      ``{board}`` is the board's listing URL: ``jobs.sciencecareers.org/jobs`` or, on
      boards mounted under a path, ``www.timeshighereducation.com/unijobs/listings``.
      Server-rendered, 20 ``<li class="lister__item" id="item-{id}">`` per page, total in
      ``<h1>Found 1,203 jobs</h1>``. Pages are read until every listed id is collected:
      fewer distinct ids than the total (at the start or the end of paging) raises, so the
      list is complete or the crawl fails. The RSS feed (``/jobsrss/``) is capped at 20
      items or disallowed, and the sitemaps omit jobs, so neither is used.
Job:  {root}/job/{id}/{slug}/ (``/listing/`` on some boards): JSON-LD JobPosting (title,
      description, datePosted, validThrough, hiringOrganization, jobLocation, baseSalary
      text). Generic sitemap + JSON-LD machinery (``listed_pages`` override): pages are
      fetched for new jobs and every GENERIC_REFRESH_DAYS, with checkpoints.

robots.txt (checked 2026-11-08) disallows search/RSS/e-mail/preview URLs only; the
listing and job pages are allowed. Some Madgex boards answer 403 to every request
(``coverage/blocked_by_site.yaml``: jobs.theguardian.com, nejmcareercenter.org).

Board identifier: listing URL without scheme and trailing slash. Boards are recognised on
custom domains by the ``<!-- Madgex Tracking ...`` comment + the canonical URL.
Verified on jobs.sciencecareers.org/jobs (541 jobs) and
www.timeshighereducation.com/unijobs/listings (1,203 jobs), 2026-11-08; listing format
also confirmed on jobs.chronicle.com/jobs (28,798 jobs).
"""

import re
from typing import ClassVar
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector, host_of
from jobindex.connectors.registry import register

PAGE_SIZE = 20
MAX_PAGES = 2000  # 40,000 jobs; the largest board seen (Chronicle) has ~29,000
_TOTAL = re.compile(r"Found\s+([\d,.]+)\s+jobs?\b", re.I)
_ITEM_ID = re.compile(r"^item-(\d+)$")
_JOB_PATH = re.compile(r"^(?P<root>/.*?)?/(?:job|listing)/(?P<id>\d+)(?:/[^/]*)?/?$")
_LISTING_PATH = re.compile(r"^(?P<listing>/(?:.*/)?(?:jobs|listings))(?:/.*)?$")


def _root_of(listing_path: str) -> str:
    """``/unijobs/listings`` -> ``/unijobs``; ``/jobs`` -> ``""``."""
    return listing_path.rsplit("/", 1)[0]


@register
class MadgexConnector(GenericJsonLdConnector):
    platform = "madgex"
    page_fingerprint: ClassVar[re.Pattern[str] | None] = re.compile(r"<!-- Madgex Tracking")

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None  # custom domains only: recognised by page_fingerprint

    @classmethod
    def board_from_page_url(cls, url: str) -> str | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/")
        if not host:
            return None
        job = _JOB_PATH.match(path)
        if job:  # a job page: its board's listing is {root}/jobs (or /listings)
            plural = "listings" if "/listing/" in path else "jobs"
            return f"{host}{job['root'] or ''}/{plural}"
        listing = _LISTING_PATH.match(path)
        if listing:
            return f"{host}{listing['listing']}"
        return f"{host}{path}/jobs"  # the home page of a board at the domain root

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier.strip('/')}/"

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        board = urlsplit(self.board_url(board_identifier))
        if host_of(url) != host_of(board.netloc):
            return None
        match = _JOB_PATH.match(parts.path.rstrip("/"))
        if match is None or (match["root"] or "") != _root_of(board.path.rstrip("/")):
            return None
        return match["id"]

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        base = self.board_url(board_identifier)
        urls: dict[str, str] = {}
        first_total: int | None = None
        total = 0
        page = 1
        while True:
            url = base if page == 1 else f"{base}{page}/"
            total, found = _parse_listing(await self._get_text(url), url)
            first_total = total if first_total is None else first_total
            if page > 1 and not set(found) - set(urls) and len(urls) < total:
                raise ConnectorError(f"madgex: page {page} adds no jobs: {url}")
            urls.update(found)
            if len(urls) >= total or not found:
                break
            page += 1
            if page > MAX_PAGES:
                raise ConnectorError(f"madgex: over {MAX_PAGES} pages for {board_identifier}")
        expected = max(first_total, total)
        if len(urls) < expected:
            raise ConnectorError(
                f"madgex: {board_identifier} lists {expected} jobs but {len(urls)} were "
                f"collected over {page} pages (jobs moved while paging?)"
            )
        return [(link, None) for link in urls.values()]


def _parse_listing(html: str, url: str) -> tuple[int, dict[str, str]]:
    """``(total, {job id: absolute job URL})`` of one listing page."""
    match = _TOTAL.search(html)
    if match is None:
        if re.search(r"\bno jobs\b|found\s+0\s+jobs", html, re.I):
            return 0, {}
        raise ConnectorError(f"madgex: no job total on {url}; template changed?")
    total = int(re.sub(r"[,.]", "", match.group(1)))
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    found: dict[str, str] = {}
    for item in LexborHTMLParser(html).css("li.lister__item"):
        item_id = _ITEM_ID.match(item.attributes.get("id") or "")
        link = item.css_first("h3.lister__header a")
        href = (link.attributes.get("href") or "").strip() if link else ""
        if item_id is None or not href:
            continue
        path = urlsplit(href).path
        found[item_id.group(1)] = origin + path if path.startswith("/") else href
    return total, found
