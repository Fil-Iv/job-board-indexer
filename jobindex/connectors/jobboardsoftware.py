"""Exenzo job boards (sold as jobboardsoftware.com; public JSON API per tenant).

www.jobboardsoftware.com redirects to exenzo.com/en/jobboard (Exenzo, NL: "the platform
behind 60+ boards ... from Greenjobs to Binnenlands Bestuur"). Each customer is a tenant
with its own API host; the board front ends (Nuxt) call it without any token.

Config: GET https://{tenant}.api.exenzo.com/api/public/v1/configuration
        -> {"settings": {app_timezone, app_currency, ...},
            "job_boards": [{id, status, slug, host, ...}], "sites": [...]}
        A tenant can run several boards (bb: 1 = binnenlandsbestuur.nl, 2 = an empty
        *.app.exenzo.com board).
List:   GET https://{tenant}.api.exenzo.com/api/public/v1/job-boards/{id}/jobs
            ?page={n}&limit=100
        -> {"total", "lastPage", "currentPage", "data": [job], "aggregations"}
        ``limit=100`` is honoured (verified 2026-11-10). Every job carries the full text
        (``text_own`` HTML, or the ``text_platform`` parts), organisation_name, location
        (location_name, city, postal_code, country_code), salary_min/max + salary_type
        (hour | month | year), from/till (local time of ``app_timezone``), apply_url,
        segments_names and ``job_boards_url`` {board slug: public job page URL}.
        Only active jobs are listed. The default order is not by id (premium jobs first),
        so rows are de-duplicated by id and checked against ``total``; a mismatch is
        re-paged once and then refused (never a partial list).
robots.txt on the API hosts: ``Disallow:`` (empty, everything allowed).

Board identifier: ``{tenant}/{job board id}``, e.g. ``greenjobs/1``. The tenant is the
API host prefix, which is also the folder of the board's assets on
``storage.googleapis.com/exenzo-jobboard/{tenant}/`` (how custom domains are recognised).
Verified on greenjobs (greenjobs.nl, 230 jobs), overheid (vacaturebijdeoverheid.nl, 38)
and bb (binnenlandsbestuur.nl/vacatures, 140) on 2026-11-10. carriere.nu uses the same
asset bucket (``carriere-nu``) but has no ``carriere-nu.api.exenzo.com`` (404).
"""

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, normalize_salary_period, split_url

log = logging.getLogger(__name__)

API_SUFFIX = ".api.exenzo.com"
STORAGE_HOST = "storage.googleapis.com"
STORAGE_BUCKET = "exenzo-jobboard"
PAGE_SIZE = 100
MAX_PAGES = 500  # 50k jobs
DEFAULT_TIMEZONE = "Europe/Amsterdam"
#: segments_names groups that hold the employment type (they differ per tenant).
EMPLOYMENT_GROUPS = ("dienstverband", "contract", "employment", "employment_type")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RESERVED = {"www", "api", "app", "static", "cdn"}
#: "Voorstraat, 2201 Noordwijk, 2201": the city after a Dutch postcode.
_CITY_AFTER_POSTCODE = re.compile(r"\b\d{4}\s?(?:[A-Z]{2}\s+)?(?P<city>[^\d,]+?)\s*(?:,|$)")


def _api(tenant: str) -> str:
    return f"https://{tenant}{API_SUFFIX}/api/public/v1"


@register
class JobBoardSoftwareConnector(BaseConnector):
    platform = "jobboardsoftware"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        tenant = ""
        board = "1"
        if host.endswith(API_SUFFIX):
            tenant = host.removesuffix(API_SUFFIX)
            lowered = [s.lower() for s in segments]
            if "job-boards" in lowered:
                index = lowered.index("job-boards") + 1
                if index < len(lowered) and lowered[index].isdigit():
                    board = lowered[index]
        elif host == STORAGE_HOST and len(segments) > 1 and segments[0] == STORAGE_BUCKET:
            tenant = segments[1].lower()
        else:
            return None
        if tenant in _RESERVED or not _TENANT.match(tenant):
            return None
        return f"{tenant}/{board}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        tenant, board = _split(board_identifier)
        return f"{_api(tenant)}/job-boards/{board}/jobs"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        tenant, board = _split(board_identifier)
        config = await self.http.get_json(f"{_api(tenant)}/configuration")
        board_info = _board_info(config, board)
        settings = config.get("settings") or {}
        tz = _timezone(settings.get("app_timezone"))
        currency = settings.get("app_currency") or None

        rows, total = await self._list(tenant, board)
        if len(rows) != total:
            log.info("exenzo %s: got %d of %d jobs, re-paging", board_identifier, len(rows), total)
            rows, total = await self._list(tenant, board)
            if len(rows) != total:
                raise ConnectorError(
                    f"exenzo: got {len(rows)} of {total} jobs; refusing a partial result"
                )
        slug = str(board_info.get("slug") or "")
        host = str(board_info.get("host") or "")
        return [_map(r, slug=slug, host=host, tz=tz, currency=currency) for r in rows.values()]

    async def _list(self, tenant: str, board: str) -> tuple[dict[str, dict[str, Any]], int]:
        rows: dict[str, dict[str, Any]] = {}
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            data = await self.http.get_json(
                f"{_api(tenant)}/job-boards/{board}/jobs?page={page}&limit={PAGE_SIZE}"
            )
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or not isinstance(data.get("total"), int):
                raise ConnectorError("exenzo: job list has no 'data' list or 'total'")
            total = data["total"]
            for row in items:
                if isinstance(row, dict) and row.get("id") is not None:
                    rows[str(row["id"])] = row
            last_page = data.get("lastPage")
            if not items or not isinstance(last_page, int) or page >= last_page:
                return rows, total
        raise ConnectorError(f"exenzo: more than {MAX_PAGES} pages; refusing a partial result")


def _split(board_identifier: str) -> tuple[str, str]:
    tenant, _, board = board_identifier.partition("/")
    return tenant, board or "1"


def _board_info(config: Any, board: str) -> dict[str, Any]:
    boards = config.get("job_boards") if isinstance(config, dict) else None
    if not isinstance(boards, list):
        raise ConnectorError("exenzo: configuration has no 'job_boards' list")
    for info in boards:
        if isinstance(info, dict) and str(info.get("id")) == board:
            return info
    raise ConnectorError(f"exenzo: job board {board} is not in the tenant configuration")


def _timezone(name: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or DEFAULT_TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _local_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    """ "2026-11-10 00:00:00" in the tenant's time zone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None  # 0 = not given


def _description(row: dict[str, Any]) -> str | None:
    own = row.get("text_own")
    if row.get("text_type") != "platform" and isinstance(own, str) and own.strip():
        return own
    parts = row.get("text_platform")
    if isinstance(parts, dict):
        texts = [str(v) for v in parts.values() if isinstance(v, str) and v.strip()]
        if texts:
            return "\n".join(texts)
    for key in ("text_own", "description"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _city(row: dict[str, Any]) -> str | None:
    """The ``city`` field, or the city after a postcode when it holds a whole address."""
    city = str(row.get("city") or "").strip()
    if not city:
        return None
    if "," not in city and not any(ch.isdigit() for ch in city):
        return city
    match = _CITY_AFTER_POSTCODE.search(city)
    return match["city"].strip() if match else None


def _employment_type(row: dict[str, Any]) -> str | None:
    names = row.get("segments_names")
    if not isinstance(names, dict):
        return None
    for group in EMPLOYMENT_GROUPS:
        values = names.get(group)
        if isinstance(values, list):
            for value in values:
                normalized = normalize_employment_type(str(value))
                if normalized:
                    return normalized
    return None


def _source_url(row: dict[str, Any], slug: str, host: str) -> str | None:
    urls = row.get("job_boards_url")
    if isinstance(urls, dict):
        if isinstance(urls.get(slug), str):
            return str(urls[slug])
        for value in urls.values():
            if isinstance(value, str):
                return value
    if host and row.get("slug"):
        return f"https://{host}/vacature/{row['slug']}"
    return None


def _map(
    row: dict[str, Any], *, slug: str, host: str, tz: ZoneInfo, currency: str | None
) -> RawJob:
    salary_min = _amount(row.get("salary_min"))
    salary_max = _amount(row.get("salary_max"))
    has_salary = salary_min is not None or salary_max is not None
    location = str(row.get("location_name") or "").strip() or None
    return RawJob(
        external_id=str(row["id"]),
        title=str(row.get("title") or "").strip(),
        company=str(row.get("organisation_name") or "").strip() or None,
        description_html=_description(row),
        location_raw=location,
        city=_city(row),
        country=row.get("country_code") or None,
        # remote is true or null; null means "not stated".
        remote_type=RemoteType.REMOTE if row.get("remote") is True else RemoteType.UNKNOWN,
        employment_type=_employment_type(row),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency if has_salary else None,
        salary_period=normalize_salary_period(row.get("salary_type")) if has_salary else None,
        posted_at=_local_datetime(row.get("from"), tz)
        or _local_datetime(row.get("created_at"), tz),
        expires_at=_local_datetime(row.get("till"), tz),
        source_url=_source_url(row, slug, host),
        apply_url=row.get("apply_url") or None,
    )
