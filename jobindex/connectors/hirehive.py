"""HireHive career sites (``{company}.hirehive.com``; public JSON API).

List: GET https://{company}.hirehive.com/api/v1/jobs
      -> {publishedJobsCount, nextPage (full URL with take/skip, or null),
          jobs: [{id, title, location, stateCode, country{name, code}, salary,
                  description{html, text}, category, type{type, name}, experience,
                  language, publishedDate (unix seconds), createdDate, hostedUrl}]}
      Every published job with descriptions; ``nextPage`` is followed until null and the
      result must contain ``publishedJobsCount`` jobs. One request for boards seen so far
      (``?take=5`` proved the paging, 2026-11-07). Unknown companies answer 404.
      robots.txt allows everything. The board page also embeds a JSON-LD ItemList, not
      used.

The API has no company name (the source name is used) and no remote flag; ``salary`` was
null on every job seen and is parsed as free text when present.

Board identifier: the subdomain, e.g. ``focus-ireland``.

URL formats recognised:
    {company}.hirehive.com[/{job-slug}]
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on focus-ireland, catalyx and iss-recruitment-and-hr-services (2026-11-07).
"""

import re
from datetime import UTC, datetime
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.normalize import normalize_employment_type, parse_salary, split_url

_SUFFIX = ".hirehive.com"
_COMPANY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RESERVED = {"www", "www2", "app", "api", "help", "blog", "status", "hirehive-public-staging"}
MAX_PAGES = 200


def _epoch_seconds(value: Any) -> datetime | None:
    """``publishedDate`` is Unix *seconds* (``normalize.parse_datetime`` reads numbers as
    milliseconds)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@register
class HireHiveConnector(BaseConnector):
    platform = "hirehive"
    company_name_sources = (re.compile(r"Jobs at (?P<name>.+)$"),)

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(_SUFFIX):
            return None
        company = host.removesuffix(_SUFFIX)
        if company in _RESERVED or not _COMPANY.match(company):
            return None
        return company

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}{_SUFFIX}/"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        host = f"{board_identifier}{_SUFFIX}"
        url: str | None = f"https://{host}/api/v1/jobs"
        jobs: dict[str, RawJob] = {}
        expected: int | None = None
        pages = 0
        while url:
            pages += 1
            if pages > MAX_PAGES:
                raise ConnectorError(f"hirehive: over {MAX_PAGES} pages for {board_identifier}")
            data = await self.http.get_json(url)
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise ConnectorError(f"hirehive: unexpected response from {url}")
            if expected is None:
                expected = int(data.get("publishedJobsCount") or 0)
            for item in data["jobs"]:
                job = self._map(item)
                jobs[job.external_id] = job
            url = data.get("nextPage") or None
            if url is not None and split_url(url)[0] != host:
                raise ConnectorError(f"hirehive: nextPage leaves the board: {url}")
        if len(jobs) != expected:
            raise ConnectorError(
                f"hirehive: {board_identifier} reports {expected} jobs, got {len(jobs)}"
            )
        return list(jobs.values())

    @staticmethod
    def _map(item: dict[str, Any]) -> RawJob:
        if not item.get("id"):
            raise ConnectorError("hirehive: job without 'id'")
        description = item.get("description") or {}
        country = item.get("country") or {}
        job_type = item.get("type") or {}
        salary = parse_salary(item["salary"]) if isinstance(item.get("salary"), str) else None
        url = item.get("hostedUrl") or None
        return RawJob(
            external_id=str(item["id"]),
            title=str(item.get("title") or "").strip(),
            description_html=description.get("html") or None,
            description_text=description.get("text") or None,
            location_raw=item.get("location") or None,
            region=item.get("stateCode") or None,
            country=country.get("code") or country.get("name") or None,
            employment_type=normalize_employment_type(job_type.get("type") or job_type.get("name")),
            salary_min=salary.min if salary else None,
            salary_max=salary.max if salary else None,
            salary_currency=salary.currency if salary else None,
            salary_period=salary.period if salary else None,
            posted_at=_epoch_seconds(item.get("publishedDate")),
            source_url=url,
            apply_url=url,
        )
