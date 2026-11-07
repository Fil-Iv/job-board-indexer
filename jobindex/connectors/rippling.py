"""Rippling Recruiting (public job-board API behind ``ats.rippling.com``).

List:   GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
        -> [{uuid, name, department{label}, url, workLocation{label}}]
        One row per (job, location): a job open in 7 cities appears 7 times. Everything in
        one response; no descriptions.
Detail: GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}
        -> {uuid, name, description{company, role}, workLocations[], employmentType{label,id},
            createdOn, payRangeDetails[], companyName, url, ...}   (404 once closed)

Board identifier: the board slug, e.g. ``vulcanforms``. Unknown slugs answer 404.

Not parsed (verified 2026-11-07): ``payRangeDetails`` was empty on every job checked
(VulcanForms, Carbon Health), so its shape is unknown; salaries in the description text are
still found by the shared normaliser.

URL formats recognised:
    ats.rippling.com/[{locale}/]{slug}/jobs[/{uuid}[/apply]]
    api.rippling.com/platform/api/ats/v1/board/{slug}/jobs[/...]
Custom-domain career pages: links/iframes to the above (generic HTML detection).
"""

import re
from dataclasses import replace
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.normalize import normalize_employment_type, parse_datetime, split_url

API = "https://api.rippling.com/platform/api/ats/v1/board"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_LOCALE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_RESERVED = {"jobs", "api", "static", "_next"}


def _locations(labels: list[str]) -> str | None:
    return "; ".join(dict.fromkeys(label for label in labels if label)) or None


@register
class RipplingConnector(BaseConnector):
    platform = "rippling"
    needs_detail = True
    detail_cache_key = ("title", "location_raw")
    detail_fields = (
        "company",
        "description_html",
        "description_text",
        "employment_type",
        "posted_at",
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host == "ats.rippling.com":
            if lowered and _LOCALE.match(lowered[0]) and len(lowered) > 1:
                lowered = lowered[1:]
            slug = lowered[0] if lowered else ""
        elif host == "api.rippling.com":
            try:
                slug = lowered[lowered.index("board") + 1]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://ats.rippling.com/{board_identifier}/jobs"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self.http.get_json(f"{API}/{board_identifier}/jobs")
        if not isinstance(data, list):
            raise ConnectorError("rippling: list response is not a JSON array")
        by_uuid: dict[str, list[dict[str, Any]]] = {}
        for row in data:
            if not isinstance(row, dict) or not row.get("uuid"):
                raise ConnectorError("rippling: list row without 'uuid'")
            by_uuid.setdefault(str(row["uuid"]), []).append(row)
        return [self._map_list(uuid, rows, board_identifier) for uuid, rows in by_uuid.items()]

    @staticmethod
    def _map_list(uuid: str, rows: list[dict[str, Any]], board_identifier: str) -> RawJob:
        first = rows[0]
        labels = [str((r.get("workLocation") or {}).get("label") or "") for r in rows]
        url = first.get("url") or f"https://ats.rippling.com/{board_identifier}/jobs/{uuid}"
        return RawJob(
            external_id=uuid,
            title=str(first.get("name") or "").strip(),
            location_raw=_locations(labels),
            source_url=url,
            apply_url=f"{url}/apply",
        )

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        try:
            data = await self.http.get_json(f"{API}/{board_identifier}/jobs/{job.external_id}")
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None  # closed between list and detail request
            raise
        if not isinstance(data, dict) or "description" not in data:
            raise ConnectorError("rippling: detail response has no 'description'")
        description = data.get("description") or {}
        if isinstance(description, dict):  # job first, "about us" last
            parts = [description.get("role"), description.get("company")]
        else:
            parts = [description]
        html = "\n".join(p for p in parts if isinstance(p, str) and p.strip())
        employment = data.get("employmentType") or {}
        return replace(
            job,
            company=data.get("companyName") or job.company,
            description_html=html or None,
            description_text=None,
            # "id" is the readable form ("Salaried, full-time"); "label" is a code.
            employment_type=normalize_employment_type(employment.get("id"))
            or normalize_employment_type(employment.get("label")),
            posted_at=parse_datetime(data.get("createdOn")),
        )
