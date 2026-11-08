"""Ashby Posting API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
List: GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
      (one request returns every listed job with descriptions; no pagination)

URL formats recognised (``{name}`` = job board name):
    jobs.ashbyhq.com/{name}[/{job-id}[/application]]    jobs.ashbyhq.com/{name}/embed
    api.ashbyhq.com/posting-api/job-board/{name}
Custom-domain pages: links to / embeds of the hosted board in the page HTML.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    parse_salary,
    remote_type_from_value,
    split_url,
)

API = "https://api.ashbyhq.com/posting-api/job-board"
_RESERVED = {"api", "b", "meeting", "embed", "static", "favicon.ico", "robots.txt"}
_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,100}$")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@register
class AshbyConnector(BaseConnector):
    platform = "ashby"
    company_name_sources = (re.compile(r"^(?P<name>.+?) Jobs$"),)

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        name: str | None = None
        if host == "jobs.ashbyhq.com" and segments and segments[0] not in _RESERVED:
            name = segments[0]
        elif host == "api.ashbyhq.com" and segments[:2] == ["posting-api", "job-board"]:
            name = segments[2] if len(segments) >= 3 else None
        if name and _NAME.match(name):
            return name
        return None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://jobs.ashbyhq.com/{board_identifier}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self.http.get_json(
            f"{API}/{board_identifier}", params={"includeCompensation": "true"}
        )
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ConnectorError("ashby: response has no 'jobs' list")
        return [self._map(j) for j in data["jobs"] if j.get("isListed", True)]

    @staticmethod
    def _map(j: dict[str, Any]) -> RawJob:
        address = (j.get("address") or {}).get("postalAddress") or {}
        locations = [j.get("location")] + [
            s.get("location") for s in j.get("secondaryLocations") or []
        ]
        location_raw = "; ".join(loc for loc in locations if loc) or None

        remote_type = remote_type_from_value(j.get("workplaceType"))
        if remote_type is RemoteType.UNKNOWN and j.get("isRemote"):
            remote_type = RemoteType.REMOTE

        salary_min = salary_max = None
        currency = period = None
        compensation = j.get("compensation") or {}
        for component in compensation.get("summaryComponents") or []:
            if component.get("compensationType") == "Salary":
                salary_min = _decimal(component.get("minValue"))
                salary_max = _decimal(component.get("maxValue"))
                currency = component.get("currencyCode")
                period = normalize_salary_period(component.get("interval"))
                break
        else:
            parsed = parse_salary(compensation.get("scrapeableCompensationSalarySummary"))
            if parsed is not None:
                salary_min, salary_max = parsed.min, parsed.max
                currency, period = parsed.currency, parsed.period

        return RawJob(
            external_id=str(j["id"]),
            title=str(j.get("title") or "").strip(),
            description_html=j.get("descriptionHtml") or None,
            description_text=j.get("descriptionPlain") or None,
            location_raw=location_raw,
            city=address.get("addressLocality") or None,
            region=address.get("addressRegion") or None,
            country=address.get("addressCountry") or None,
            remote_type=remote_type,
            employment_type=normalize_employment_type(j.get("employmentType")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            salary_period=period,
            posted_at=parse_datetime(j.get("publishedAt")),
            source_url=j.get("jobUrl"),
            apply_url=j.get("applyUrl"),
        )
