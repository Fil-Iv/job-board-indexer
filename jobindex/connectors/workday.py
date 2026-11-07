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
* A single query returns at most 2000 postings. Past that (and past the real end),
  Workday does not return an empty page: it serves page 1 again (NVIDIA, offsets
  2000..7000). We stop paging when a page adds no new posting.
* ``total`` is only set on the first page (later pages say 0) and is itself capped at
  2000. The real count is the sum of a single-valued facet (``timeType``; NVIDIA: 2810).
* Boards above the cap are split with ``appliedFacets`` by a facet that partitions them
  (counts sum to the total, e.g. ``jobFamilyGroup``: NVIDIA's largest slice is 1779),
  recursively if a slice is still too big.
* The union is checked against the expected total; short results are re-fetched once,
  then refused (offset paging over a list that changes mid-crawl can skip postings).

URL formats recognised:
    {tenant}.wd{n}.myworkdayjobs.com/[{locale}/]{site}[/job/{location}/{slug}_{id}]
    {tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/...
    [{tenant}.]wd{n}.myworkdaysite.com/[{locale}/]recruiting/{tenant}/{site}[/job/...]
Custom-domain career pages: links/iframes to any of the above (generic HTML detection).
"""

import asyncio
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
RESULT_CAP = 2000  # one query never returns more than this many postings
MAX_SPLIT_DEPTH = 3
NON_JSON_RETRIES = 2
NON_JSON_BACKOFF_SECONDS = 2.0
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
        postings, expected = await self._collect_all(board_identifier)
        if expected is not None and len(postings) < expected:
            log.info(
                "workday %s: got %d of %d postings, re-paging once",
                board_identifier,
                len(postings),
                expected,
            )
            postings, expected = await self._collect_all(board_identifier)
            if expected is not None and len(postings) < expected:
                raise ConnectorError(
                    f"workday: got {len(postings)} of {expected} postings; "
                    "refusing a partial result"
                )
        base = _public_base(board_identifier)
        return [self._map_list(p, base) for p in postings.values()]

    async def _collect_all(
        self, board_identifier: str
    ) -> tuple[dict[str, dict[str, Any]], int | None]:
        first = await self._page(board_identifier, {}, 0)
        expected = _expected_total(first)
        postings = await self._collect(board_identifier, {}, first, expected, depth=0)
        return postings, expected

    async def _collect(
        self,
        board_identifier: str,
        applied: dict[str, list[str]],
        first: dict[str, Any],
        expected: int | None,
        depth: int,
    ) -> dict[str, dict[str, Any]]:
        """All postings matching ``applied`` facets. Above RESULT_CAP, split by a facet."""
        if expected is None or expected <= RESULT_CAP:
            return await self._page_through(board_identifier, applied, first, expected)
        if depth >= MAX_SPLIT_DEPTH:
            raise ConnectorError(f"workday: cannot split {expected} postings below {RESULT_CAP}")
        split = _partition_facet(first, expected, applied)
        if split is None:
            raise ConnectorError(
                f"workday: {expected} postings exceed the {RESULT_CAP} result cap and no "
                "facet partitions them; refusing a partial result"
            )
        param, values = split
        log.info(
            "workday %s: %d postings > %d cap, splitting by %s (%d values)",
            board_identifier,
            expected,
            RESULT_CAP,
            param,
            len(values),
        )
        postings: dict[str, dict[str, Any]] = {}
        for value_id, count in values:
            sub_applied = {**applied, param: [value_id]}
            sub_first = await self._page(board_identifier, sub_applied, 0)
            sub_expected = _expected_total(sub_first) or count
            postings.update(
                await self._collect(
                    board_identifier, sub_applied, sub_first, sub_expected, depth + 1
                )
            )
        return postings

    async def _page(
        self, board_identifier: str, applied: dict[str, list[str]], page: int
    ) -> dict[str, Any]:
        body = {
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "appliedFacets": applied,
            "searchText": "",
        }
        # Workday occasionally answers 200 with an empty/non-JSON body (seen on NVIDIA,
        # 2026-11-07); the HTTP client only retries error statuses, so retry here.
        for attempt in range(NON_JSON_RETRIES + 1):
            response = await self.http.request(
                "POST",
                f"{self._api(board_identifier)}/jobs",
                json=body,
                headers={"Accept": "application/json"},
            )
            try:
                data = response.json()
            except ValueError as exc:
                if attempt == NON_JSON_RETRIES:
                    raise ConnectorError("workday: list response is not JSON") from exc
                log.info("workday %s: non-JSON page %d, retrying", board_identifier, page)
                await asyncio.sleep(NON_JSON_BACKOFF_SECONDS * (attempt + 1))
                continue
            if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
                raise ConnectorError("workday: response has no 'jobPostings' list")
            return data
        raise AssertionError("unreachable")  # pragma: no cover

    async def _page_through(
        self,
        board_identifier: str,
        applied: dict[str, list[str]],
        first: dict[str, Any],
        expected: int | None,
    ) -> dict[str, dict[str, Any]]:
        postings: dict[str, dict[str, Any]] = {}
        data = first
        for page in range(MAX_PAGES):
            if page > 0:
                data = await self._page(board_identifier, applied, page)
            batch = data["jobPostings"]
            before = len(postings)
            for posting in batch:
                path = posting.get("externalPath")
                if path:
                    postings[_external_id(path)] = posting
            # End of list: a short page, or — Workday's quirk past the real end — a full page
            # that only repeats postings we already have (it wraps around to page 1).
            if len(batch) < PAGE_SIZE or len(postings) == before:
                return postings
            if expected is not None and len(postings) >= expected:
                return postings
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


def _partition_facet(
    first_page: dict[str, Any], expected: int, applied: dict[str, list[str]]
) -> tuple[str, list[tuple[str, int]]] | None:
    """A flat facet whose value counts sum to ``expected`` (so every posting has exactly
    one value), preferring the one with the smallest largest slice."""
    best: tuple[int, str, list[tuple[str, int]]] | None = None
    for facet in first_page.get("facets") or []:
        param = facet.get("facetParameter")
        values = facet.get("values") or []
        if not param or param in applied or any("values" in v for v in values):
            continue  # nested (hierarchical) facets overlap; skip them
        pairs = [(str(v["id"]), int(v.get("count") or 0)) for v in values if v.get("id")]
        if len(pairs) < 2 or sum(c for _, c in pairs) != expected:
            continue
        largest = max(c for _, c in pairs)
        if best is None or largest < best[0]:
            best = (largest, param, pairs)
    return (best[1], best[2]) if best else None
