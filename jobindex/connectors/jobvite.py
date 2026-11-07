"""Jobvite career sites (``jobs.jobvite.com/{company}``): paged search + job pages (HTML).

List:   GET https://jobs.jobvite.com/{company}/search/?p={n}   (n = 0, 1, ...)
        Server-rendered table, 50 jobs per page: ``td.jv-job-list-name a`` links to
        ``/{company}/job/{id}``, ``td.jv-job-list-location``, and
        ``<div class="jv-pagination-text">1-50 of 249</div>`` (also on one-page boards).
        Pages are read until the reported total is reached; any other count is an error.
        A board without open jobs says "No results found." and has no counter.
        The default ``/{company}/jobs`` page is NOT complete: it groups jobs by category
        and showed 134 of Nutanix's 249 jobs (2026-11-07). There is no sitemap and no
        public JSON API; robots.txt is 404 (everything allowed).
Detail: GET https://jobs.jobvite.com/{company}/job/{id}  (only for new/changed jobs)
        ``.jv-job-detail-description`` (HTML) and ``.jv-job-detail-meta`` (category,
        location). Some tenants (webmd, heifer) also embed a JSON-LD JobPosting with
        hiringOrganization, employmentType, datePosted and baseSalary, which is used when
        present; others (nutanix) do not, so JSON-LD cannot be the only source.
        Unknown companies and closed jobs redirect away (to www.jobvite.com or the job
        list): the job is gone.

Board identifier: the company slug, e.g. ``nutanix``.

URL formats recognised:
    jobs.jobvite.com/{company}[/jobs|/search|/job/{id}[/apply]|/jobAlerts]
    jobs.jobvite.com/careers/{company}/...
Custom-domain career pages: links to the above (generic HTML detection).

Verified on webmd, heifer and nutanix (2026-11-07).
"""

import re
from dataclasses import replace
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.normalize import split_url
from jobindex.schemaorg import extract_job_postings, to_raw_job

HOST = "jobs.jobvite.com"
MAX_PAGES = 200  # x 50 jobs
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_RESERVED = {"careers", "__assets__", "api", "static"}
_TOTAL = re.compile(r"\d+\s*-\s*\d+\s+of\s+(\d+)")
_JOB_LINK = re.compile(r"^/(?P<slug>[^/]+)/job/(?P<id>[A-Za-z0-9]+)/?$")


def _clean(text: str) -> str:
    return " ".join(text.split())


def parse_search_page(html: str, company: str) -> tuple[list[RawJob], int | None]:
    """Jobs listed on one search page and the total the page reports (``None`` if the
    page is not a Jobvite search page)."""
    tree = LexborHTMLParser(html)
    jobs = []
    for row in tree.css("tr"):
        link = row.css_first("td.jv-job-list-name a")
        match = _JOB_LINK.match(link.attributes.get("href") or "") if link else None
        if link is None or match is None or match["slug"].lower() != company:
            continue
        location = row.css_first("td.jv-job-list-location")
        url = f"https://{HOST}/{company}/job/{match['id']}"
        jobs.append(
            RawJob(
                external_id=match["id"],
                title=_clean(link.text()),
                location_raw=(_clean(location.text()) or None) if location else None,
                source_url=url,
                apply_url=f"{url}/apply",
            )
        )
    counter = tree.css_first(".jv-pagination-text")
    total = _TOTAL.search(counter.text()) if counter else None
    if total is not None:
        return jobs, int(total.group(1))
    body = tree.css_first(".jv-page-body")
    if not jobs and body is not None and "no results found" in body.text().lower():
        return jobs, 0
    return jobs, None


@register
class JobviteConnector(BaseConnector):
    platform = "jobvite"
    needs_detail = True
    detail_fields = (
        "description_html",
        "description_text",
        "company",
        "employment_type",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "posted_at",
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host != HOST or not lowered:
            return None
        slug = lowered[1] if lowered[0] == "careers" and len(lowered) > 1 else lowered[0]
        if slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}/jobs"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        jobs: dict[str, RawJob] = {}
        total: int | None = None
        page = 0
        while total is None or len(jobs) < total:
            if page >= MAX_PAGES:
                raise ConnectorError(f"jobvite: over {MAX_PAGES} pages for {board_identifier}")
            url = f"https://{HOST}/{board_identifier}/search/?p={page}"
            response = await self.http.get(url)
            found, page_total = parse_search_page(response.text, board_identifier)
            if page_total is None:
                raise ConnectorError(f"jobvite: {url} is not a job list (unknown company?)")
            if total is not None and page_total != total:
                raise ConnectorError(f"jobvite: job count changed while paging {url}")
            total = page_total
            new = [job for job in found if job.external_id not in jobs]
            if not new and len(jobs) < total:
                raise ConnectorError(
                    f"jobvite: {board_identifier} reports {total} jobs, page {page} added "
                    f"none ({len(jobs)} so far)"
                )
            jobs.update((job.external_id, job) for job in new)
            page += 1
        if len(jobs) != total:
            raise ConnectorError(f"jobvite: {board_identifier} reports {total}, got {len(jobs)}")
        return list(jobs.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = job.source_url or f"https://{HOST}/{board_identifier}/job/{job.external_id}"
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        _, segments, _ = split_url(str(response.url))
        if segments[1:3] != ["job", job.external_id]:
            return None  # redirected away: the job is closed
        tree = LexborHTMLParser(response.text)
        description = tree.css_first(".jv-job-detail-description")
        if description is None:
            raise ConnectorError(f"jobvite: no job description on {url}; template changed?")
        detail = replace(job, description_html=(description.inner_html or "").strip() or None)
        postings = extract_job_postings(response.text)
        return _with_jsonld(detail, postings[0]) if postings else detail


def _with_jsonld(job: RawJob, item: dict[str, Any]) -> RawJob:
    """Take company, employment type, salary and date from the page's JobPosting."""
    data = to_raw_job(item, external_id=job.external_id, source_url=job.source_url)
    return replace(
        job,
        company=data.company or job.company,
        employment_type=data.employment_type or job.employment_type,
        salary_min=data.salary_min,
        salary_max=data.salary_max,
        salary_currency=data.salary_currency,
        salary_period=data.salary_period,
        posted_at=data.posted_at,
    )
