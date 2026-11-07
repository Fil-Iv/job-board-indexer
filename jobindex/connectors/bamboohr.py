"""BambooHR careers site (public JSON behind every ``{company}.bamboohr.com/careers`` page).

List:   GET https://{company}.bamboohr.com/careers/list
        -> {"meta": {"totalCount": n}, "result": [{id, jobOpeningName, departmentLabel,
            employmentStatusLabel, location{city,state}, atsLocation{country,state,city},
            isRemote, locationType}]}      (every open job in one response; no descriptions)
Detail: GET https://{company}.bamboohr.com/careers/{id}/detail
        -> {"result": {"jobOpening": {description, compensation, datePosted, location
            {..., addressCountry}, atsLocation, locationType, jobOpeningShareUrl, ...}}}
        (404 once the opening is closed)

Board identifier: the BambooHR subdomain, e.g. ``scribd``.

Quirks (verified 2026-11-07):
* Unknown subdomains answer HTTP 200 with an HTML marketing page, not 404: a non-JSON
  list response is an error, never "0 jobs".
* ``locationType``: "0" on-site, "1" remote, "2" hybrid.
* ``compensation`` is free text ("110,000 - 120,000"), parsed with the shared salary
  parser; the currency/period are often missing.

URL formats recognised:
    {company}.bamboohr.com/careers[/{id}[/detail]]   {company}.bamboohr.com/careers/list
    {company}.bamboohr.com/jobs[/view.php?id=..]     {company}.bamboohr.com/jobs/embed2.php
Custom-domain career pages: the embed script / links to the above (generic HTML detection).
"""

import re
from dataclasses import replace
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    normalize_employment_type,
    parse_datetime,
    parse_salary,
    split_url,
)

_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_RESERVED = {"www", "app", "api", "help", "status", "marketplace", "partners", "blog"}
_LOCATION_TYPE = {"0": RemoteType.ONSITE, "1": RemoteType.REMOTE, "2": RemoteType.HYBRID}


def _location(opening: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(city, region, country) — ``atsLocation`` (structured) wins over ``location``."""
    ats = opening.get("atsLocation") or {}
    loc = opening.get("location") or {}
    city = ats.get("city") or loc.get("city") or None
    region = ats.get("state") or ats.get("province") or loc.get("state") or None
    country = ats.get("country") or loc.get("addressCountry") or None
    return city, region, country


def _remote_type(opening: dict[str, Any]) -> RemoteType:
    if opening.get("isRemote") is True:
        return RemoteType.REMOTE
    return _LOCATION_TYPE.get(str(opening.get("locationType")), RemoteType.UNKNOWN)


@register
class BambooHRConnector(BaseConnector):
    platform = "bamboohr"
    needs_detail = True
    detail_cache_key = ("title", "location_raw")
    detail_fields = (
        "description_html",
        "description_text",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "posted_at",
        "country",
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if not host.endswith(".bamboohr.com"):
            return None
        sub = host.removesuffix(".bamboohr.com")
        if "." in sub or sub in _RESERVED or not _SUBDOMAIN.match(sub):
            return None
        if segments and segments[0] not in ("careers", "jobs"):
            return None  # e.g. the login app at {company}.bamboohr.com/home
        return sub

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.bamboohr.com/careers"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = f"https://{board_identifier}.bamboohr.com/careers/list"
        response = await self.http.get(url)
        try:
            data = response.json()
        except ValueError:
            # Unknown accounts get a 200 HTML page: that is "no such board", not "no jobs".
            raise ConnectorError(f"bamboohr: {url} did not return JSON") from None
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list):
            raise ConnectorError("bamboohr: list response has no 'result' list")
        total = (data.get("meta") or {}).get("totalCount")
        if isinstance(total, int) and total != len(result):
            raise ConnectorError(f"bamboohr: totalCount={total} but {len(result)} results")
        return [self._map_list(o, board_identifier) for o in result]

    @staticmethod
    def _map_list(opening: dict[str, Any], board_identifier: str) -> RawJob:
        job_id = str(opening["id"])
        city, region, country = _location(opening)
        location_raw = ", ".join(p for p in (city, region, country) if p) or None
        url = f"https://{board_identifier}.bamboohr.com/careers/{job_id}"
        return RawJob(
            external_id=job_id,
            title=str(opening.get("jobOpeningName") or "").strip(),
            location_raw=location_raw,
            city=city,
            region=region,
            country=country,
            remote_type=_remote_type(opening),
            employment_type=normalize_employment_type(
                opening.get("employmentType") or opening.get("employmentStatusLabel")
            ),
            source_url=url,
            apply_url=url,
        )

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = f"https://{board_identifier}.bamboohr.com/careers/{job.external_id}/detail"
        try:
            data = await self.http.get_json(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None  # closed between list and detail request
            raise
        opening = ((data or {}).get("result") or {}).get("jobOpening")
        if not isinstance(opening, dict):
            raise ConnectorError("bamboohr: detail response has no 'jobOpening'")
        if opening.get("jobOpeningStatus") not in (None, "Open"):
            return None
        salary = parse_salary(opening.get("compensation"))
        _, _, country = _location(opening)
        return replace(
            job,
            description_html=opening.get("description") or None,
            description_text=None,
            country=job.country or country,
            salary_min=salary.min if salary else None,
            salary_max=salary.max if salary else None,
            salary_currency=salary.currency if salary else None,
            salary_period=salary.period if salary else None,
            posted_at=parse_datetime(opening.get("datePosted")),
        )
