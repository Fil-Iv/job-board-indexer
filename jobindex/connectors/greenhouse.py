"""Greenhouse Job Board API.

Docs: https://developers.greenhouse.io/job-board.html
List:  GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true
       (one request returns every job with description and pay ranges; no pagination)
Board: GET https://boards-api.greenhouse.io/v1/boards/{token} -> {"name": ...}

URL formats recognised (``{t}`` = board token):
    boards.greenhouse.io/{t}[/jobs/{id}]          job-boards.greenhouse.io/{t}[/jobs/{id}]
    boards.eu.greenhouse.io/{t}                   job-boards.eu.greenhouse.io/{t}
    boards.greenhouse.io/embed/job_board?for={t}  boards.greenhouse.io/embed/job_board/js?for={t}
    boards.greenhouse.io/embed/job_app?for={t}&token={id}
    boards-api.greenhouse.io/v1/boards/{t}/...    api.greenhouse.io/v1/boards/{t}/...
Custom-domain pages: any of the above embedded in the page, or
``Grnhse.Settings.boardToken = "{t}"``.
"""

import re
from decimal import Decimal
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.normalize import (
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    parse_location,
    split_url,
    unescape_html,
)

API = "https://boards-api.greenhouse.io/v1/boards"
_BOARD_HOSTS = {
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
}
_API_HOSTS = {"boards-api.greenhouse.io", "api.greenhouse.io", "boards-api.eu.greenhouse.io"}
_RESERVED = {"embed", "v1", "jobs", "api", "robots.txt", "favicon.ico", "assets"}
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


@register
class GreenhouseConnector(BaseConnector):
    platform = "greenhouse"
    html_fingerprints = (
        re.compile(r"""Grnhse\.Settings\.boardToken\s*=\s*["'](?P<id>[A-Za-z0-9_-]+)["']"""),
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, query = split_url(url)
        token: str | None = None
        if host in _BOARD_HOSTS | _API_HOSTS and "for" in query:
            token = query["for"][0]  # embed URLs
        elif host in _API_HOSTS and segments[:2] == ["v1", "boards"] and len(segments) >= 3:
            token = segments[2]
        elif host in _BOARD_HOSTS and segments and segments[0] not in _RESERVED:
            token = segments[0]
        if token and _TOKEN.match(token):
            return token.lower()
        return None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://job-boards.greenhouse.io/{board_identifier}"

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        data = await self.http.get_json(f"{API}/{board_identifier}")
        name = data.get("name") if isinstance(data, dict) else None
        return str(name) if name else None

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self.http.get_json(
            f"{API}/{board_identifier}/jobs",
            params={"content": "true", "pay_transparency": "true"},
        )
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ConnectorError("greenhouse: response has no 'jobs' list")
        jobs = data["jobs"]
        total = (data.get("meta") or {}).get("total")
        if isinstance(total, int) and total != len(jobs):
            raise ConnectorError(f"greenhouse: meta.total={total} but got {len(jobs)} jobs")
        return [self._map(job) for job in jobs]

    @staticmethod
    def _map(job: dict[str, Any]) -> RawJob:
        location_raw = (job.get("location") or {}).get("name") or None
        offices = [o for o in job.get("offices") or [] if o.get("location")]
        # A single office has a precise "City, Region, Country" string; prefer it.
        loc = parse_location(offices[0]["location"]) if len(offices) == 1 else None

        employment_type = None
        for meta in job.get("metadata") or []:
            name = str(meta.get("name") or "").lower()
            if "employment type" in name or "time type" in name or "job type" in name:
                value = meta.get("value")
                employment_type = normalize_employment_type(
                    value if isinstance(value, str) else None
                )
                break

        salary_min = salary_max = None
        currency = period = None
        ranges = job.get("pay_input_ranges") or []
        if ranges:
            pay = ranges[0]
            if pay.get("min_cents") is not None:
                salary_min = Decimal(pay["min_cents"]) / 100
            if pay.get("max_cents") is not None:
                salary_max = Decimal(pay["max_cents"]) / 100
            currency = pay.get("currency_type")
            period = normalize_salary_period(pay.get("title"))

        url = job.get("absolute_url")
        return RawJob(
            external_id=str(job["id"]),
            title=str(job.get("title") or "").strip(),
            company=job.get("company_name"),
            description_html=unescape_html(job.get("content")),
            location_raw=location_raw,
            city=loc.city if loc else None,
            region=loc.region if loc else None,
            country=loc.country if loc else None,
            employment_type=employment_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            salary_period=period,
            posted_at=parse_datetime(job.get("first_published") or job.get("updated_at")),
            source_url=url,
            apply_url=url,
        )
