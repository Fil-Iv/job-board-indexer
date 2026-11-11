"""JOIN (join.com) company career pages: paged ``__NEXT_DATA__`` list + JSON-LD job pages.

robots.txt (join.com, 2026-11-11) disallows only ``/*/lp/`` for ``*``. The job sitemap
index (``/companies/sitemap-jobs-index.xml``) answers 403, so it is not used.

List: GET https://join.com/companies/{company}[?page={n}] -> Next.js page whose
      ``__NEXT_DATA__`` holds ``props.pageProps.pageData.jobs`` (current layout) or
      ``props.pageProps.initialState.jobs`` (older layout, still served for some
      companies): ``{items: [{id, idParam, title, createdAt, workplaceType,
      city {cityName, countryName}, employmentType {name}, category, country}],
      pagination: {page, pageCount, perPage (5), total}}``. Pages are read until
      ``pageCount``; the distinct jobs must equal ``total``, and every page must report
      the page number that was asked for (else the crawl fails). On 2026-11-07 the list
      was empty server-side; it is filled since (checked 2026-11-11).
      The site serves at most 20 pages (100 jobs): ``?page=21`` returns page 20 again,
      and the ``categoryId`` filter is echoed but not applied (simplyrightde: 101 jobs,
      2026-11-11). Companies with more than 100 jobs are therefore refused (crawl
      failure), never indexed partially.
Job:  GET https://join.com/companies/{company}/{idParam} -> JSON-LD JobPosting (title,
      description, datePosted, employmentType, hiringOrganization, jobLocation).
      ``pageData.job.workplaceType`` (ONSITE | HYBRID | REMOTE) gives the remote type;
      ``pageData.job.employmentType.name`` "Ausbildung" marks apprenticeships (the
      JSON-LD says INTERN).
      Unknown companies and closed jobs answer 404.

Ids: ``id`` is the job; the number in ``idParam`` is the current *ad* of the job and
changes when the ad is re-published (the old ``idParam`` URL then 301-redirects to the
new one). So the external id is ``id`` and the page version includes ``idParam``.

Board identifier: the company slug (``bring``, ``zbv-automationcom``).
Verified on bring (4 jobs), zbv-automationcom (6, 2 pages) and hallo-theo (older
layout, 1), 2026-11-11; simplyrightde (101 jobs) is refused, see above.
"""

import json
import re
import zlib
from datetime import UTC, datetime
from typing import Any

from jobindex.config import get_settings
from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector, refresh_token
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, split_url

HOST = "join.com"
#: The last page the site serves; later page numbers return this page again.
MAX_PAGES = 20
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,150}$")
_RESERVED = {"sitemap.xml", "sitemap-jobs-index.xml", "sitemap-index.xml"}
_JOB_SEGMENT = re.compile(r"^(?P<id>\d+)-")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_WORKPLACE = {"ONSITE": RemoteType.ONSITE, "HYBRID": RemoteType.HYBRID, "REMOTE": RemoteType.REMOTE}


def next_data(html: str) -> dict[str, Any]:
    """``props.pageProps`` of a join.com page."""
    match = _NEXT_DATA.search(html)
    if match is None:
        raise ConnectorError("join: page has no __NEXT_DATA__")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise ConnectorError("join: __NEXT_DATA__ is not JSON") from exc
    props = data.get("props", {}).get("pageProps") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        raise ConnectorError("join: __NEXT_DATA__ has no pageProps")
    return props


def _state(props: dict[str, Any]) -> dict[str, Any]:
    """The page state: ``pageData`` (current layout) or ``initialState`` (older)."""
    state = props.get("pageData") or props.get("initialState")
    if not isinstance(state, dict):
        raise ConnectorError("join: __NEXT_DATA__ has neither pageData nor initialState")
    return state


@register
class JoinConnector(GenericJsonLdConnector):
    platform = "join"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if host != HOST or len(segments) < 2 or segments[0] != "companies":
            return None
        slug = segments[1].lower()
        if slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/companies/{board_identifier}"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._job_of_ad: dict[str, str] = {}  # ad number (idParam) -> job id, from the list

    def job_id(self, url: str, board_identifier: str) -> str | None:
        """Job id of a job page URL (via the ad number in it; the ad number itself for an
        ad the list did not show). Used by the base class to tell a page that redirected
        away from its job (closed) from a job page without JSON-LD (template change)."""
        host, segments, _ = split_url(url)
        if host != HOST or len(segments) != 3 or segments[:2] != ["companies", board_identifier]:
            return None
        match = _JOB_SEGMENT.match(segments[2])
        if match is None:
            return None
        return self._job_of_ad.get(match.group("id"), match.group("id"))

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        await self._load_checkpoint(board_identifier)
        items = await self._list(board_identifier)
        day = (datetime.now(UTC) - datetime(2000, 1, 1, tzinfo=UTC)).days
        days = max(1, get_settings().generic_refresh_days)
        stubs = []
        for job_id, item in items.items():
            url = f"{self.board_url(board_identifier)}/{item['idParam']}"
            ad = _JOB_SEGMENT.match(str(item["idParam"]))
            if ad:
                self._job_of_ad[ad.group("id")] = job_id
            # re-read the page when the listed fields or the ad change, and every few days
            listed = json.dumps(
                [item.get(k) for k in ("idParam", "title", "city", "employmentType")],
                sort_keys=True,
            )
            version = f"{zlib.crc32(listed.encode()):08x}:{refresh_token(url, days, day)}"
            stubs.append(
                RawJob(
                    external_id=job_id,
                    title="",
                    source_url=url,
                    list_version=self._list_version(url, version),
                )
            )
        return stubs

    async def _list(self, board_identifier: str) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            url = self.board_url(board_identifier) + (f"?page={page}" if page > 1 else "")
            jobs = _state(next_data(await self._get_text(url))).get("jobs")
            pagination = jobs.get("pagination") if isinstance(jobs, dict) else None
            page_items = jobs.get("items") if isinstance(jobs, dict) else None
            if not isinstance(pagination, dict) or not isinstance(page_items, list):
                raise ConnectorError(f"join: {url} has no jobs list with pagination")
            if int(pagination.get("pageCount") or 0) > MAX_PAGES:
                raise ConnectorError(
                    f"join: {board_identifier} has {pagination.get('total')} jobs; the site "
                    f"lists at most {MAX_PAGES} pages, so the list cannot be complete"
                )
            if pagination.get("page") != page:
                raise ConnectorError(f"join: {url} returned page {pagination.get('page')}")
            total = int(pagination.get("total") or 0)
            for item in page_items:
                if not isinstance(item, dict) or not item.get("id") or not item.get("idParam"):
                    raise ConnectorError(f"join: job without id/idParam on {url}")
                items[str(item["id"])] = item
            if page >= int(pagination.get("pageCount") or 0):
                break
        else:
            raise ConnectorError(f"join: more than {MAX_PAGES} pages; refusing a partial list")
        if len(items) != total:
            raise ConnectorError(
                f"join: {board_identifier} reports {total} jobs, listed {len(items)}; "
                "refusing a partial list"
            )
        return items

    def adjust_page(self, job: RawJob, html: str) -> RawJob:
        """Remote type from ``workplaceType``; and the employment type from JOIN's own
        type name when it is more precise than the JSON-LD's Google type (an
        "Ausbildung" / apprenticeship is ``INTERN`` there)."""
        try:
            posting = _state(next_data(html)).get("job")
        except ConnectorError:
            return job
        if not isinstance(posting, dict):
            return job
        changes: dict[str, Any] = {}
        remote = _WORKPLACE.get(str(posting.get("workplaceType") or "").upper())
        if remote is not None and job.remote_type is RemoteType.UNKNOWN:
            changes["remote_type"] = remote
        kind = posting.get("employmentType")
        name = kind.get("name") if isinstance(kind, dict) else None
        if normalize_employment_type(name if isinstance(name, str) else None) == "apprenticeship":
            changes["employment_type"] = "apprenticeship"
        return RawJob(**{**job.as_dict(), **changes}) if changes else job

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        html = await self._get_text(self.board_url(board_identifier))
        company = _state(next_data(html)).get("company")
        name = str(company.get("name") or "").strip() if isinstance(company, dict) else ""
        return name or None
