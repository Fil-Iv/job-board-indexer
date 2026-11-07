"""Workday Recruiting (CXS career-site API — undocumented but public, used by every
Workday career site).

List:   POST https://{host}/wday/cxs/{tenant}/{site}/jobs
        {"limit": 20, "offset": n, "appliedFacets": {}, "searchText": ""}
        -> {"total", "jobPostings": [{title, externalPath, locationsText, postedOn, ...}],
            "facets": [...]}   (no descriptions; 20 is the maximum page size)
Detail: GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}
        -> {"jobPostingInfo": {jobDescription, location, startDate, timeType, ...}}

Board identifier: ``{host}/{tenant}/{site}``, e.g.
``nvidia.wd5.myworkdayjobs.com/nvidia/NVIDIAExternalCareerSite`` or
``wd3.myworkdaysite.com/pwc/Global_Experienced_Careers``.

Pagination quirks (verified 2026-11-07):
* Past the real end, Workday does not return an empty page: it serves page 1 again
  (verified on NVIDIA at offsets 2820..7000). We stop when a page adds no new posting.
* ``total`` is only filled in on the first page (later pages say 0), and on some tenants
  it is capped at 2000 while paging past offset 2000 still returns jobs (NVIDIA: total=2000,
  real count 2810). The real count is the sum of the ``timeType`` facet. We page until a
  short page, de-duplicate, and refuse to return fewer jobs than expected (one retry,
  because offset paging over a list that changes mid-crawl can skip postings).

URL formats recognised:
    {tenant}.wd{n}.myworkdayjobs.com/[{locale}/]{site}[/job/{location}/{slug}_{id}]
    {tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/...
    [{tenant}.]wd{n}.myworkdaysite.com/[{locale}/]recruiting/{tenant}/{site}[/job/...]
Custom-domain career pages: links/iframes to any of the above (generic HTML detection).
"""

import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    detect_remote_type,
    normalize_employment_type,
    parse_datetime,
    remote_type_from_value,
    split_url,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 20  # Workday rejects larger pages
MAX_PAGES = 1000  # 20k postings
_JOBS_HOST = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$")
_SITE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)?(?P<dc>wd\d+)\.myworkdaysite\.com$")
_LOCALE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2,4})?$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_N_LOCATIONS = re.compile(r"^\d+ Locations?$", re.IGNORECASE)


def _split_identifier(board_identifier: str) -> tuple[str, str, str]:
    try:
        host, tenant, site = board_identifier.split("/")
    except ValueError:
        raise ConnectorError(f"workday: bad board identifier {board_identifier!r}") from None
    return host, tenant, site


def _public_base(board_identifier: str) -> str:
    host, tenant, site = _split_identifier(board_identifier)
    if host.endswith(".myworkdaysite.com"):
        return f"https://{host}/recruiting/{tenant}/{site}"
    return f"https://{host}/{site}"


@register
class WorkdayConnector(BaseConnector):
    platform = "workday"
    needs_detail = True
    # The list only has title + a location summary, and "postedOn" is relative ("Posted
    # Today"), so only the title can tell us a posting changed. Everything else comes from
    # the detail request and is reused while the title is unchanged.
    detail_cache_key = ("title",)
    detail_fields = (
        "description_html",
        "description_text",
        "location_raw",
        "city",
        "region",
        "country",
        "remote_type",
        "employment_type",
        "posted_at",
        "apply_url",
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if m := _JOBS_HOST.match(host):
            tenant = m["tenant"]
            if segments[:2] == ["wday", "cxs"] and len(segments) >= 4:
                tenant, site = segments[2], segments[3]
            else:
                rest = segments[1:] if segments and _LOCALE.match(segments[0]) else segments
                site = rest[0] if rest else ""
        elif m := _SITE_HOST.match(host):
            host = f"{m['dc']}.myworkdaysite.com"  # the API lives on the bare wdN host
            if segments[:2] == ["wday", "cxs"] and len(segments) >= 4:
                tenant, site = segments[2], segments[3]
            else:
                rest = segments[1:] if segments and _LOCALE.match(segments[0]) else segments
                if len(rest) < 3 or rest[0] != "recruiting":
                    return None
                tenant, site = rest[1], rest[2]
        else:
            return None
        if site in {"", "job", "wday", "login"} or not (_NAME.match(tenant) and _NAME.match(site)):
            return None
        return f"{host}/{tenant}/{site}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return _public_base(board_identifier)

    def _api(self, board_identifier: str) -> str:
        host, tenant, site = _split_identifier(board_identifier)
        return f"https://{host}/wday/cxs/{tenant}/{site}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        postings, expected = await self._fetch_all(board_identifier)
        if expected is not None and len(postings) < expected:
            log.info(
                "workday %s: got %d of %d postings, re-paging once",
                board_identifier,
                len(postings),
                expected,
            )
            postings, expected = await self._fetch_all(board_identifier)
            if expected is not None and len(postings) < expected:
                raise ConnectorError(
                    f"workday: got {len(postings)} of {expected} postings; "
                    "refusing a partial result"
                )
        base = _public_base(board_identifier)
        return [self._map_list(p, base) for p in postings.values()]

    async def _fetch_all(
        self, board_identifier: str
    ) -> tuple[dict[str, dict[str, Any]], int | None]:
        url = f"{self._api(board_identifier)}/jobs"
        postings: dict[str, dict[str, Any]] = {}
        expected: int | None = None
        for page in range(MAX_PAGES):
            response = await self.http.request(
                "POST",
                url,
                json={
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "appliedFacets": {},
                    "searchText": "",
                },
                headers={"Accept": "application/json"},
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise ConnectorError("workday: list response is not JSON") from exc
            batch = data.get("jobPostings") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                raise ConnectorError("workday: response has no 'jobPostings' list")
            if page == 0:
                expected = _expected_total(data)
            before = len(postings)
            for posting in batch:
                path = posting.get("externalPath")
                if path:
                    postings[_external_id(path)] = posting
            # End of list: a short page, or — Workday's quirk past the real end — a full page
            # that only repeats postings we already have (it wraps around to page 1).
            if len(batch) < PAGE_SIZE or len(postings) == before:
                return postings, expected
            if expected is not None and len(postings) >= expected:
                return postings, expected
        raise ConnectorError(f"workday: more than {MAX_PAGES} pages; refusing a partial result")

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        base = _public_base(board_identifier)
        if not job.source_url or not job.source_url.startswith(base):
            raise ConnectorError(f"workday: cannot derive detail path from {job.source_url!r}")
        path = job.source_url[len(base) :]
        try:
            data = await self.http.get_json(f"{self._api(board_identifier)}{path}")
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None  # unpublished between list and detail request
            raise
        info = data.get("jobPostingInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            raise ConnectorError("workday: detail response has no 'jobPostingInfo'")
        if info.get("posted") is False:
            return None

        locations = [info.get("location"), *(info.get("additionalLocations") or [])]
        location_raw = "; ".join(str(loc) for loc in locations if loc) or job.location_raw
        country = ((info.get("jobRequisitionLocation") or {}).get("country") or {}).get(
            "alpha2Code"
        )
        remote_type = remote_type_from_value(info.get("remoteType"))
        if remote_type is RemoteType.UNKNOWN:
            remote_type = job.remote_type
        if remote_type is RemoteType.UNKNOWN:
            remote_type = detect_remote_type(location_raw, job.title)
        return replace(
            job,
            description_html=info.get("jobDescription") or None,
            description_text=None,
            location_raw=location_raw,
            country=country or None,
            remote_type=remote_type,
            employment_type=normalize_employment_type(info.get("timeType")),
            posted_at=parse_datetime(info.get("startDate")),
            apply_url=info.get("externalUrl") or job.source_url,
        )

    @staticmethod
    def _map_list(posting: dict[str, Any], base: str) -> RawJob:
        path = str(posting["externalPath"])
        locations_text = posting.get("locationsText") or None
        if locations_text and _N_LOCATIONS.match(locations_text):
            locations_text = None  # "3 Locations": the detail request has the real ones
        return RawJob(
            external_id=_external_id(path),
            title=str(posting.get("title") or "").strip(),
            location_raw=locations_text,
            remote_type=remote_type_from_value(posting.get("remoteType")),
            source_url=f"{base}{path}",
        )


def _external_id(external_path: str) -> str:
    """ "/job/US-CA-Santa-Clara/Senior-Manager_JR2019986" -> "JR2019986" (the requisition id,
    stable when the title changes). Falls back to the whole path."""
    tail = urlsplit(external_path).path.rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit("_", 1)[1] if "_" in tail else external_path


def _expected_total(first_page: dict[str, Any]) -> int | None:
    """Number of postings the board really has (see module docstring)."""
    total = first_page.get("total") if isinstance(first_page.get("total"), int) else None
    facet_total = None
    for facet in first_page.get("facets") or []:
        if facet.get("facetParameter") == "timeType":
            facet_total = sum(int(v.get("count") or 0) for v in facet.get("values") or [])
    candidates = [n for n in (total, facet_total) if n]
    return max(candidates) if candidates else None
