"""Recruitee careers site API.

List: GET https://{company}.recruitee.com/api/offers/
      -> {"offers": [{id, title, description, requirements, location, city, country_code,
                      remote, hybrid, on_site, employment_type_code, salary, ...}]}
      (one request, every published offer, with descriptions; no pagination)

Board identifier: the Recruitee subdomain, e.g. ``bunq``.

URL formats recognised:
    {company}.recruitee.com[/o/{slug}[/c/new]]     {company}.recruitee.com/api/offers/
Custom domains (e.g. careers.bunq.com): the API is NOT served there (404, verified
2026-11-07), but the page links to ``https://{company}.recruitee.com``, which the generic
HTML detection in ``resolve_url`` picks up.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    split_url,
)

_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_RESERVED = {"www", "app", "api", "support", "blog", "help", "status", "careers", "docs"}


def _decimal(value: Any) -> Decimal | None:
    """Salary amounts come as strings; "0" means "not set" in practice."""
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


@register
class RecruiteeConnector(BaseConnector):
    platform = "recruitee"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(".recruitee.com"):
            return None
        sub = host.removesuffix(".recruitee.com")
        if "." in sub or sub in _RESERVED or not _SUBDOMAIN.match(sub):
            return None
        return sub

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.recruitee.com"

    async def _offers(self, board_identifier: str) -> list[dict[str, Any]]:
        data = await self.http.get_json(f"https://{board_identifier}.recruitee.com/api/offers/")
        if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
            raise ConnectorError("recruitee: response has no 'offers' list")
        return [o for o in data["offers"] if o.get("status", "published") == "published"]

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        offers = await self._offers(board_identifier)
        name = offers[0].get("company_name") if offers else None
        return str(name) if name else None

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        return [self._map(o) for o in await self._offers(board_identifier)]

    @staticmethod
    def _map(offer: dict[str, Any]) -> RawJob:
        if offer.get("remote"):
            remote_type = RemoteType.REMOTE
        elif offer.get("hybrid"):
            remote_type = RemoteType.HYBRID
        elif offer.get("on_site"):
            remote_type = RemoteType.ONSITE
        else:
            remote_type = RemoteType.UNKNOWN

        html = "\n".join(
            part for part in (offer.get("description"), offer.get("requirements")) if part
        )
        salary = offer.get("salary") or {}
        salary_min, salary_max = _decimal(salary.get("min")), _decimal(salary.get("max"))
        has_salary = salary_min is not None or salary_max is not None
        return RawJob(
            external_id=str(offer["id"]),
            title=str(offer.get("title") or "").strip(),
            company=offer.get("company_name") or None,
            description_html=html or None,
            location_raw=offer.get("location") or None,
            city=offer.get("city") or None,
            region=offer.get("state_name") or None,
            country=offer.get("country_code") or offer.get("country") or None,
            remote_type=remote_type,
            employment_type=normalize_employment_type(offer.get("employment_type_code")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=(salary.get("currency") or None) if has_salary else None,
            salary_period=normalize_salary_period(salary.get("period")) if has_salary else None,
            posted_at=parse_datetime(offer.get("published_at")),
            source_url=offer.get("careers_url"),
            apply_url=offer.get("careers_apply_url"),
        )
