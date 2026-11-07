"""The interface every platform connector implements.

A connector knows three things about one platform (Greenhouse, Lever, ...):

1. ``detect(url)`` — whether a URL points at a board on that platform, and which board
   (the *board identifier*, stored in ``sources.board_identifier``).
2. ``fetch_jobs(board_identifier)`` — download **all** currently open postings, following
   pagination, mapped to :class:`RawJob`.
3. optionally ``fetch_job_detail(board_identifier, job)`` — for platforms whose list
   endpoint lacks descriptions (set ``needs_detail = True``).

Connectors never touch the database; ``jobindex.crawler`` diffs their output with what
is stored.

Contract for ``fetch_jobs``: return the **complete** list of open postings, or raise.
Never return a partial list (e.g. when page 3 of 5 fails): every posting missing from a
successful result is marked closed. Raise :class:`ConnectorError` for unexpected
payloads; ``FetchError`` from the HTTP client may simply propagate. Either way the
crawler records a fetch failure and leaves the jobs untouched.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields, replace
from datetime import datetime
from decimal import Decimal
from html import unescape
from typing import Any, ClassVar

from jobindex.checkpoint import CheckpointStore, MemoryCheckpoint
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    content_hash,
    detect_remote_type,
    find_salary,
    html_to_text,
    infer_salary_period,
    normalize_country,
    normalize_region,
    parse_location,
)


class ConnectorError(Exception):
    """The response was fetched but could not be understood (unexpected shape, etc.)."""


class JobCountDropError(ConnectorError):
    """The board suddenly lists far fewer postings than on the last successful crawl;
    treated as a fetch failure rather than as mass closures (see ``max_job_drop``)."""


@dataclass(frozen=True, slots=True)
class RawJob:
    """One posting as returned by a connector; mirrors the content columns of ``jobs``.

    Connectors fill what the platform provides (structured fields preferred over parsed
    text). :meth:`normalized` derives the rest (plain-text description, location parts,
    remote type, salary found in the text) with the helpers in ``jobindex.normalize``.
    """

    external_id: str
    title: str
    company: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    location_raw: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2
    remote_type: RemoteType = RemoteType.UNKNOWN
    employment_type: str | None = None  # see normalize.normalize_employment_type
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    salary_currency: str | None = None  # ISO 4217
    salary_period: str | None = None  # hour | day | week | month | year
    posted_at: datetime | None = None
    source_url: str | None = None
    apply_url: str | None = None
    #: The posting stops being open at this time (schema.org ``validThrough``, or "now"
    #: for a page that says the job is closed). The crawler never counts a posting whose
    #: ``expires_at`` has passed as open: it is stored closed, so its detail page is not
    #: fetched again while ``list_version`` is unchanged.
    expires_at: datetime | None = None
    #: Opaque version of the posting in the *list* (e.g. the sitemap ``<lastmod>``), for
    #: ``detail_cache_key``. Not part of ``raw_hash``: it says when to re-fetch, not what
    #: the posting contains.
    list_version: str | None = None

    def normalized(self, *, company_fallback: str | None = None) -> "RawJob":
        """Return a copy with derivable fields filled in. Explicit values are kept."""
        changes: dict[str, Any] = {}
        if self.company is None and company_fallback:
            changes["company"] = company_fallback
        if self.description_text is None and self.description_html:
            changes["description_text"] = html_to_text(self.description_html)

        loc = parse_location(self.location_raw)
        country = normalize_country(self.country) or loc.country
        if self.city is None and self.region is None and loc.country in (None, country):
            changes.update(city=loc.city, region=loc.region)
        else:
            changes["region"] = normalize_region(self.region, country)
        changes["country"] = country

        if self.remote_type is RemoteType.UNKNOWN:
            changes["remote_type"] = detect_remote_type(self.location_raw, self.title)

        if (self.salary_min is not None or self.salary_max is not None) and not self.salary_period:
            changes["salary_period"] = infer_salary_period(self.salary_max or self.salary_min)
        elif self.salary_min is None and self.salary_max is None:
            salary = find_salary(changes.get("description_text", self.description_text))
            if salary is not None:
                changes.update(
                    salary_min=salary.min,
                    salary_max=salary.max,
                    salary_currency=salary.currency,
                    salary_period=salary.period,
                )
        return replace(self, **changes) if changes else self

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def content_hash(self) -> str:
        """Hash of the meaningful fields (normalize.HASH_FIELDS) — drives change tracking."""
        return content_hash(self.as_dict())

    def raw_hash(self) -> str:
        """Hash of *every* field as given; call it before :meth:`normalized` to fingerprint
        what the source sent (tells source changes from our own parser changes)."""
        data = self.as_dict()
        del data["list_version"]
        if data["expires_at"] is None:  # keeps hashes from before the field existed valid
            del data["expires_at"]
        return content_hash(data, keys=tuple(sorted(data)))


# Pulls URLs out of HTML/JS/JSON: absolute, protocol-relative, and JSON-escaped ("\/").
_URL_IN_TEXT = re.compile(r"(?:https?:)?//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s\"'<>`\\)]*")


def urls_in_html(html: str) -> list[str]:
    text = unescape(html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/"))
    urls = []
    for match in _URL_IN_TEXT.findall(text):
        urls.append("https:" + match if match.startswith("//") else match)
    return urls


class BaseConnector(ABC):
    #: Unique platform key, stored in ``sources.platform`` (e.g. ``"greenhouse"``).
    platform: ClassVar[str]

    #: Regexes with a named group ``id`` that find the board identifier in page HTML
    #: where it is not part of a URL (e.g. ``Grnhse.Settings.boardToken = "acme"``).
    html_fingerprints: ClassVar[tuple[re.Pattern[str], ...]] = ()

    #: True if ``fetch_jobs`` lacks descriptions and ``fetch_job_detail`` must be called.
    needs_detail: ClassVar[bool] = False
    #: Fields only ``fetch_job_detail`` provides. The crawler reuses the stored values when
    #: ``detail_cache_key`` fields are unchanged, so details are fetched only for new or
    #: changed postings.
    detail_fields: ClassVar[tuple[str, ...]] = ("description_html", "description_text")
    detail_cache_key: ClassVar[tuple[str, ...]] = ("title", "location_raw", "posted_at")

    def __init__(self, http: HttpClient, checkpoint: CheckpointStore | None = None) -> None:
        self.http = http
        #: Where a long ``fetch_jobs`` may save progress to resume after a failure
        #: (``sources.crawl_checkpoint`` when run by the crawler). See jobindex.checkpoint.
        self.checkpoint: CheckpointStore = checkpoint or MemoryCheckpoint()

    @classmethod
    @abstractmethod
    def detect(cls, url: str) -> str | None:
        """Return the board identifier if ``url`` is one of this platform's URLs."""

    @classmethod
    @abstractmethod
    def board_url(cls, board_identifier: str) -> str:
        """Canonical public URL of the board (stored in ``sources.base_url``)."""

    @classmethod
    def detect_in_html(cls, html: str) -> dict[str, int]:
        """Board identifiers referenced by a (custom-domain) careers page, with counts.

        Looks at every URL in the page (script ``src``, iframes, embeds, links, URLs inside
        inline JSON) and at :attr:`html_fingerprints`.
        """
        counts: dict[str, int] = {}
        for url in urls_in_html(html):
            identifier = cls.detect(url)
            if identifier:
                counts[identifier] = counts.get(identifier, 0) + 1
        for pattern in cls.html_fingerprints:
            for match in pattern.finditer(html):
                identifier = match.group("id")
                counts[identifier] = counts.get(identifier, 0) + 1
        return counts

    @abstractmethod
    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        """Return every currently open posting on the board, or raise. See module docstring."""

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        """Return ``job`` completed with detail-only fields, or ``None`` if the posting no
        longer exists (it will then be treated as closed). Raise on any other failure."""
        return job

    def max_job_drop(self) -> float | None:
        """Largest tolerated fractional drop of the open-job count vs. the last successful
        crawl (0.5 = refuse a result with less than half the postings). ``None`` disables
        the check. Used for sources where a broken listing (sitemap, page template) can
        look like a board that closed most of its jobs."""
        return None

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        """Company name for ``add-source``, if the platform exposes it."""
        return None
