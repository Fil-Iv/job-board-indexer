"""Breezy HR careers portal (public JSON feed behind every ``{company}.breezy.hr`` portal).

List: GET https://{company}.breezy.hr/json?verbose=true
      -> [{id, friendly_id, name, url, published_date, type{id,name}, department, salary
           (free text), description (HTML, only with verbose=true), location{city,
           state{id,name}, country{id,name}, is_remote, remote_details{value}, name},
           locations[], company{name}}]
      Every published position in one response; without ``verbose=true`` the descriptions
      are missing (verified 2026-11-07), so no per-job request is needed.

Board identifier: the portal subdomain, e.g. ``kimmel-associates``. Unknown subdomains
answer 404.

URL formats recognised:
    {company}.breezy.hr[/p/{id}-{slug}[/apply]]     {company}.breezy.hr/json
    {company}.breezy.hr/embed/js?...                app.breezy.hr/api/apply/... (no id)
Custom-domain career pages: the embed script / links to the above (generic HTML detection).
"""

import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    normalize_employment_type,
    parse_datetime,
    parse_salary,
    remote_type_from_value,
    split_url,
)

_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_RESERVED = {"www", "app", "api", "help", "marketing", "assets", "attachments", "status"}


def _location_name(location: dict[str, Any]) -> str | None:
    name = location.get("name")
    if name:
        return str(name)
    parts = [location.get("city"), (location.get("state") or {}).get("name")]
    parts.append((location.get("country") or {}).get("name"))
    return ", ".join(str(p) for p in parts if p) or None


def _remote_type(location: dict[str, Any]) -> RemoteType:
    details = (location.get("remote_details") or {}).get("value")
    if details:
        remote = remote_type_from_value(str(details))
        if remote is not RemoteType.UNKNOWN:
            return remote
    return RemoteType.REMOTE if location.get("is_remote") else RemoteType.UNKNOWN


@register
class BreezyConnector(BaseConnector):
    platform = "breezy"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(".breezy.hr"):
            return None
        sub = host.removesuffix(".breezy.hr")
        if "." in sub or sub in _RESERVED or not _SUBDOMAIN.match(sub):
            return None
        return sub

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.breezy.hr"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self.http.get_json(f"https://{board_identifier}.breezy.hr/json?verbose=true")
        if not isinstance(data, list):
            raise ConnectorError("breezy: response is not a JSON array")
        return [self._map(p) for p in data]

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        jobs = await self.fetch_jobs(board_identifier)
        return jobs[0].company if jobs else None

    @staticmethod
    def _map(position: dict[str, Any]) -> RawJob:
        if not position.get("id"):
            raise ConnectorError("breezy: position without 'id'")
        primary = position.get("location") or {}
        others = [loc for loc in position.get("locations") or [] if isinstance(loc, dict)]
        names = [_location_name(primary), *(_location_name(loc) for loc in others)]
        location_raw = "; ".join(dict.fromkeys(n for n in names if n)) or None
        single = len(set(n for n in names if n)) <= 1
        salary = parse_salary(position.get("salary") or None)
        employment = position.get("type") or {}
        url = position.get("url") or None
        return RawJob(
            external_id=str(position["id"]),
            title=str(position.get("name") or "").strip(),
            company=(position.get("company") or {}).get("name") or None,
            description_html=position.get("description") or None,
            location_raw=location_raw,
            # Structured parts only when the job has a single location.
            city=(primary.get("city") or None) if single else None,
            region=((primary.get("state") or {}).get("id") or None) if single else None,
            country=((primary.get("country") or {}).get("id") or None) if single else None,
            remote_type=_remote_type(primary),
            employment_type=normalize_employment_type(employment.get("id"))
            or normalize_employment_type(employment.get("name")),
            salary_min=salary.min if salary else None,
            salary_max=salary.max if salary else None,
            salary_currency=salary.currency if salary else None,
            salary_period=salary.period if salary else None,
            posted_at=parse_datetime(position.get("published_date")),
            source_url=url,
            apply_url=f"{url}/apply" if url else None,
        )
