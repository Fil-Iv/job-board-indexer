"""Generic connector: sitemap/RSS job URLs -> job pages -> schema.org JobPosting.

Many job-board platforms and ATS career sites list every job page in a sitemap (some in an
RSS feed) and describe each job page with a schema.org ``JobPosting`` (JSON-LD or
microdata) for Google for Jobs. :class:`GenericJsonLdConnector` indexes such boards; each
platform is a thin subclass in its own module that says where the sitemaps are and which
URLs are job pages of a board (``generic_jsonld`` itself registers nothing).

How a crawl works
-----------------
1. ``fetch_jobs`` reads the board's sitemaps (``sitemap_roots``; index files are
   followed) and/or feeds, keeps the URLs ``job_id`` accepts and returns one *stub* per
   job: external id + URL + ``list_version`` (the sitemap ``<lastmod>``). Any failed or
   unreadable sitemap raises, so the list is complete or the crawl fails.
2. The crawler calls ``fetch_job_detail`` only for stubs whose ``list_version`` differs
   from the stored job (``detail_cache_key``); unchanged jobs reuse the stored data.
   Without a ``<lastmod>`` the version is a refresh token that changes every
   ``GENERIC_REFRESH_DAYS`` (staggered per URL), so edits are still picked up.
3. ``fetch_job_detail`` downloads the page and maps its JobPosting to a RawJob.
   A posting that is over — ``validThrough`` in the past, a "this job is closed/expired"
   notice, HTTP 404/410, or a redirect away from the job — is returned with ``expires_at``
   set; the crawler stores it closed and skips it while its ``list_version`` is unchanged.

Safety
------
* ``max_job_drop()``: the crawl fails (``JobCountDropError``) instead of closing jobs if
  the board lists more than ``GENERIC_MAX_JOB_DROP`` (default 50%) fewer open postings
  than on the last successful crawl.
* Pages with neither a JobPosting nor a closed notice are tolerated (stored closed) but
  if they are more than ``MAX_UNEXPLAINED_SHARE`` of the pages fetched in one crawl, the
  crawl fails: the page template probably changed.

Resumable
---------
Large boards (thousands of pages at 1 request/second) save the fetched pages to the
source's checkpoint every ``CHECKPOINT_EVERY`` pages. If the crawl fails, the next one
reuses those pages (same URL + ``list_version``, checkpoint less than
``CHECKPOINT_MAX_AGE`` old) instead of downloading them again. The crawler clears the
checkpoint after a successful crawl.
"""

import logging
import re
import zlib
from abc import abstractmethod
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.checkpoint import is_fresh, now_iso
from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.discovery import decode_body, parse_feed, parse_sitemap
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import infer_salary_period, parse_datetime
from jobindex.schemaorg import extract_job_postings, to_raw_job

log = logging.getLogger(__name__)

MAX_SITEMAP_FILES = 500  # per board; more means a misconfigured filter -> refuse
CHECKPOINT_EVERY = 100  # fetched pages between checkpoint saves
CHECKPOINT_MAX_AGE = timedelta(hours=24)
CHECKPOINT_VERSION = 1
MAX_UNEXPLAINED = 3  # pages without JobPosting/closed notice tolerated per crawl ...
MAX_UNEXPLAINED_SHARE = 0.2  # ... or this share of fetched pages, whichever is larger
PROGRESS_EVERY = 100

# Words that make a closed-sounding phrase conditional: "open until the position is filled".
_NOT_AFTER = "".join(
    f"(?<!{word} )" for word in ("until", "once", "when", "after", "before", "if", "unless")
)

# "This job is closed" notices, matched against the page's visible text (lower-cased).
CLOSED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        _NOT_AFTER + r"\b(?:this|the) "
        r"(?:job|position|posting|vacancy|role|opening|opportunity|listing)"
        r"(?: posting| listing| ad| offer)? (?:is|has been|was) (?:now )?"
        r"(?:no longer (?:available|active|open|accepting applications)|closed|expired|filled)"
    ),
    re.compile(r"\bno longer accepting applications\b"),
    re.compile(r"\b(?:job|position|posting|vacancy) (?:has )?expired\b"),
    re.compile(
        r"cette offre (?:n'est plus (?:disponible|active|en ligne)|est (?:expir|clôtur|pourvue))"
    ),
    re.compile(
        r"(?:stelle|stellenanzeige|anzeige) ist "
        r"(?:nicht mehr (?:verfügbar|aktiv)|abgelaufen|besetzt)"
    ),
    re.compile(r"oferta (?:ya )?no está disponible|oferta (?:ha )?(?:caducad|expirad)"),
)

_CANONICAL = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)|"
    r"<meta[^>]+property=[\"']og:url[\"'][^>]*content=[\"']([^\"']+)",
    re.I,
)

#: Fields only the job page provides (all content); stubs carry id, URL and list_version.
_DETAIL_FIELDS = tuple(
    f.name for f in fields(RawJob) if f.name not in ("external_id", "list_version")
)


class GenericJsonLdConnector(BaseConnector):
    """Base for sitemap + JSON-LD platforms. Subclasses set ``platform`` and implement
    ``detect``, ``board_url`` and ``job_id`` (plus ``sitemap_roots`` if robots.txt does
    not point at the job sitemap); see the module docstring. One-host boards can use
    :class:`HostedBoardConnector`.
    """

    needs_detail = True
    detail_fields: ClassVar[tuple[str, ...]] = _DETAIL_FIELDS
    detail_cache_key: ClassVar[tuple[str, ...]] = ("list_version",)

    #: Regex that identifies one of this platform's pages on a custom domain; the board is
    #: then derived from the page's canonical URL (``board_from_page_url``).
    page_fingerprint: ClassVar[re.Pattern[str] | None] = None
    #: Patterns of "job closed" notices (visible text, lower-cased).
    closed_patterns: ClassVar[tuple[re.Pattern[str], ...]] = CLOSED_PATTERNS
    #: Child sitemaps of an index that may contain job URLs (searched in the child URL);
    #: ``None`` follows every child.
    child_sitemap: ClassVar[re.Pattern[str] | None] = None
    #: False for platforms whose ``validThrough`` is not the real end of the posting
    #: (Hireology: date posted + 30 days on jobs that are still open).
    trust_valid_through: ClassVar[bool] = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._resumed: dict[str, dict[str, Any]] = {}  # external_id -> checkpointed page
        self._pending: dict[str, dict[str, Any]] = {}  # fetched since the crawl started
        self._board: str | None = None
        #: Hosts the board's sitemaps were served from after redirects (a native
        #: ``acme.mysmartjobboard.com`` that redirects to ``jobs.acme.org``).
        self.board_hosts: set[str] = set()
        self._fetched = 0
        self._unexplained = 0

    # -- per-platform hooks --------------------------------------------------

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        """Sitemap (or sitemap index) URLs listing the board's job pages. Default: the
        ``Sitemap:`` lines of robots.txt, else ``/sitemap.xml``."""
        url = self.board_url(board_identifier)
        declared = await self.http.robots_sitemaps(url)
        return declared or ["{0.scheme}://{0.netloc}/sitemap.xml".format(urlsplit(url))]

    async def feed_urls(self, board_identifier: str) -> list[str]:
        """RSS/Atom feeds listing job pages, read in addition to the sitemaps."""
        return []

    @abstractmethod
    def job_id(self, url: str, board_identifier: str) -> str | None:
        """External id if ``url`` is a job page of this board, else ``None``."""

    def page_url(self, url: str, external_id: str, board_identifier: str) -> str:
        """URL to download for a listed job (default: as listed; override to skip a
        redirect the platform always makes)."""
        return url

    @classmethod
    def board_from_page_url(cls, url: str) -> str | None:
        """Board identifier for a custom-domain page carrying ``page_fingerprint``."""
        return urlsplit(url).hostname

    def parse_date(self, value: str) -> datetime | None:
        """Parse a JobPosting date (``datePosted``/``validThrough``); override for platforms
        with non-ISO dates."""
        return parse_datetime(value)

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        """Platform-specific fix-ups of the mapped posting (``item`` = the JobPosting)."""
        return job

    def is_closed_page(self, text: str) -> bool:
        return any(p.search(text) for p in self.closed_patterns)

    # -- BaseConnector -------------------------------------------------------

    @classmethod
    def detect_in_html(cls, html: str, page_url: str | None = None) -> dict[str, int]:
        counts = super().detect_in_html(html, page_url)
        if cls.page_fingerprint is None or not cls.page_fingerprint.search(html):
            return counts
        # The board is the page's own site: its canonical URL, else where it was fetched.
        urls = [m.group(1) or m.group(2) for m in _CANONICAL.finditer(html)]
        urls = [u for u in urls if u.startswith("http")] or ([page_url] if page_url else [])
        for url in urls:
            board = cls.board_from_page_url(url)
            if board:
                counts[board] = counts.get(board, 0) + 1
        return counts

    def max_job_drop(self) -> float | None:
        return get_settings().generic_max_job_drop

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        await self._load_checkpoint(board_identifier)
        entries: dict[str, tuple[str, str | None]] = {}  # external_id -> (url, lastmod)

        def add(url: str, lastmod: str | None) -> None:
            external_id = self.job_id(url, board_identifier)
            if external_id is None:
                return
            known = entries.get(external_id)
            if known is None or (lastmod or "") > (known[1] or ""):
                entries[external_id] = (url, lastmod)

        queue = list(dict.fromkeys(await self.sitemap_roots(board_identifier)))
        seen: set[str] = set()
        while queue:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if len(seen) > MAX_SITEMAP_FILES:
                raise ConnectorError(
                    f"more than {MAX_SITEMAP_FILES} sitemaps for {board_identifier}"
                )
            text = await self._get_text(url)
            if "<urlset" not in text.lower() and "<sitemapindex" not in text.lower():
                raise ConnectorError(f"{url} is not a sitemap")
            children, pages = parse_sitemap(text, url)
            pattern = self.child_sitemap
            queue += [c for c in children if pattern is None or pattern.search(c)]
            for page in pages:
                add(page.url, page.lastmod)
        for url in await self.feed_urls(board_identifier):
            text = await self._get_text(url)
            if not re.search(r"<(rss|feed)\b", text[:2000], re.I):
                raise ConnectorError(f"{url} is not an RSS/Atom feed")
            for link in parse_feed(text, url):
                add(link, None)

        log.info(
            "%s %s: %d job URLs from %d sitemaps",
            self.platform,
            board_identifier,
            len(entries),
            len(seen),
        )
        return [
            RawJob(
                external_id=external_id,
                title="",
                source_url=self.page_url(url, external_id, board_identifier),
                list_version=self._list_version(url, lastmod),
            )
            for external_id, (url, lastmod) in entries.items()
        ]

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        saved = self._resumed.get(job.external_id)
        if saved is not None and saved.get("list_version") == job.list_version:
            return _load_job(saved)
        detailed = await self._fetch_page(board_identifier, job)
        self._fetched += 1
        self._pending[job.external_id] = _dump_job(detailed)
        if self._fetched % CHECKPOINT_EVERY == 0:
            await self._save_checkpoint(board_identifier)
        if self._fetched % PROGRESS_EVERY == 0:
            log.info(
                "%s %s: %d job pages fetched (%d reused from checkpoint)",
                self.platform,
                board_identifier,
                self._fetched,
                len(self._resumed),
            )
        return detailed

    # -- internals -----------------------------------------------------------

    async def _get_text(self, url: str) -> str:
        response = await self.http.get(url)
        self.board_hosts.add(host_of(str(response.url)))
        try:
            return decode_body(url, response.content, response.encoding)
        except (OSError, zlib.error) as exc:
            raise ConnectorError(f"cannot decompress {url}: {exc}") from exc

    def _list_version(self, url: str, lastmod: str | None) -> str:
        if lastmod:
            return lastmod[:64]
        day = (datetime.now(UTC) - datetime(2000, 1, 1, tzinfo=UTC)).days
        return refresh_token(url, max(1, get_settings().generic_refresh_days), day)

    async def _fetch_page(self, board_identifier: str, stub: RawJob) -> RawJob:
        url = stub.source_url or ""
        now = datetime.now(UTC)
        closed = replace(stub, expires_at=now)  # tombstone: the posting is over
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return closed
            raise
        html = response.text
        final_url = str(response.url)
        items = extract_job_postings(html)
        text = _visible_text(html)
        if not items:
            moved = self.job_id(final_url, board_identifier) != stub.external_id
            if not (moved or self.is_closed_page(text)):
                self._unexplained += 1
                limit = max(MAX_UNEXPLAINED, self._fetched * MAX_UNEXPLAINED_SHARE)
                if self._unexplained > limit:
                    raise ConnectorError(
                        f"{self._unexplained} of {self._fetched + 1} job pages had no "
                        f"JobPosting (latest: {url}); page template changed?"
                    )
                log.warning("%s: no JobPosting on %s; treating it as closed", self.platform, url)
            return replace(closed, title=_page_title(html))
        item = items[0]
        raw = to_raw_job(item, external_id=stub.external_id, source_url=url)
        if raw.posted_at is None and isinstance(item.get("datePosted"), str):
            raw = replace(raw, posted_at=self.parse_date(item["datePosted"].strip()))
        raw = self.adjust(_plausible_salary(raw), item)
        expires_at = self._valid_through(item) if self.trust_valid_through else None
        if self.is_closed_page(text):
            expires_at = now if expires_at is None else min(expires_at, now)
        return replace(raw, expires_at=expires_at, list_version=stub.list_version)

    def _valid_through(self, item: dict[str, Any]) -> datetime | None:
        value = item.get("validThrough")
        value = value[0] if isinstance(value, list) and value else value
        if not isinstance(value, str):
            return None
        value = value.strip()
        parsed = self.parse_date(value)
        if parsed is not None and len(value) > 10:
            return parsed
        # A date (or a malformed timestamp around one, e.g. ApplicantPro's
        # "2026-12-03 00:00:00T23:59"): valid through the end of that day.
        date = re.match(r"\d{4}-\d{2}-\d{2}", value)
        day = parse_datetime(date.group(0)) if date else None
        return day + timedelta(days=1) if day else None

    async def _load_checkpoint(self, board_identifier: str) -> None:
        self._board = board_identifier
        data = await self.checkpoint.load()
        if (
            not data
            or data.get("version") != CHECKPOINT_VERSION
            or data.get("board") != board_identifier
            or not is_fresh(data.get("saved_at"), CHECKPOINT_MAX_AGE)
        ):
            return
        self._resumed = dict(data.get("pages") or {})
        log.info(
            "%s %s: resuming, %d job pages from the checkpoint",
            self.platform,
            board_identifier,
            len(self._resumed),
        )

    async def _save_checkpoint(self, board_identifier: str) -> None:
        pages = {**self._resumed, **self._pending}
        await self.checkpoint.save(
            {
                "version": CHECKPOINT_VERSION,
                "board": board_identifier,
                "saved_at": now_iso(),
                "pages": pages,
            }
        )


def refresh_token(url: str, days: int, day: int) -> str:
    """``list_version`` for a URL without ``<lastmod>``: changes every ``days`` days (``day``
    = days since 2000-01-01), on a different day per URL, so a big board's pages are not
    all re-fetched by the same crawl."""
    offset = zlib.crc32(url.encode()) % days
    return f"refresh:{days}:{(day + offset) // days}"


MAX_PLAUSIBLE_ANNUAL_SALARY = Decimal(5_000_000)


def _plausible_salary(job: RawJob) -> RawJob:
    """Fix or drop salaries that cannot be right.

    * annual amounts labelled hourly (ApplicantPro: "HOUR", 79229.54-113390.04): an hourly
      rate of 1000+ is re-inferred from the amount;
    * amounts in cents (JobMount on jobnet.wacities.org: 10226400-12463200 "YEAR" for
      $102,264-124,632, on some postings only): annual amounts of 5M+ are dropped, since
      guessing the scale would be wrong as often as right.
    """
    amount = job.salary_max or job.salary_min
    if amount is None:
        return job
    if job.salary_period == "hour" and amount >= 1000:
        return replace(job, salary_period=infer_salary_period(amount))
    if job.salary_period in ("year", None) and amount >= MAX_PLAUSIBLE_ANNUAL_SALARY:
        return replace(
            job, salary_min=None, salary_max=None, salary_currency=None, salary_period=None
        )
    return job


def _visible_text(html: str) -> str:
    tree = LexborHTMLParser(html)
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    body = tree.body
    return re.sub(r"\s+", " ", body.text(separator=" ") if body else "").lower()


def _page_title(html: str) -> str:
    title = LexborHTMLParser(html).css_first("title")
    return title.text(strip=True)[:500] if title else ""


def _dump_job(job: RawJob) -> dict[str, Any]:
    """JSON-safe dict of a RawJob (for the checkpoint)."""
    data: dict[str, Any] = {}
    for name, value in job.as_dict().items():
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, RemoteType):
            value = value.value
        data[name] = value
    return data


def _load_job(data: dict[str, Any]) -> RawJob:
    values = dict(data)
    for name in ("posted_at", "expires_at"):
        values[name] = parse_datetime(values.get(name))
    for name in ("salary_min", "salary_max"):
        values[name] = Decimal(values[name]) if values.get(name) is not None else None
    values["remote_type"] = RemoteType(values.get("remote_type") or RemoteType.UNKNOWN.value)
    known = {f.name for f in fields(RawJob)}
    return RawJob(**{k: v for k, v in values.items() if k in known})


def host_of(url: str) -> str:
    """Lower-case host of ``url`` without ``www.``."""
    host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return host.removeprefix("www.")


_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Platform infrastructure, not boards (cdn2.niceboard.co, assets.jobboardly.com, ...).
_RESERVED = re.compile(
    r"^(?:www|app|api|admin|help|support|docs|blog|status|cdn\d*|assets?|static|media|"
    r"img|images|files|mail|geo|feeds)$"
)


class HostedBoardConnector(GenericJsonLdConnector):
    """A board is one host: ``{board}.<platform domain>`` or a custom domain.

    Board identifier: the host as published (``www.motorsport.jobs``, lower case). URLs on
    ``native_suffix`` hosts are detected directly; custom domains through
    ``page_fingerprint`` + the page's canonical URL. Job pages are the URLs on the board's
    host whose path matches ``job_path`` (named group ``id`` = external id).
    """

    #: ``".niceboard.co"``: subdomains of this are boards of the platform.
    native_suffix: ClassVar[str | None] = None
    job_path: ClassVar[re.Pattern[str]]

    @classmethod
    def detect(cls, url: str) -> str | None:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
        if cls.native_suffix is None or not host.endswith(cls.native_suffix):
            return None
        sub = host.removesuffix(cls.native_suffix)
        if "." in sub or _RESERVED.match(sub) or not _SUBDOMAIN.match(sub):
            return None
        return host

    @classmethod
    def board_from_page_url(cls, url: str) -> str | None:
        host = (urlsplit(url).hostname or "").lower()
        return host or None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}/"

    def job_id(self, url: str, board_identifier: str) -> str | None:
        host = host_of(url)
        if host != host_of(board_identifier) and host not in self.board_hosts:
            return None
        match = self.job_path.fullmatch(urlsplit(url).path)
        if match is None:
            return None
        # the first named group that matched (alternatives: ``(?P<id>..)|(?P<uuid>..)``)
        return next((value for value in match.groupdict().values() if value), None)


__all__ = ["GenericJsonLdConnector", "HostedBoardConnector", "host_of"]
