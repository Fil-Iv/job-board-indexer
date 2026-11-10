"""Polymer job boards (``jobs.polymer.co/{slug}``; public JSON API behind the board).

List:   GET https://api.polymer.co/v1/hire/organizations/{slug}/jobs?page={n}   (n >= 1)
        -> {items: [{id, title, city, state_region, country, display_location,
                     kind_pretty, published_at, job_post_url, remoteness_pretty
                     ("Remote" | "No remote" | ...), salary_pretty ("95K - 140K USD a
                     year"), job_category_name}],
            meta: {page, count, total, is_last, next_page, organization_name}}
        ``meta.count`` is the number of jobs on the board and ``meta.total`` the number of
        pages (not jobs). ``is_last``/``next_page`` are not reliable: past the end they say
        "not last, next page 3" with no items. So pages 1..total are read and the result
        must contain exactly ``count`` jobs. Without ``page`` the API answers 422, as it
        does for unknown organizations.
Detail: GET https://api.polymer.co/v1/hire/organizations/{slug}/jobs/{id}
        -> same fields + description (HTML), kind (full_time, ...), archived_at.
        Only for new or changed jobs (``needs_detail``).

Board identifier: the board slug, e.g. ``themis``.

URL formats recognised:
    jobs.polymer.co/{slug}[/{job-id}]
    api.polymer.co/v1/hire/organizations/{slug}/jobs[/{job-id}]
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on themis and invicta-software (2026-11-07).
"""

import re
from dataclasses import replace
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, parse_datetime, parse_salary, split_url

API = "https://api.polymer.co/v1/hire/organizations"
MAX_PAGES = 200
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_RESERVED = {"packs", "assets", "api", "users", "candidates"}


def _remote(value: str | None) -> RemoteType:
    text = (value or "").lower()
    if not text:
        return RemoteType.UNKNOWN
    if "no remote" in text or "on-site" in text or "onsite" in text:
        return RemoteType.ONSITE
    if "hybrid" in text:
        return RemoteType.HYBRID
    if "remote" in text:
        return RemoteType.REMOTE
    return RemoteType.UNKNOWN


@register
class PolymerConnector(BaseConnector):
    platform = "polymer"
    detail_fields = ("description_html", "description_text", "employment_type")
    needs_detail = True

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host == "jobs.polymer.co" and lowered:
            slug = lowered[0]
        elif (
            host == "api.polymer.co"
            and lowered[:3] == ["v1", "hire", "organizations"]
            and len(lowered) > 3
        ):
            slug = lowered[3]
        else:
            return None
        if slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://jobs.polymer.co/{board_identifier}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = f"{API}/{board_identifier}/jobs"
        jobs: dict[str, RawJob] = {}
        expected: int | None = None
        pages = 1
        page = 1
        while page <= pages:
            data = await self.http.get_json(url, params={"page": page})
            items = data.get("items") if isinstance(data, dict) else None
            meta = data.get("meta") if isinstance(data, dict) else None
            if not isinstance(items, list) or not isinstance(meta, dict):
                raise ConnectorError(f"polymer: unexpected response for {board_identifier}")
            if expected is None:
                expected = int(meta.get("count") or 0)
                pages = int(meta.get("total") or 0)
                if pages > MAX_PAGES:
                    raise ConnectorError(f"polymer: {pages} pages for {board_identifier}")
            company = meta.get("organization_name") or None
            for item in items:
                job = self._map(item, company)
                jobs[job.external_id] = job
            page += 1
        if len(jobs) != expected:
            raise ConnectorError(
                f"polymer: {board_identifier} reports {expected} jobs, got {len(jobs)}"
            )
        return list(jobs.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = f"{API}/{board_identifier}/jobs/{job.external_id}"
        try:
            data = await self.http.get_json(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if not isinstance(data, dict) or str(data.get("id")) != job.external_id:
            raise ConnectorError(f"polymer: unexpected detail response for {url}")
        if data.get("archived_at"):
            return None
        return replace(
            job,
            description_html=data.get("description") or None,
            employment_type=normalize_employment_type(data.get("kind")) or job.employment_type,
        )

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        data = await self.http.get_json(f"{API}/{board_identifier}/jobs", params={"page": 1})
        name = (data.get("meta") or {}).get("organization_name") if isinstance(data, dict) else None
        return str(name) if name else None

    @staticmethod
    def _map(item: dict[str, Any], company: str | None) -> RawJob:
        if not item.get("id"):
            raise ConnectorError("polymer: job without 'id'")
        salary = parse_salary(item.get("salary_pretty"))
        url = item.get("job_post_url") or None
        return RawJob(
            external_id=str(item["id"]),
            title=str(item.get("title") or "").strip(),
            company=item.get("organization_name") or company,
            location_raw=item.get("display_location") or None,
            city=item.get("city") or None,
            region=item.get("state_region") or None,
            country=item.get("country") or None,
            remote_type=_remote(item.get("remoteness_pretty")),
            employment_type=normalize_employment_type(item.get("kind_pretty")),
            salary_min=salary.min if salary else None,
            salary_max=salary.max if salary else None,
            salary_currency=salary.currency if salary else None,
            salary_period=salary.period if salary else None,
            posted_at=parse_datetime(item.get("published_at")),
            source_url=url,
            apply_url=url,
        )
