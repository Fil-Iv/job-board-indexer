"""Rival (formerly SilkRoad) Recruiting career sites (``jobs.silkroad.com/{company}/{portal}``).

List: GET https://jobs.silkroad.com/{company}/{portal}?page={n}   (n = 1, 2, ...)
      Server-rendered, 10 jobs per page: links ``/{company}/{portal}/jobs/{id}`` and
      ``<div id="Jobs_PagedJobList_CurrentPageText">Page 1 of 4</div>``. There is no job
      total, only the page count: pages 1..N are read, every page must add new jobs and the
      page count must not change while paging; anything else is an error. No sitemap; the
      ``/*/*/Ajax/`` endpoints are disallowed by robots.txt. Unknown boards: 404. A page
      without the counter raises (no empty board was available to verify).
Job:  JSON-LD JobPosting (title, description, datePosted, employmentType,
      hiringOrganization, jobLocation; addressRegion is ISO 3166-2, "US-OH" -> "OH").
      Older postings that are still listed have no JSON-LD (BU, 2026-11-08): they are
      mapped from the page markup (``parse_page_html``: ``#Jobs_JobDetail_TitleText``,
      the ``ConfigurablePageDetail__*`` blocks, the company from the page title).
      Generic sitemap + JSON-LD machinery
      (``generic_jsonld``, ``listed_pages`` override): pages are fetched for new jobs and
      every GENERIC_REFRESH_DAYS, with checkpoints.

robots.txt sets ``Crawl-delay: 10`` (honoured by ``HttpClient``; the file starts with a
byte-order mark, see ``jobindex.robots``), so a first crawl of a 200-job board takes about
40 minutes; later crawls only fetch new pages.

Board identifier: ``{company}/{portal}`` as in the URL, case preserved (``SOCCo/Careers``,
``BU/External``).
Verified on SOCCo/Careers and BU/External (2026-11-08).
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.normalize import normalize_employment_type, split_url

HOST = "jobs.silkroad.com"
MAX_PAGES = 500
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
# robots.txt-disallowed / infrastructure folders, never a company
_RESERVED = {"bin", "content", "favicons", "images", "scripts", "views", "api"}
_PAGES = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
_ISO_REGION = re.compile(r"^[A-Z]{2}-(?P<code>[A-Z0-9]{1,3})$")  # "US-OH"
_LINK = re.compile(r"href=[\"'](/[^\"'?#]+/jobs/\d+)/?[\"']")


@register
class RivalConnector(GenericJsonLdConnector):
    platform = "rival"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if host != HOST or len(segments) < 2:
            return None
        company, portal = segments[0], segments[1]
        if company.lower() in _RESERVED or not (_SEGMENT.match(company) and _SEGMENT.match(portal)):
            return None
        return f"{company}/{portal}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}"

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != HOST:
            return None
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) != 4 or "/".join(segments[:2]).lower() != board_identifier.lower():
            return None
        return segments[3] if segments[2].lower() == "jobs" and segments[3].isdigit() else None

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        urls: dict[str, str] = {}
        pages: int | None = None
        page = 1
        while pages is None or page <= pages:
            if page > MAX_PAGES:
                raise ConnectorError(f"rival: over {MAX_PAGES} pages for {board_identifier}")
            url = f"{self.board_url(board_identifier)}?page={page}"
            html = await self._get_text(url)
            counter = _PAGES.search(html)
            found = {
                job_id: f"https://{HOST}{path}"
                for path in _LINK.findall(html)
                if (job_id := self.job_id(f"https://{HOST}{path}", board_identifier))
            }
            if counter is None:  # also a board without jobs: not seen yet, so fail loudly
                raise ConnectorError(f"rival: no page counter on {url}; template changed?")
            if pages is not None and int(counter.group(2)) != pages:
                raise ConnectorError(f"rival: page count changed while paging {url}")
            pages = int(counter.group(2))
            if not set(found) - set(urls):
                raise ConnectorError(f"rival: page {page} of {pages} adds no jobs: {url}")
            urls.update(found)
            page += 1
        return [(url, None) for url in urls.values()]

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        # addressRegion is an ISO 3166-2 code ("US-OH"): keep the subdivision part.
        match = _ISO_REGION.match(job.region or "")
        return replace(job, region=match["code"]) if match else job

    def parse_page_html(self, html: str, stub: RawJob) -> RawJob | None:
        tree = LexborHTMLParser(html)
        heading = tree.css_first("#Jobs_JobDetail_TitleText")
        section = tree.css_first("section.sr-job-detail__description")
        if heading is None or section is None:
            return None

        def field(name: str) -> str | None:
            node = tree.css_first(f"#ConfigurablePageDetail__{name} > div")
            return (node.text(strip=True) or None) if node is not None else None

        parts = [
            node.html or ""
            for name in ("JobDescription", "RequiredSkills")
            if (node := tree.css_first(f"#ConfigurablePageDetail__{name}")) is not None
        ]
        title = " ".join(heading.text().split())
        description = "".join(parts) or section.html
        title_tag = tree.css_first("title")
        # "<job title> - <location> - <company>"
        company = ([*title_tag.text(strip=True).rsplit(" - ", 1), ""])[1] if title_tag else ""
        apply = tree.css_first("#Base_BackToJobs_Multiform_ApplyLink")
        apply_path = (apply.attributes.get("href") or "") if apply is not None else ""
        return RawJob(
            external_id=stub.external_id,
            title=title,
            company=company or None,
            description_html=description,
            location_raw=field("DisplayLocation"),
            employment_type=normalize_employment_type(field("DisplayPosition")),
            posted_at=_posted(field("PostingDate")),
            source_url=stub.source_url,
            apply_url=f"https://{HOST}{apply_path}" if apply_path.startswith("/") else None,
        )


def _posted(value: str | None) -> datetime | None:
    """``"2/9/2026"`` (US month/day/year) -> 2026-02-09 UTC."""
    try:
        return datetime.strptime(value or "", "%m/%d/%Y").replace(tzinfo=UTC)
    except ValueError:
        return None
