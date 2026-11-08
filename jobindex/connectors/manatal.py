"""Manatal career pages (``www.careers-page.com/{slug}``; public "open" API).

List: GET https://api.manatal.com/open/v3/career-page/{slug}/jobs/
          ?page={n}&page_size=100&ordering=created_at
      -> {"count", "next", "previous", "results": [{id, hash, position_name, description
          (HTML), organization_name, city, state, country, location_display, is_remote,
          contract_details, is_salary_visible, ...}]}
      DRF pagination; the default page size is 10, ``page_size=100`` is honoured
      (verified 2026-11-07). ``next`` points at core.api.manatal.com; we keep paging the
      same host by page number instead. Unknown slugs answer 404.

      Ordering matters: the default order is not stable across pages. On MR.DIY (2260 jobs,
      2026-11-07) it listed 3 jobs twice at the page 1/2 boundary and never listed 3
      others, identically on every run. ``ordering=created_at`` (honoured; ``ordering=id``
      is ignored) returns every job exactly once, and new jobs land on the last page
      instead of shifting the earlier ones.

Board identifier: the career-page slug, e.g. ``eastwest-bank``.
Job page: https://www.careers-page.com/{slug}/job/{hash}

Not parsed: salaries. ``is_salary_visible`` was false on every job checked (EastWest Bank,
MR.DIY), so the visible salary fields are unknown; salaries in the description text are
still found by the shared normaliser.

URL formats recognised:
    [www.]careers-page.com/{slug}[/job/{hash}[/apply]]     {slug}.careers-page.com[/...]
    api.manatal.com/open/v3/career-page/{slug}/jobs/
Custom-domain career pages: links/iframes to the above (generic HTML detection).
"""

import logging
import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, split_url

log = logging.getLogger(__name__)

API = "https://api.manatal.com/open/v3/career-page"
PAGE_SIZE = 100
MAX_PAGES = 500  # 50k jobs
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_RESERVED = {"www", "api", "app", "static", "help", "jobs", "job", "login", "signup"}


@register
class ManatalConnector(BaseConnector):
    platform = "manatal"
    company_name_sources = (re.compile(r"^-?\s*(?P<name>.+?) \| Career Page$"),)

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        slug = ""
        if host in ("careers-page.com", "www.careers-page.com"):
            slug = lowered[0] if lowered else ""
        elif host.endswith(".careers-page.com"):
            slug = host.removesuffix(".careers-page.com")
        elif host in ("api.manatal.com", "core.api.manatal.com") and "career-page" in lowered:
            index = lowered.index("career-page") + 1
            slug = lowered[index] if index < len(lowered) else ""
        else:
            return None
        if "." in slug or slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://www.careers-page.com/{board_identifier}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        rows, count = await self._list(board_identifier)
        if count is not None and len(rows) != count:
            log.info("manatal %s: got %d of %d jobs, re-paging", board_identifier, len(rows), count)
            rows, count = await self._list(board_identifier)
            if count is not None and len(rows) != count:
                raise ConnectorError(
                    f"manatal: got {len(rows)} of {count} jobs; refusing a partial result"
                )
        return [self._map(r, board_identifier) for r in rows.values()]

    async def _list(self, board_identifier: str) -> tuple[dict[str, dict[str, Any]], int | None]:
        rows: dict[str, dict[str, Any]] = {}
        count: int | None = None
        for page in range(1, MAX_PAGES + 1):
            data = await self.http.get_json(
                f"{API}/{board_identifier}/jobs/"
                f"?page={page}&page_size={PAGE_SIZE}&ordering=created_at"
            )
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise ConnectorError("manatal: response has no 'results' list")
            if isinstance(data.get("count"), int):
                count = data["count"]
            for row in results:
                if row.get("id") is not None:
                    rows[str(row["id"])] = row
            if not data.get("next") or not results:
                return rows, count
        raise ConnectorError(f"manatal: more than {MAX_PAGES} pages; refusing a partial result")

    @staticmethod
    def _map(row: dict[str, Any], board_identifier: str) -> RawJob:
        # is_remote False/None means "not stated", not on-site.
        remote_type = RemoteType.REMOTE if row.get("is_remote") is True else RemoteType.UNKNOWN
        job_hash = row.get("hash") or row["id"]
        url = f"https://www.careers-page.com/{board_identifier}/job/{job_hash}"
        return RawJob(
            external_id=str(row["id"]),
            title=str(row.get("position_name") or "").strip(),
            description_html=row.get("description") or None,
            location_raw=row.get("location_display") or None,
            city=row.get("city") or None,
            region=row.get("state") or None,
            country=row.get("country") or None,
            remote_type=remote_type,
            employment_type=normalize_employment_type(row.get("contract_details")),
            source_url=url,
            apply_url=f"{url}/apply",
        )
