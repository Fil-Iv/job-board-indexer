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
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from jobindex.checkpoint import is_fresh, now_iso
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
CHECKPOINT_EVERY_PAGES = 25  # save progress every 500 postings
PROGRESS_EVERY_PAGES = 10  # log progress every 200 postings
CHECKPOINT_MAX_AGE = timedelta(hours=2)  # older progress is stale: start over
CHECKPOINT_VERSION = 1
# Fields of a list posting we keep (and checkpoint): enough for _map_list.
_KEPT_FIELDS = ("externalPath", "title", "locationsText", "remoteType")
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
        run = await self._start_run(board_identifier, resume=True)
        expected = await self._collect_all(run)
        if expected is not None and len(run.postings) < expected:
            log.info(
                "workday %s: got %d of %d postings, re-paging from scratch once",
                board_identifier,
                len(run.postings),
                expected,
            )
            run = await self._start_run(board_identifier, resume=False)
            expected = await self._collect_all(run)
            if expected is not None and len(run.postings) < expected:
                await self.checkpoint.clear()
                raise ConnectorError(
                    f"workday: got {len(run.postings)} of {expected} postings; "
                    "refusing a partial result"
                )
        # The list is complete: never let a later failure resume from it (it would be stale).
        await self.checkpoint.clear()
        log.info("workday %s: list complete, %d postings", board_identifier, len(run.postings))
        base = _public_base(board_identifier)
        return [self._map_list(p, base) for p in run.postings.values()]

    async def _start_run(self, board_identifier: str, *, resume: bool) -> "_Run":
        saved = await self.checkpoint.load() if resume else None
        run = _Run.from_checkpoint(board_identifier, saved)
        if run is None:
            await self.checkpoint.clear()
            return _Run(board_identifier)
        log.info(
            "workday %s: resuming from checkpoint (%d postings, %d slices done)",
            board_identifier,
            len(run.postings),
            len(run.done),
        )
        return run

    async def _collect_all(self, run: "_Run") -> int | None:
        first = await self._page(run.board, {}, 0)
        run.expected = _expected_total(first)
        await self._collect(run, {}, first, run.expected, depth=0)
        return run.expected

    async def _collect(
        self,
        run: "_Run",
        applied: dict[str, list[str]],
        first: dict[str, Any] | None,
        expected: int | None,
        depth: int,
    ) -> None:
        """Add all postings matching ``applied`` facets to ``run``. Above RESULT_CAP, split
        by a facet. ``first`` (page 0 of this slice) is fetched lazily when None."""
        if _slice_key(applied) in run.done:
            return  # finished in an earlier, interrupted attempt
        if expected is None or expected <= RESULT_CAP:
            await self._page_through(run, applied, first, expected)
            return
        if depth >= MAX_SPLIT_DEPTH:
            raise ConnectorError(f"workday: cannot split {expected} postings below {RESULT_CAP}")
        if first is None:
            first = await self._page(run.board, applied, 0)
        split = _partition_facet(first, expected, applied)
        if split is None:
            raise ConnectorError(
                f"workday: {expected} postings exceed the {RESULT_CAP} result cap and no "
                "facet partitions them; refusing a partial result"
            )
        param, values = split
        log.info(
            "workday %s: %d postings > %d cap, splitting by %s (%d values)",
            run.board,
            expected,
            RESULT_CAP,
            param,
            len(values),
        )
        for value_id, count in values:
            sub_applied = {**applied, param: [value_id]}
            if _slice_key(sub_applied) in run.done:
                continue
            if count > RESULT_CAP:  # needs its own facets to split further
                sub_first = await self._page(run.board, sub_applied, 0)
                sub_expected = _expected_total(sub_first) or count
                await self._collect(run, sub_applied, sub_first, sub_expected, depth + 1)
            else:
                await self._collect(run, sub_applied, None, count, depth + 1)
        run.done.add(_slice_key(applied))
        await self._save(run, force=True)

    async def _save(self, run: "_Run", *, force: bool = False) -> None:
        run.pages_since_save += 1
        if force or run.pages_since_save >= CHECKPOINT_EVERY_PAGES:
            run.pages_since_save = 0
            await self.checkpoint.save(run.to_checkpoint())

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
        run: "_Run",
        applied: dict[str, list[str]],
        first: dict[str, Any] | None,
        expected: int | None,
    ) -> None:
        key = _slice_key(applied)
        page, count = run.cursor.get(key, (0, 0))
        while page < MAX_PAGES:
            if page == 0 and first is not None:
                data = first
            else:
                data = await self._page(run.board, applied, page)
            batch = data["jobPostings"]
            added = run.add(batch)
            count += added
            page += 1
            run.pages += 1
            if run.pages % PROGRESS_EVERY_PAGES == 0:
                log.info(
                    "workday %s: %d pages, %d/%s postings%s",
                    run.board,
                    run.pages,
                    len(run.postings),
                    run.expected if run.expected is not None else "?",
                    f" (slice {key})" if applied else "",
                )
            # End of list: a short page, or — Workday's quirk past the real end — a full page
            # that only repeats postings we already have (it wraps around to page 1).
            if len(batch) < PAGE_SIZE or added == 0 or (expected and count >= expected):
                run.cursor.pop(key, None)
                run.done.add(key)
                await self._save(run, force=True)
                return
            run.cursor[key] = (page, count)
            await self._save(run)
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


def _slice_key(applied: dict[str, list[str]]) -> str:
    """Stable name of a facet slice ("" = the whole board), used as checkpoint key."""
    return json.dumps(applied, sort_keys=True) if applied else ""


@dataclass
class _Run:
    """State of one list fetch; serialisable to a checkpoint so it can be resumed."""

    board: str
    postings: dict[str, dict[str, Any]] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)  # finished slice keys
    cursor: dict[str, tuple[int, int]] = field(default_factory=dict)  # slice -> (page, count)
    expected: int | None = None
    pages: int = 0
    pages_since_save: int = 0

    def add(self, batch: list[dict[str, Any]]) -> int:
        """Add list postings; return how many were new."""
        before = len(self.postings)
        for posting in batch:
            path = posting.get("externalPath")
            if path:
                self.postings[_external_id(path)] = {
                    k: posting[k] for k in _KEPT_FIELDS if k in posting
                }
        return len(self.postings) - before

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "version": CHECKPOINT_VERSION,
            "board": self.board,
            "saved_at": now_iso(),
            "postings": self.postings,
            "done": sorted(self.done),
            "cursor": {k: list(v) for k, v in self.cursor.items()},
        }

    @classmethod
    def from_checkpoint(cls, board: str, data: dict[str, Any] | None) -> "_Run | None":
        """The saved run, or None if absent, for another board, of an old format or stale."""
        if not data or data.get("version") != CHECKPOINT_VERSION or data.get("board") != board:
            return None
        if not is_fresh(data.get("saved_at"), CHECKPOINT_MAX_AGE):
            return None
        try:
            return cls(
                board=board,
                postings=dict(data["postings"]),
                done=set(data["done"]),
                cursor={k: (int(v[0]), int(v[1])) for k, v in data["cursor"].items()},
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


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
