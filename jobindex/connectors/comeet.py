"""Comeet (Spark Hire Recruit) career pages: positions embedded in the hosted board page.

List: GET https://www.comeet.com/jobs/{slug}/{uid}
      The server-rendered page declares ``COMPANY_DATA = {...};`` (name, ...) and
      ``COMPANY_POSITIONS_DATA = [...];``: every published position, with descriptions:
      [{uid, name, department, employment_type, experience_level, workplace_type
        (On-site|Hybrid|Remote), location{name, city, state, country, is_remote},
        custom_fields{details: [{name: "Description"|"Requirements"|..., value: HTML}]},
        url_active_page, url_comeet_hosted_page, time_updated, is_internal}]
      One request per crawl (verified on safefleet/95.001 and vastdata/43.001,
      2026-11-07). An unknown board redirects to the Comeet home page, which has no
      ``COMPANY_POSITIONS_DATA``: that is an error, never "0 jobs".

The documented careers API (``comeet.co/careers-api/2.0/company/{uid}/positions``) needs
a company token; it is not used, since the board page already carries the same data.

There is no posting date (``time_updated`` is the last edit). ``workplace_type`` decides
the remote type (``location.is_remote`` is also true for some hybrid positions).

Board identifier: ``{slug}/{uid}``, e.g. ``safefleet/95.001`` (the company uid has the
form ``XX.XXX``, hex digits).

URL formats recognised:
    www.comeet.com/jobs/{slug}/{uid}[/{position-slug}/{position-uid}]   (also comeet.co)
Custom-domain career pages: links to the above (generic HTML detection).
"""

import json
import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, remote_type_from_value, split_url

_HOSTS = {"comeet.com", "comeet.co"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_COMPANY_UID = re.compile(r"^[0-9a-f]{2}\.[0-9a-f]{3}$")
# ``var COMPANY_POSITIONS_DATA ;`` is declared first, assigned later in the same script.
_POSITIONS = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\])\s*;\s*\n", re.S)
_COMPANY = re.compile(r"COMPANY_DATA\s*=\s*(\{.*?\})\s*;\s*\n", re.S)


def _description(position: dict[str, Any]) -> str | None:
    """The ``custom_fields.details`` sections as one HTML document (one heading each)."""
    details = (position.get("custom_fields") or {}).get("details") or []
    parts = []
    for section in details:
        value = section.get("value") if isinstance(section, dict) else None
        if value:
            name = section.get("name")
            parts.append(f"<h3>{name}</h3>{value}" if name else str(value))
    return "\n".join(parts) or None


def _json(pattern: re.Pattern[str], html: str, what: str) -> Any:
    match = pattern.search(html)
    if match is None:
        raise ConnectorError(f"comeet: no {what} in the board page (unknown board?)")
    try:
        return json.loads(match.group(1))
    except ValueError as exc:
        raise ConnectorError(f"comeet: {what} is not valid JSON: {exc}") from exc


@register
class ComeetConnector(BaseConnector):
    platform = "comeet"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if host not in _HOSTS or len(segments) < 3 or segments[0].lower() != "jobs":
            return None
        slug, uid = segments[1].lower(), segments[2].upper()
        if not _SLUG.match(slug) or not _COMPANY_UID.match(uid.lower()):
            return None
        return f"{slug}/{uid}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://www.comeet.com/jobs/{board_identifier}"

    async def _page(self, board_identifier: str) -> str:
        response = await self.http.get(self.board_url(board_identifier))
        return response.text

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        html = await self._page(board_identifier)
        positions = _json(_POSITIONS, html, "COMPANY_POSITIONS_DATA")
        if not isinstance(positions, list):
            raise ConnectorError("comeet: COMPANY_POSITIONS_DATA is not a list")
        company = (_json(_COMPANY, html, "COMPANY_DATA") or {}).get("name") or None
        return [self._map(p, company) for p in positions if not p.get("is_internal")]

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        name = (_json(_COMPANY, await self._page(board_identifier), "COMPANY_DATA") or {}).get(
            "name"
        )
        return str(name) if name else None

    @staticmethod
    def _map(position: dict[str, Any], company: str | None) -> RawJob:
        if not position.get("uid"):
            raise ConnectorError("comeet: position without 'uid'")
        location = position.get("location") or {}
        remote = remote_type_from_value(position.get("workplace_type"))
        if remote is RemoteType.UNKNOWN and location.get("is_remote"):
            remote = RemoteType.REMOTE
        url = position.get("url_active_page") or position.get("url_comeet_hosted_page") or None
        return RawJob(
            external_id=str(position["uid"]),
            title=str(position.get("name") or "").strip(),
            company=position.get("company_name") or company,
            description_html=_description(position),
            location_raw=location.get("name") or None,
            city=location.get("city") or None,
            region=location.get("state") or None,
            country=location.get("country") or None,
            remote_type=remote,
            employment_type=normalize_employment_type(position.get("employment_type")),
            source_url=url,
            apply_url=url,
        )
