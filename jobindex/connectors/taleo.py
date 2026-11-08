"""Oracle Taleo Enterprise career sections (``{tenant}.taleo.net/careersection/{section}``).

List: GET {host}/careersection/sitemap.jss?portalCode={section}&lang=en — Taleo's own SEO
      sitemap of one career section: every open job page
      ``/careersection/{section}/jobdetail.ftl?job={number}&lang=en`` (no ``<lastmod>``).
      ``sitemap.jss?lang=en`` without ``portalCode`` is an index of the tenant's sections
      (its ``<sitemap>`` entries have no ``<loc>`` tag, so it is not used). A section that
      is switched off ("Career Section Unavailable") also serves an *empty* sitemap, so an
      empty sitemap is only accepted after the section's search page was checked to be up.
Job:  the jobdetail page has no JSON-LD; the posting is the argument list of
      ``api.fillList('requisitionDescriptionInterface', 'descRequisition', [...])``, which
      Taleo's script pours into the section's template. Its layout is the same on every
      tenant seen (HDR, Stantec, WSP, 2026-11-08): a fixed preamble, the title, the job
      number, the description blocks (HTML prefixed ``!*!``, sometimes each sent twice),
      then the labelled fields as (value, tooltip) pairs starting with "Primary Location"
      (``Country-State-City``) and ending with the "Job Posting" date
      (``Feb 19, 2025`` or ``Oct 1, 2024, 4:08:57 PM``). The title is cross-checked
      against ``og:title``. A job that is gone is served as ``unavailablerequisition.ftl``
      (``requisitionUnavailableInterface``): stored closed.

Why not the JSON search (``POST /careersection/rest/jobboard/searchjobs``): the sitemap
and job pages are complete and need no headers or page state. robots.txt is 404 on
``*.taleo.net`` (everything allowed).

Board identifier: ``{host}/{section}`` (``wsp.taleo.net/globalcareersection``); custom
domains (``jobs.dubaicareers.ae``) are recognised by the ``/careersection/{section}/
*.ftl`` path. Generic sitemap machinery (``generic_jsonld``): pages are fetched for new
jobs and every GENERIC_REFRESH_DAYS, with checkpoints.
Verified on stantec.taleo.net/ex, wsp.taleo.net/globalcareersection and hdr.taleo.net/ex
(2026-11-08).
"""

import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

from jobindex.connectors.base import ConnectorError, RawJob, company_name_from_page
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.normalize import normalize_employment_type

_SECTION_PATH = re.compile(r"^/careersection/(?P<section>[A-Za-z0-9_.-]{1,80})/[a-z]+\.ftl$")
# Path segments under /careersection/ that are platform folders, not sections.
_NOT_SECTIONS = re.compile(r"^(?:\d{4}PRD[\d.]*|theme|rest|iam|application|feed)$", re.I)
_FILL_LIST = re.compile(
    r"api\.fillList\(\s*'requisitionDescriptionInterface'\s*,\s*'descRequisition'\s*,"
    r"\s*\[(?P<values>.*?)\]\s*\)\s*;",
    re.S,
)
_JS_STRING = re.compile(r"'((?:[^'\\]|\\.)*)'")
_OG_TITLE = re.compile(r"<meta[^>]+property=\"og:title\"[^>]+content=\"([^\"]*)\"", re.I)
_DATE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}(?:, \d{1,2}:\d{2}:\d{2} [AP]M)?$")
_EMPLOYMENT = re.compile(
    r"^(?:full[- ]?time|part[- ]?time|temporary|contract(?:or)?|seasonal|intern(?:ship)?|"
    r"per diem|casual|prn)\b",
    re.I,
)
_UNAVAILABLE_SECTION = re.compile(r"Career Section Unavailable|id=\"maintenanceTitle\"", re.I)
_UNAVAILABLE_JOB = re.compile(r"requisitionUnavailableInterface|unavailablerequisition\.ftl")
#: First labelled field after the description; also proves the template is the known one.
PRIMARY_LOCATION = "Primary Location"


@register
class TaleoConnector(GenericJsonLdConnector):
    platform = "taleo"
    company_name_sources = (
        "og:site_name",
        re.compile(r"^(?:job search\s*[|\-–]\s*)?(?P<name>(?!job search$)[^|]+)$", re.I),
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
        match = _SECTION_PATH.match(parts.path)
        if not host or match is None or _NOT_SECTIONS.match(match["section"]):
            return None
        return f"{host}/{match['section']}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        host, section = cls._split(board_identifier)
        return f"https://{host}/careersection/{section}/jobsearch.ftl?lang=en"

    @staticmethod
    def _split(board_identifier: str) -> tuple[str, str]:
        host, _, section = board_identifier.partition("/")
        if not host or not section:
            raise ConnectorError(f"taleo: bad board identifier {board_identifier!r}")
        return host, section

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        host, section = self._split(board_identifier)
        return [f"https://{host}/careersection/sitemap.jss?portalCode={section}&lang=en"]

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        listed = await super().listed_pages(board_identifier)
        if not any(self.job_id(url, board_identifier) for url, _ in listed):
            # An empty sitemap is also what a switched-off section serves.
            page = await self._get_text(self.board_url(board_identifier))
            if _UNAVAILABLE_SECTION.search(page):
                raise ConnectorError(f"taleo: career section unavailable: {board_identifier}")
        return listed

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        host, section = self._split(board_identifier)
        if (parts.hostname or "").lower() != host:
            return None
        if parts.path != f"/careersection/{section}/jobdetail.ftl":
            return None
        job = parse_qs(parts.query).get("job", [""])[0].strip()
        return job or None

    def is_closed_html(self, html: str) -> bool:
        return _FILL_LIST.search(html) is None and bool(_UNAVAILABLE_JOB.search(html))

    def parse_page_html(self, html: str, stub: RawJob) -> RawJob | None:
        match = _FILL_LIST.search(html)
        if match is None or PRIMARY_LOCATION not in html:
            return None
        values = [_js_unescape(v) for v in _JS_STRING.findall(match["values"])]
        try:
            number_at = values.index(stub.external_id, 1)
        except ValueError:
            return None
        title = values[number_at - 1].strip()
        og_title = _OG_TITLE.search(html)
        if og_title and unescape(og_title.group(1)).strip() not in ("", title):
            raise ConnectorError(f"taleo: title mismatch on {stub.source_url}; layout changed?")

        description: list[str] = []
        fields_at = len(values)
        for index in range(number_at + 1, len(values)):
            value = values[index]
            is_html = value.startswith("!*!")
            following = values[index + 1] if index + 1 < len(values) else None
            if value and not is_html and value == following:
                fields_at = index  # first (value, tooltip) pair: Primary Location
                break
            text = unquote(value.removeprefix("!*!")) if is_html else value
            if text.strip() and text not in description:
                description.append(text)
        fields = values[fields_at:]
        dates = [v for v in fields if _DATE.match(v)]
        employment = next((v for v in fields if _EMPLOYMENT.match(v)), None)
        host, section = self._split_url(stub.source_url or "")
        return RawJob(
            external_id=stub.external_id,
            title=title,
            company=company_name_from_page(html, self.company_name_sources),
            description_html="".join(
                d if d.lstrip().startswith("<") else f"<p>{d}</p>" for d in description
            )
            or None,
            location_raw=_location(fields[0]) if fields else None,
            employment_type=normalize_employment_type(employment),
            posted_at=_posted(dates[-1]) if dates else None,
            source_url=stub.source_url,
            apply_url=(
                f"https://{host}/careersection/{section}/jobapply.ftl"
                f"?job={stub.external_id}&lang=en"
            ),
        )

    @staticmethod
    def _split_url(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        match = _SECTION_PATH.match(parts.path)
        return (parts.hostname or "").lower(), (match["section"] if match else "")


def _js_unescape(value: str) -> str:
    """A single-quoted JS string literal as Taleo writes it (``\\'``, ``\\\\``)."""
    return re.sub(r"\\(.)", r"\1", value)


def _location(value: str) -> str | None:
    """``"United States-Montana-Billings"`` -> ``"Billings, Montana, United States"``;
    a city keeps its own hyphens (``Winston-Salem``)."""
    parts = [p.strip() for p in value.split("-", 2) if p.strip()]
    ordered: list[str] = []
    for part in reversed(parts):
        if not ordered or ordered[-1].lower() != part.lower():
            ordered.append(part)
    return ", ".join(ordered) or None


def _posted(value: str) -> datetime | None:
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
