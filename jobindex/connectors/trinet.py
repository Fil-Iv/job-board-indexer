"""TriNet Hire career pages (``app.trinethire.com/companies/{id}-{slug}/jobs``): HTML.

List:   GET https://app.trinethire.com/companies/{id}/jobs
        Server-rendered ``table.job-list`` with one ``tr.job`` per open job: link
        ``/companies/{id}-{slug}/jobs/{job id}-{slug}``, ``td.type``, ``td.location``.
        All jobs on one page (no pagination, no count), so the table must be present
        (else: error) and the generic job-drop guard (``max_job_drop``) protects against
        a broken page. Unknown companies answer 404. No JSON-LD, no sitemap, no public
        API. robots.txt allows ``*`` (``Content-Signal: search=yes``).
Detail: GET the job URL (only for new/changed jobs; 404 = closed)
        ``h3.job-name``, ``ul.job-meta li`` ("<strong>Type:</strong> Full-time",
        "Salary: $95,000 - $110,000", category, experience) and ``div.job-descr`` (HTML).

Board identifier: the numeric company id (``130621``); the slug is optional in URLs.

URL formats recognised:
    app.trinethire.com/companies/{id}[-{slug}][/jobs[/{job id}-{slug}]]
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on Curative (130621) and Precor (203100), 2026-11-07.
"""

import re
from dataclasses import replace
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.normalize import normalize_employment_type, parse_salary, split_url

HOST = "app.trinethire.com"
_COMPANY = re.compile(r"^(?P<id>\d+)(?:-[a-z0-9-]*)?$")
_JOB_PATH = re.compile(r"^/companies/(?P<company>\d+)(?:-[^/]*)?/jobs/(?P<id>\d+)(?:-[^/]*)?/?$")


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


@register
class TriNetConnector(BaseConnector):
    platform = "trinet"
    needs_detail = True
    detail_fields = (
        "description_html",
        "description_text",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if host != HOST or len(segments) < 2 or segments[0].lower() != "companies":
            return None
        match = _COMPANY.match(segments[1].lower())
        return match["id"] if match else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/companies/{board_identifier}/jobs"

    def max_job_drop(self) -> float | None:
        return get_settings().generic_max_job_drop

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = self.board_url(board_identifier)
        response = await self.http.get(url)
        tree = LexborHTMLParser(response.text)
        table = tree.css_first("table.job-list")
        if table is None:
            raise ConnectorError(f"trinet: no job table on {url}; template changed?")
        title = tree.css_first("title")
        company = _clean(title.text()).removeprefix("Careers at ").strip() if title else ""
        jobs: dict[str, RawJob] = {}
        for row in table.css("tr.job"):
            link = row.css_first("a")
            path = urlsplit((link.attributes.get("href") or "") if link else "").path
            match = _JOB_PATH.match(path)
            if link is None or match is None or match["company"] != board_identifier:
                continue
            kind, location = row.css_first("td.type"), row.css_first("td.location")
            job_url = f"https://{HOST}{path}"
            jobs[match["id"]] = RawJob(
                external_id=match["id"],
                title=_clean(link.text()),
                company=company or None,
                location_raw=(_clean(location.text()) or None) if location else None,
                employment_type=normalize_employment_type(kind.text()) if kind else None,
                source_url=job_url,
                apply_url=job_url,
            )
        return list(jobs.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = job.source_url or ""
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if _JOB_PATH.match(response.url.path) is None:
            return None  # redirected away: closed
        tree = LexborHTMLParser(response.text)
        description = tree.css_first("div.job-descr")
        if description is None:
            raise ConnectorError(f"trinet: no div.job-descr on {url}; template changed?")
        salary = None
        for item in tree.css("ul.job-meta li"):
            text = _clean(item.text())
            if text.lower().startswith("salary:"):
                salary = parse_salary(text.split(":", 1)[1])
        return replace(
            job,
            description_html=(description.inner_html or "").strip() or None,
            salary_min=salary.min if salary else None,
            salary_max=salary.max if salary else None,
            salary_currency=salary.currency if salary else None,
            salary_period=salary.period if salary else None,
        )
