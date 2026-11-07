"""WP Job Manager (WordPress plugin by Automattic) job boards, via the WordPress REST API.

List:  GET {site}/wp-json/wp/v2/job-listings?per_page=100&page={n}&orderby=id&order=asc
           &_fields=id,date_gmt,link,title,content,meta,job-types
       -> [{id, date_gmt, link, title: {rendered}, content: {rendered},
            meta: {_job_location, _company_name, _application, _filled, _remote_position,
                   _job_salary, _job_salary_currency, _job_salary_unit, ...},
            "job-types": [term ids]}]
       Headers X-WP-Total / X-WP-TotalPages give the size; per_page is capped at 100.
Types: GET {site}/wp-json/wp/v2/job-types?per_page=100&_fields=id,name
       (term id -> name, e.g. "Full Time", "Vollzeit"), one request per crawl.

The REST endpoint lists ``publish`` posts only, so expired listings (post status
``expired``) are not in it. Filled positions (``_filled = 1``) are still ``publish`` and
are dropped here. The plugin's own AJAX list (``/jm-ajax/get_listings/``) is NOT used: it
returns rendered HTML without descriptions and, on some themes, includes expired
listings (clsrecruitment.ie: about 420 entries back to 2016 vs 29 open, verified
2026-11-07).

Board identifier: the WordPress site root without scheme, as given by the page's
``<link rel="https://api.w.org/" href="https://{root}/wp-json/">`` (``startjob.dk``,
``example.org/careers`` for WordPress in a subdirectory). WP Job Manager has no URL
pattern of its own (``/job/{slug}/`` is common to many plugins), so boards are only
recognised from page HTML: the plugin's assets under ``/wp-content/plugins/wp-job-manager/``.

Not supported: sites that disable or restrict the REST API (kiwiconseil.fr answers 404 on
``/wp-json/wp/v2/job-listings``) and sites without pretty permalinks (REST at
``?rest_route=``). Their crawls fail with an HTTP error instead of returning no jobs.

Verified on startjob.dk, fmals.de and clsrecruitment.ie (2026-11-07).
"""

import re
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    Salary,
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    parse_salary,
)

PER_PAGE = 100  # WordPress REST maximum
_LIST_FIELDS = "id,date_gmt,link,title,content,meta,job-types"

_PLUGIN_ASSET = re.compile(r"/wp-content/plugins/wp-job-manager/")
_API_LINK = re.compile(
    r"<link[^>]+rel=[\"']https://api\.w\.org/[\"'][^>]*href=[\"']([^\"']+)[\"']"
    r"|<link[^>]+href=[\"']([^\"']+)[\"'][^>]*rel=[\"']https://api\.w\.org/[\"']",
    re.IGNORECASE,
)
_BOARD = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?(?:/[^\s?#]*[^\s?#/])?$")


def _site_root(api_url: str) -> str | None:
    """``https://Example.org/careers/wp-json/`` -> ``example.org/careers``."""
    parts = urlsplit(unescape(api_url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    path = parts.path.rstrip("/")
    if not path.endswith("/wp-json"):
        return None  # ?rest_route= sites: not supported (see module docstring)
    root = parts.hostname.lower() + (f":{parts.port}" if parts.port else "")
    root += path.removesuffix("/wp-json")
    return root if _BOARD.match(root) else None


def _total(response: httpx.Response, header: str) -> int:
    try:
        return int(response.headers[header])
    except (KeyError, ValueError):
        raise ConnectorError(f"wpjobmanager: response lacks a numeric {header} header") from None


def _http_url(value: Any) -> str | None:
    """``_application`` is a URL or an e-mail address (sometimes garbage, e.g.
    ``http://,person@...``); keep real web URLs only."""
    text = str(value or "").strip()
    parts = urlsplit(text)
    host = parts.hostname or ""
    if parts.scheme in ("http", "https") and "." in host and "@" not in text:
        return text
    return None


def _flag(value: Any) -> bool:
    """Meta booleans come as 0/1, "0"/"1" or true/false depending on the plugin version."""
    return str(value).strip().lower() in ("1", "true", "yes")


def _salary(meta: dict[str, Any]) -> Salary | None:
    """``_job_salary`` is free text ("50000", "€40k - €50k"); currency and unit
    (HOUR/DAY/WEEK/MONTH/YEAR) are separate fields."""
    raw = str(meta.get("_job_salary") or "").strip()
    if not raw:
        return None
    currency = str(meta.get("_job_salary_currency") or "").strip().upper() or None
    period = normalize_salary_period(str(meta.get("_job_salary_unit") or ""))
    try:
        amount: Decimal | None = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        amount = None
    if amount is not None:
        return Salary(amount, amount, currency, period) if amount > 0 else None
    parsed = parse_salary(f"{currency} {raw}" if currency else raw)
    if parsed is None:
        return None
    return Salary(parsed.min, parsed.max, parsed.currency or currency, parsed.period or period)


@register
class WpJobManagerConnector(BaseConnector):
    platform = "wpjobmanager"

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None  # no URL pattern of its own: boards are found from page HTML

    @classmethod
    def detect_in_html(cls, html: str, page_url: str | None = None) -> dict[str, int]:
        if not _PLUGIN_ASSET.search(html):
            return {}
        roots = [m.group(1) or m.group(2) for m in _API_LINK.finditer(html)]
        board = next((r for r in map(_site_root, roots) if r), None)
        if board is None and page_url and not roots:
            # No REST discovery link (some security plugins remove it): assume the
            # site root is the page's host.
            board = _site_root(
                f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}/wp-json/"
            )
        return {board: 1} if board else {}

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}/"

    def _api(self, board_identifier: str, route: str) -> str:
        return f"https://{board_identifier}/wp-json/wp/v2/{route}"

    async def _job_types(self, board_identifier: str) -> dict[int, str]:
        data = await self.http.get_json(
            self._api(board_identifier, "job-types"),
            params={"per_page": PER_PAGE, "_fields": "id,name"},
        )
        if not isinstance(data, list):
            raise ConnectorError("wpjobmanager: job-types response is not a list")
        return {int(t["id"]): unescape(str(t["name"])) for t in data if "id" in t}

    async def _page(self, board_identifier: str, page: int) -> tuple[list[Any], int, int]:
        response = await self.http.get(
            self._api(board_identifier, "job-listings"),
            params={
                "per_page": PER_PAGE,
                "page": page,
                "orderby": "id",
                "order": "asc",
                "_fields": _LIST_FIELDS,
            },
            headers={"Accept": "application/json"},
        )
        try:
            data = response.json()
        except ValueError:
            raise ConnectorError(f"wpjobmanager: page {page} is not JSON") from None
        if not isinstance(data, list):
            raise ConnectorError(f"wpjobmanager: page {page} is not a list")
        return data, _total(response, "X-WP-Total"), _total(response, "X-WP-TotalPages")

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        types = await self._job_types(board_identifier)
        items, total, pages = await self._page(board_identifier, 1)
        for page in range(2, pages + 1):
            more, page_total, _ = await self._page(board_identifier, page)
            if page_total != total:  # a listing was added/removed mid-crawl: retry later
                raise ConnectorError(
                    f"wpjobmanager: X-WP-Total changed from {total} to {page_total} while paging"
                )
            items.extend(more)
        by_id = {str(item["id"]): item for item in items if isinstance(item, dict) and "id" in item}
        if len(by_id) != total:
            raise ConnectorError(f"wpjobmanager: got {len(by_id)} listings, X-WP-Total={total}")
        return [
            self._map(item, types)
            for item in by_id.values()
            if not _flag((item.get("meta") or {}).get("_filled"))
        ]

    @staticmethod
    def _map(item: dict[str, Any], types: dict[int, str]) -> RawJob:
        meta: dict[str, Any] = item.get("meta") or {}
        mapped = (normalize_employment_type(types.get(i)) for i in item.get("job-types") or [])
        employment_type = next((t for t in mapped if t), None)
        remote = _flag(meta.get("_remote_position"))
        remote_type = RemoteType.REMOTE if remote else RemoteType.UNKNOWN
        salary = _salary(meta)
        date_gmt = item.get("date_gmt")
        link = item.get("link") or None
        return RawJob(
            external_id=str(item["id"]),
            title=unescape(str((item.get("title") or {}).get("rendered") or "")).strip(),
            company=str(meta.get("_company_name") or "").strip() or None,
            description_html=(item.get("content") or {}).get("rendered") or None,
            location_raw=str(meta.get("_job_location") or "").strip() or None,
            remote_type=remote_type,
            employment_type=employment_type,
            salary_min=salary.min if salary else None,
            salary_max=salary.max if salary else None,
            salary_currency=salary.currency if salary else None,
            salary_period=salary.period if salary else None,
            posted_at=parse_datetime(f"{date_gmt}Z") if date_gmt else None,
            source_url=link,
            apply_url=_http_url(meta.get("_application")) or link,
        )
