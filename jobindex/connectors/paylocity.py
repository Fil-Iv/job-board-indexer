"""Paylocity job boards (``recruiting.paylocity.com``): embedded job list + HTML job pages.

List:   GET https://recruiting.paylocity.com/recruiting/jobs/All/{module guid}
        Server-rendered page with ``window.pageData = {...};``: ModuleTitle (company) and
        ``Jobs: [{JobId, JobTitle, LocationName, PublishedDate, IsInternal, IsRemote,
        JobLocation{City, State, Country}, Description (truncated)}]``: every posted job
        (481 on AdaptHealth, 2026-11-07). Internal-only jobs (``IsInternal``) are skipped.
        An unknown guid redirects to ``/Recruiting/jobs/JobNotFound`` (no pageData): error.
        The JSON feed ``/recruiting/v2/api/feed/jobs/{guid}`` answered ``"jobs": []`` for
        a board with 481 jobs, so it is not used. robots.txt: 404 (everything allowed).
Detail: GET https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}
        (only for new/changed jobs; ``PublishedDate`` changes when a job is re-published)
        ``.job-preview-header .preview-location`` starts with the work arrangement
        ("Fully Remote", "Hybrid Remote"; the list's ``IsRemote`` is true for both), and
        ``.job-preview-details`` has ``div.job-listing-header`` sections ("Job Type",
        "Description", "Requirements", ...), each followed by its content. Some tenants add
        a JSON-LD JobPosting (AdaptHealth), others do not (Peak Support), so the HTML is
        the source. Closed jobs redirect to ``/Recruiting/Jobs/JobNotFound``.

Board identifier: the module guid, lower case
(``4bcae427-e9e7-4d6d-9772-73ef70c3a278``).

URL formats recognised:
    recruiting.paylocity.com/recruiting/jobs/All/{guid}[/{name}]   (any case)
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on AdaptHealth (4bcae427-...) and Peak Support (67c76e24-...), 2026-11-07.
"""

import json
import re
from dataclasses import replace
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, parse_datetime, split_url

HOST = "recruiting.paylocity.com"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\})\s*;\s*\n", re.S)
_TYPE_SECTIONS = {"job type", "position type", "employment type"}


def page_data(html: str) -> dict[str, Any]:
    """The board page's ``window.pageData`` object (with its ``Jobs`` list)."""
    match = _PAGE_DATA.search(html)
    if match is None:
        raise ConnectorError("paylocity: no window.pageData in the board page (unknown board?)")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise ConnectorError(f"paylocity: window.pageData is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("Jobs"), list):
        raise ConnectorError("paylocity: window.pageData has no Jobs list")
    return data


def _next_element(node: LexborNode) -> LexborNode | None:
    sibling = node.next
    while sibling is not None and sibling.tag in ("-text", "-comment"):
        sibling = sibling.next
    return sibling


def _remote(label: str) -> RemoteType:
    text = label.lower()
    if "hybrid" in text:
        return RemoteType.HYBRID
    if "remote" in text:
        return RemoteType.REMOTE
    if "on-site" in text or "onsite" in text or "in office" in text:
        return RemoteType.ONSITE
    return RemoteType.UNKNOWN


@register
class PaylocityConnector(BaseConnector):
    platform = "paylocity"
    needs_detail = True
    detail_fields = ("description_html", "description_text", "employment_type", "remote_type")

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host != HOST or lowered[:3] != ["recruiting", "jobs", "all"] or len(lowered) < 4:
            return None
        return lowered[3] if _GUID.match(lowered[3]) else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/recruiting/jobs/All/{board_identifier}"

    async def _board(self, board_identifier: str) -> dict[str, Any]:
        response = await self.http.get(self.board_url(board_identifier))
        return page_data(response.text)

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self._board(board_identifier)
        company = str(data.get("ModuleTitle") or "") or None
        jobs = []
        for item in data["Jobs"]:
            if not isinstance(item, dict) or not item.get("JobId"):
                raise ConnectorError("paylocity: job without JobId in pageData")
            if not item.get("IsInternal"):
                jobs.append(self._map(item, company))
        return jobs

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        return str((await self._board(board_identifier)).get("ModuleTitle") or "") or None

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = job.source_url or f"https://{HOST}/Recruiting/Jobs/Details/{job.external_id}"
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        segments = [s.lower() for s in split_url(str(response.url))[1]]
        if segments != ["recruiting", "jobs", "details", job.external_id]:
            return None  # redirected to JobNotFound: closed
        tree = LexborHTMLParser(response.text)
        details = tree.css_first(".job-preview-details")
        if details is None:
            raise ConnectorError(f"paylocity: no .job-preview-details on {url}; template changed?")
        sections: list[str] = []
        employment_type = None
        for header in details.css(".job-listing-header"):
            name = " ".join(header.text().split())
            content = _next_element(header)
            if content is None:
                continue
            if name.lower() in _TYPE_SECTIONS:
                employment_type = normalize_employment_type(content.text(strip=True))
            elif (content.inner_html or "").strip():
                sections.append(f"<h3>{name}</h3>{(content.inner_html or '').strip()}")
        location = tree.css_first(".job-preview-header .preview-location")
        remote = _remote(location.text().split("•")[0] if location else "")
        return replace(
            job,
            description_html="\n".join(sections) or None,
            employment_type=employment_type,
            remote_type=job.remote_type if remote is RemoteType.UNKNOWN else remote,
        )

    @staticmethod
    def _map(item: dict[str, Any], company: str | None) -> RawJob:
        job_id = str(item["JobId"])
        location = item.get("JobLocation") or {}
        url = f"https://{HOST}/Recruiting/Jobs/Details/{job_id}"
        return RawJob(
            external_id=job_id,
            title=str(item.get("JobTitle") or "").strip(),
            company=company,
            location_raw=item.get("LocationName") or None,
            city=location.get("City") or None,
            region=location.get("State") or None,
            country=location.get("Country") or None,
            posted_at=parse_datetime(item.get("PublishedDate")),
            source_url=url,
            apply_url=f"https://{HOST}/Recruiting/Jobs/Apply/{job_id}",
        )
