"""Pinpoint careers site (public JSON behind every ``{company}.pinpointhq.com`` board).

List: GET https://{company}.pinpointhq.com/postings.json
      -> {"data": [{id, title, url, description, key_responsibilities,
                    skills_knowledge_expertise, benefits (+ *_header), employment_type,
                    workplace_type (onsite|hybrid|remote), compensation_minimum/_maximum/
                    _currency/_frequency/_visible, deadline_at, location{city, province,
                    name}, job{department, ...}}]}
      Every published posting, with descriptions, in one response (no pagination).

Board identifier: the Pinpoint subdomain, e.g. ``indrive``.

Quirks (verified 2026-11-07):
* robots.txt is per tenant (davies/indrive allow us; dazn/osapiens ``Disallow: /``). The
  HttpClient enforces it per host, so a disallowed tenant fails with RobotsDisallowedError.
* ``location.name`` is a free label (a country, a city, "Home United States"); "Home" is
  Pinpoint's placeholder for home-based roles and is dropped from the location text.
* The description is split over several HTML sections, each with its own header.

URL formats recognised:
    {company}.pinpointhq.com[/{locale}][/postings/{uuid}[/applications/new]|/jobs/{id}]
    {company}.pinpointhq.com/postings.json
Custom-domain career pages: links/iframes to the above (generic HTML detection).
"""

import re
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.normalize import (
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    remote_type_from_value,
    split_url,
)

_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_RESERVED = {"www", "app", "api", "help", "support", "status", "developers", "blog"}
# (section field, header field, default header)
_SECTIONS = (
    ("description", None, None),
    ("key_responsibilities", "key_responsibilities_header", "Key responsibilities"),
    ("skills_knowledge_expertise", "skills_knowledge_expertise_header", "Skills"),
    ("benefits", "benefits_header", "Benefits"),
)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _description(posting: dict[str, Any]) -> str | None:
    parts = []
    for field, header_field, default in _SECTIONS:
        body = posting.get(field)
        if not isinstance(body, str) or not body.strip():
            continue
        if header_field:
            header = posting.get(header_field) or default
            parts.append(f"<h3>{escape(str(header))}</h3>")
        parts.append(body)
    return "\n".join(parts) or None


def _location(location: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for key in ("city", "province", "name"):
        value = str(location.get(key) or "").strip()
        value = re.sub(r"^Home\b\s*", "", value)  # "Home", "Home United States"
        if value and value.lower() not in (p.lower() for p in parts):
            parts.append(value)
    return ", ".join(parts) or None


@register
class PinpointConnector(BaseConnector):
    platform = "pinpoint"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(".pinpointhq.com"):
            return None
        sub = host.removesuffix(".pinpointhq.com")
        if "." in sub or sub in _RESERVED or not _SUBDOMAIN.match(sub):
            return None
        return sub

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.pinpointhq.com"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        data = await self.http.get_json(f"https://{board_identifier}.pinpointhq.com/postings.json")
        postings = data.get("data") if isinstance(data, dict) else None
        if not isinstance(postings, list):
            raise ConnectorError("pinpoint: response has no 'data' list")
        return [self._map(p) for p in postings]

    @staticmethod
    def _map(posting: dict[str, Any]) -> RawJob:
        visible = posting.get("compensation_visible") is not False
        salary_min = _decimal(posting.get("compensation_minimum")) if visible else None
        salary_max = _decimal(posting.get("compensation_maximum")) if visible else None
        has_salary = salary_min is not None or salary_max is not None
        url = posting.get("url") or None
        return RawJob(
            external_id=str(posting["id"]),
            title=str(posting.get("title") or "").strip(),
            description_html=_description(posting),
            location_raw=_location(posting.get("location") or {}),
            remote_type=remote_type_from_value(posting.get("workplace_type")),
            employment_type=normalize_employment_type(
                posting.get("employment_type") or posting.get("employment_type_text")
            ),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=(posting.get("compensation_currency") or None) if has_salary else None,
            salary_period=(
                normalize_salary_period(posting.get("compensation_frequency"))
                if has_salary
                else None
            ),
            posted_at=parse_datetime(posting.get("published_at") or posting.get("created_at")),
            source_url=url,
            apply_url=f"{url}/applications/new" if url else None,
        )
