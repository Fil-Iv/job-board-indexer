"""Workable public job widget API.

List: GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
      -> {"name", "description", "jobs": [{shortcode, title, description, locations, ...}]}
      (one request, every published job, with descriptions; no pagination)

Board identifier: the Workable account subdomain, e.g. ``blueground``.

URL formats recognised:
    apply.workable.com/{account}[/j/{shortcode}[/apply]]
    {account}.workable.com[/j/{shortcode}]            (legacy hosted boards)
    apply.workable.com/api/v1/widget/accounts/{account}
    apply.workable.com/api/v3/accounts/{account}/jobs
    www.workable.com/api/accounts/{account}
Custom-domain career pages: links to the hosted board (generic HTML detection). The JS
widget embed (``whr_embed(<numeric id>)``) only exposes a numeric id that the public API
does not accept, so such pages are resolved only if they also link to the board.
"""

import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, parse_datetime, split_url

API = "https://apply.workable.com/api/v1/widget/accounts"
_ACCOUNT = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_RESERVED_SUBDOMAINS = {"www", "apply", "jobs", "help", "resources", "careers", "api", "blog"}
_RESERVED_PATHS = {"j", "api", "careers", "static", "assets", "robots.txt", "favicon.ico"}


@register
class WorkableConnector(BaseConnector):
    platform = "workable"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        account: str | None = None
        if host == "apply.workable.com" and segments:
            if segments[:4] == ["api", "v1", "widget", "accounts"] and len(segments) > 4:
                account = segments[4]
            elif segments[:3] == ["api", "v3", "accounts"] and len(segments) > 3:
                account = segments[3]
            elif segments[0] not in _RESERVED_PATHS:
                account = segments[0]
        elif host == "workable.com" and segments[:2] == ["api", "accounts"] and len(segments) > 2:
            account = segments[2]
        elif host.endswith(".workable.com"):
            sub = host.removesuffix(".workable.com")
            if "." not in sub and sub not in _RESERVED_SUBDOMAINS:
                account = sub
        if account:
            account = account.lower()
            if _ACCOUNT.match(account):
                return account
        return None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://apply.workable.com/{board_identifier}/"

    async def _fetch(self, board_identifier: str) -> dict[str, Any]:
        data = await self.http.get_json(f"{API}/{board_identifier}", params={"details": "true"})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ConnectorError("workable: response has no 'jobs' list")
        return data

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        name = (await self._fetch(board_identifier)).get("name")
        return str(name) if name else None

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self._fetch(board_identifier)
        company = data.get("name") or None
        return [self._map(job, company) for job in data["jobs"]]

    @staticmethod
    def _map(job: dict[str, Any], company: str | None) -> RawJob:
        locations = [loc for loc in job.get("locations") or [] if not loc.get("hidden")]
        first = (
            locations[0]
            if locations
            else {
                "city": job.get("city"),
                "region": job.get("state"),
                "country": job.get("country"),
            }
        )
        location_raw = (
            "; ".join(
                ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
                for loc in (locations or [first])
            )
            or None
        )
        return RawJob(
            external_id=str(job["shortcode"]),
            title=str(job.get("title") or "").strip(),
            company=company,
            description_html=job.get("description") or None,
            location_raw=location_raw,
            city=first.get("city") or None,
            region=first.get("region") or None,
            country=first.get("countryCode") or first.get("country") or None,
            remote_type=RemoteType.REMOTE if job.get("telecommuting") else RemoteType.UNKNOWN,
            employment_type=normalize_employment_type(job.get("employment_type")),
            posted_at=parse_datetime(job.get("published_on") or job.get("created_at")),
            source_url=job.get("url") or job.get("shortlink"),
            apply_url=job.get("application_url"),
        )
