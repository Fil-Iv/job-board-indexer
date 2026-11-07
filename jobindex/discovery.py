"""Discover a board's job URLs from sitemaps / RSS and check them for schema.org JobPosting.

This is the core of a *generic* connector: many job boards and ATS career sites list every
job page in a sitemap (or an RSS feed) and mark each job page up with a schema.org
JobPosting for Google for Jobs. If both hold, one connector can index them all.

:func:`evaluate_board` measures exactly that for one board (``scripts/evaluate_generic.py``
runs it over the coverage plan). It only reads what robots.txt allows.

Sitemap/RSS parsing is regex-based on purpose: no XML entity expansion on untrusted input.
"""

import gzip
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.http import FetchError, HttpClient
from jobindex.schemaorg import extract_job_postings, is_expired

log = logging.getLogger(__name__)

MAX_SITEMAPS = 25  # child sitemaps followed per board
MAX_URLS = 50_000

# Path fragments that mark an individual job page (many languages / platforms).
_JOB_PATH = re.compile(
    r"/(?:jobs?|job-?details?|jobdetail|positions?|vacanc(?:y|ies)|openings?|careers?/job|"
    r"stellen(?:angebote)?|stelle|offres?|empleo|vagas?|ofertas?|requisition|posting|"
    r"careersection/[^/]+/jobdetail)\b",
    re.IGNORECASE,
)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)\s*(?:\]\]>)?\s*</loc>", re.I)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)
_SITEMAP_TAG = re.compile(r"<sitemap>(.*?)</sitemap>", re.I | re.S)
_URL_TAG = re.compile(r"<url>(.*?)</url>", re.I | re.S)
_RSS_LINK = re.compile(r"<item\b.*?<link>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.I | re.S)
_ATOM_LINK = re.compile(r"<entry\b.*?<link[^>]*href=\"([^\"]+)\"", re.I | re.S)
_COMMON_FEEDS = ("/feed/", "/jobs/feed/", "/rss", "/jobs.rss", "/?feed=job_feed", "/feed/jobs")


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    """One ``<url>`` of a sitemap (or ``<item>`` of a feed)."""

    url: str
    lastmod: str | None = None


def parse_sitemap(text: str, base_url: str) -> tuple[list[str], list[SitemapEntry]]:
    """``(child sitemap URLs, page entries)`` of a sitemap index or urlset.

    Relative URLs are resolved against ``base_url``; non-http(s) URLs are dropped (some
    platforms list bare hosts such as ``acme.applicantpro.com``).
    """
    children = _SITEMAP_TAG.findall(text)
    if children or "<sitemapindex" in text.lower():
        locs = [m for c in children for m in _LOC.findall(c)] or _LOC.findall(text)
        return _absolute(base_url, locs), []
    entries: list[SitemapEntry] = []
    blocks = _URL_TAG.findall(text)
    for block in blocks:
        loc = _LOC.search(block)
        if not loc:
            continue
        lastmod = _LASTMOD.search(block)
        for url in _absolute(base_url, [loc.group(1)]):
            entries.append(SitemapEntry(url, lastmod.group(1) if lastmod else None))
    if not blocks:  # <loc> without <url> wrappers (seen on hand-written sitemaps)
        entries = [SitemapEntry(u) for u in _absolute(base_url, _LOC.findall(text))]
    return [], entries


def parse_feed(text: str, base_url: str) -> list[str]:
    """Item links of an RSS or Atom feed."""
    return _absolute(base_url, _RSS_LINK.findall(text) + _ATOM_LINK.findall(text))


def decode_body(url: str, content: bytes, encoding: str | None) -> str:
    """Response body as text; gunzips ``.xml.gz`` sitemaps."""
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content.decode(encoding or "utf-8", errors="replace")


@dataclass(slots=True)
class Discovery:
    sitemaps: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)  # every <loc> seen (pages)
    lastmod: dict[str, str] = field(default_factory=dict)  # page URL -> sitemap <lastmod>
    feed_urls: list[str] = field(default_factory=list)
    job_urls: list[str] = field(default_factory=list)  # candidates that look like job pages
    blocked: list[str] = field(default_factory=list)  # URLs robots.txt/403 kept us from
    errors: list[str] = field(default_factory=list)


def _absolute(base: str, urls: list[str]) -> list[str]:
    """Resolve URLs found in a document against it; drop anything that is not http(s)."""
    resolved = (urljoin(base, u) for u in urls)
    return [u for u in resolved if urlsplit(u).scheme in ("http", "https")]


async def _get_text(http: HttpClient, url: str, d: Discovery) -> str | None:
    try:
        response = await http.get(url)
    except FetchError as exc:
        if exc.status_code in (401, 403, 429) or "robots.txt" in str(exc):
            d.blocked.append(f"{url} ({exc})")
        else:
            d.errors.append(f"{url}: {exc}")
        return None
    try:
        return decode_body(url, response.content, response.encoding)
    except OSError:  # truncated/invalid gzip
        return None


async def _read_sitemaps(http: HttpClient, roots: list[str], d: Discovery) -> None:
    queue = list(dict.fromkeys(roots))
    seen: set[str] = set()
    while queue and len(seen) < MAX_SITEMAPS and len(d.sitemap_urls) < MAX_URLS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        text = await _get_text(http, url, d)
        if not text or "<loc" not in text.lower():
            continue
        d.sitemaps.append(url)
        children, entries = parse_sitemap(text, url)
        # Prefer child sitemaps that look job-related, then the rest.
        children.sort(key=lambda u: 0 if re.search(r"job|career|position|vacanc", u, re.I) else 1)
        queue.extend(children)
        for entry in entries:
            d.sitemap_urls.append(entry.url)
            if entry.lastmod:
                d.lastmod[entry.url] = entry.lastmod


async def _read_feeds(http: HttpClient, board_url: str, page: str | None, d: Discovery) -> None:
    candidates: list[str] = []
    if page:
        tree = LexborHTMLParser(page)
        for link in tree.css('link[rel="alternate"]'):
            kind = (link.attributes.get("type") or "").lower()
            href = link.attributes.get("href")
            if href and ("rss" in kind or "atom" in kind):
                candidates.append(urljoin(board_url, href))
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(board_url))
    if not candidates:
        candidates = [origin + path for path in _COMMON_FEEDS]
    for url in dict.fromkeys(candidates):
        text = await _get_text(http, url, d)
        if not text or not re.search(r"<(rss|feed)\b", text[:2000], re.I):
            continue
        d.feeds.append(url)
        d.feed_urls.extend(parse_feed(text, url))
        break


# Board-path segments that name a page type, not a tenant ("/careers/SearchJobs", "/us/en").
_GENERIC_SEGMENTS = frozenset(
    {"jobs", "job", "careers", "career", "search", "searchjobs", "apply", "home", "listing"}
    | {"openings", "positions", "vacancies", "main", "cw"}
    | {"en", "us", "de", "fr", "uk", "en-us", "en_us", "en-gb"}
)


def tenant_prefix(board_url: str) -> str:
    """The tenant part of a board path on a shared host: ``/sandiegopadres`` for
    careers.hireology.com/sandiegopadres, ``""`` for careers.x.com/jobs/search."""
    segments = [s for s in urlsplit(board_url).path.split("/") if s]
    while segments and (segments[-1].lower() in _GENERIC_SEGMENTS or "." in segments[-1]):
        segments.pop()
    return "/" + "/".join(segments) if segments else ""


def looks_like_job_url(url: str, board_url: str) -> bool:
    board, candidate = urlsplit(board_url), urlsplit(url)
    if candidate.netloc.lower() != board.netloc.lower():
        return False
    if candidate.path.rstrip("/") == board.path.rstrip("/"):
        return False  # the listing itself
    return bool(_JOB_PATH.search(candidate.path)) or bool(re.search(r"/\d{4,}", candidate.path))


async def discover(http: HttpClient, board_url: str) -> Discovery:
    """Find job page URLs for a board via its sitemaps (robots.txt + /sitemap.xml) and feeds."""
    d = Discovery()
    page = await _get_text(http, board_url, d)
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(board_url))
    try:
        roots = _absolute(board_url, await http.robots_sitemaps(board_url))
    except FetchError:
        roots = []
    await _read_sitemaps(http, [*roots, f"{origin}/sitemap.xml"], d)
    await _read_feeds(http, board_url, page, d)
    urls = list(dict.fromkeys(d.sitemap_urls + d.feed_urls))
    d.job_urls = [u for u in urls if looks_like_job_url(u, board_url)]
    # Shared hosts (careers.hireology.com/<tenant>, jobs.dover.com/<tenant>) publish one
    # sitemap for every tenant: keep this board's URLs when the sitemap has any.
    prefix = tenant_prefix(board_url).lower()
    if prefix:
        own = [u for u in d.job_urls if urlsplit(u).path.lower().startswith(prefix + "/")]
        d.job_urls = own or d.job_urls
    return d


@dataclass(slots=True)
class BoardEvaluation:
    board_url: str
    sitemaps: int
    feeds: int
    urls_found: int
    job_urls: int
    sampled: int
    with_jobposting: int
    expired: int
    field_coverage: dict[str, int]
    blocked: list[str]
    errors: list[str]
    sample_urls: list[str]

    @property
    def generic_ok(self) -> bool:
        """Good enough for the generic connector: job URLs discoverable and most sampled
        pages carry a JobPosting with title + description."""
        return (
            self.job_urls > 0
            and self.sampled > 0
            and self.with_jobposting >= max(1, (self.sampled * 2 + 2) // 3)
            and self.field_coverage.get("title", 0) >= self.with_jobposting
            and self.field_coverage.get("description", 0) >= self.with_jobposting
        )

    def as_dict(self) -> dict[str, Any]:
        data = {k: getattr(self, k) for k in self.__dataclass_fields__}
        data["generic_ok"] = self.generic_ok
        return data


_FIELDS = (
    "title",
    "description",
    "datePosted",
    "jobLocation",
    "hiringOrganization",
    "employmentType",
    "validThrough",
    "baseSalary",
    "identifier",
)


_DETAIL_ID = re.compile(r"(?:\d{4,}|[0-9a-f]{8,}|[A-Za-z0-9]{8,}(?=/view))", re.I)


# Listing/browse pages that also carry numeric ids (ApplicantPro, SmartJobBoard, JobMount).
_NOT_DETAIL = re.compile(
    r"/(?:compan(?:y|ies)|employers?|category|categories|locations?|department|"
    r"classifications|employment-type|jobsandemployment|tags?|blogs?|alerts?|search)(?:/|$)",
    re.I,
)


def looks_like_detail_url(url: str, prefix: str = "") -> bool:
    """A single job page rather than a listing/category page: the last path segments
    carry an id (``/jobs/1892162.html``, ``/job/race-engineer-49a8f639``) and the path
    (after the board's tenant ``prefix``) is not a browse page (``/company/1236/x``)."""
    path = urlsplit(url).path
    if prefix and path.lower().startswith(prefix.lower()):
        path = path[len(prefix) :]
    if _NOT_DETAIL.search(path):
        return False
    tail = "/".join(path.rstrip("/").split("/")[-2:])
    return bool(_DETAIL_ID.search(tail))


def pick_sample(job_urls: list[str], sample: int, prefix: str = "") -> list[str]:
    """Up to ``sample`` URLs spread evenly over the list (sitemaps are often oldest-first),
    preferring URLs that look like single job pages over listing/category pages."""
    detail = [u for u in job_urls if looks_like_detail_url(u, prefix)]
    pool = detail or job_urls
    if len(pool) <= sample:
        return list(pool)
    step = (len(pool) - 1) / (sample - 1) if sample > 1 else 0
    return list(dict.fromkeys(pool[round(i * step)] for i in range(sample)))


async def evaluate_board(http: HttpClient, board_url: str, *, sample: int = 6) -> BoardEvaluation:
    d = await discover(http, board_url)
    picks = pick_sample(d.job_urls, sample, tenant_prefix(board_url))
    coverage: Counter[str] = Counter()
    with_posting = expired = 0
    for url in picks:
        text = await _get_text(http, url, d)
        items = extract_job_postings(text) if text else []
        if not items:
            continue
        with_posting += 1
        item = items[0]
        expired += is_expired(item)
        for name in _FIELDS:
            if item.get(name):
                coverage[name] += 1
    return BoardEvaluation(
        board_url=board_url,
        sitemaps=len(d.sitemaps),
        feeds=len(d.feeds),
        urls_found=len(set(d.sitemap_urls + d.feed_urls)),
        job_urls=len(d.job_urls),
        sampled=len(picks),
        with_jobposting=with_posting,
        expired=expired,
        field_coverage=dict(coverage),
        blocked=d.blocked,
        errors=d.errors[:5],
        sample_urls=picks,
    )
