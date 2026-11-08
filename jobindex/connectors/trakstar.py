"""Trakstar Hire (formerly Recruiterbox; public "jsapi" used by the hosted careers pages).

List: GET https://jsapi.recruiterbox.com/v1/openings?client_name={company}&limit=100&offset=n
      -> {"meta": {"offset", "limit", "total"}, "objects": [{id, title, description (HTML),
          location{city, state, country, zipcode}, hosted_url, allows_remote,
          position_type, team, tags, close_date}]}
      ``limit`` up to 100 is honoured (default 20; verified 2026-11-07). Unknown companies
      answer HTTP 400. There is no posting date.

Board identifier: the account name, e.g. ``stax``.

``allows_remote`` is Trakstar's "remote work allowed" switch; jobs with it set are mapped to
remote (the location is then the office the role is attached to).

URL formats recognised:
    {company}.hire.trakstar.com[/jobs/{id}[/...]]     {company}.recruiterbox.com[/jobs/...]
    jsapi.recruiterbox.com/v1/openings?client_name={company}
Custom-domain career pages: the embed widget / links to the above (generic HTML detection).
"""

import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, split_url

API = "https://jsapi.recruiterbox.com/v1/openings"
PAGE_SIZE = 100
MAX_PAGES = 200
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_RESERVED = {"www", "app", "api", "jsapi", "support", "status", "help", "blog", "home"}


@register
class TrakstarConnector(BaseConnector):
    platform = "trakstar"
    company_name_sources = (re.compile(r"^(?P<name>.+?) jobs\s*\|"),)

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, query = split_url(url)
        if host == "jsapi.recruiterbox.com":
            name = (query.get("client_name") or [""])[0].lower()
        elif host.endswith(".hire.trakstar.com"):
            name = host.removesuffix(".hire.trakstar.com")
        elif host.endswith(".recruiterbox.com"):
            name = host.removesuffix(".recruiterbox.com")
        else:
            return None
        if "." in name or name in _RESERVED or not _NAME.match(name):
            return None
        return name

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.hire.trakstar.com"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        rows: dict[str, dict[str, Any]] = {}
        total: int | None = None
        for page in range(MAX_PAGES):
            data = await self.http.get_json(
                f"{API}?client_name={board_identifier}&limit={PAGE_SIZE}&offset={page * PAGE_SIZE}"
            )
            objects = data.get("objects") if isinstance(data, dict) else None
            if not isinstance(objects, list):
                raise ConnectorError("trakstar: response has no 'objects' list")
            meta_total = (data.get("meta") or {}).get("total")
            total = meta_total if isinstance(meta_total, int) else total
            for row in objects:
                if row.get("id"):
                    rows[str(row["id"])] = row
            if len(objects) < PAGE_SIZE or (total is not None and len(rows) >= total):
                break
        else:
            raise ConnectorError(
                f"trakstar: more than {MAX_PAGES} pages; refusing a partial result"
            )
        if total is not None and len(rows) != total:
            raise ConnectorError(
                f"trakstar: got {len(rows)} of {total} openings; refusing a partial result"
            )
        return [self._map(r) for r in rows.values()]

    @staticmethod
    def _map(row: dict[str, Any]) -> RawJob:
        location = row.get("location") or {}
        parts = [location.get(k) for k in ("city", "state", "country")]
        url = row.get("hosted_url") or None
        return RawJob(
            external_id=str(row["id"]),
            title=str(row.get("title") or "").strip(),
            description_html=row.get("description") or None,
            location_raw=", ".join(str(p) for p in parts if p) or None,
            city=location.get("city") or None,
            region=location.get("state") or None,
            country=location.get("country") or None,
            remote_type=RemoteType.REMOTE if row.get("allows_remote") else RemoteType.UNKNOWN,
            employment_type=normalize_employment_type(row.get("position_type")),
            source_url=url,
            apply_url=url,
        )
