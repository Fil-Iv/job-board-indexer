"""Lever Postings API.

Docs: https://github.com/lever/postings-api
List: GET https://api.lever.co/v0/postings/{site}?mode=json&skip={n}&limit={m}
      (EU accounts: api.eu.lever.co). Paginated with skip/limit; descriptions included.

Board identifier: ``{site}`` for the US instance, ``eu:{site}`` for the EU instance.

URL formats recognised:
    jobs.lever.co/{site}[/{posting-id}[/apply]]      jobs.eu.lever.co/{site}[/...]
    api.lever.co/v0/postings/{site}[/...]            api.eu.lever.co/v0/postings/{site}
Custom-domain pages: any of the above embedded in the page, or the "lever-jobs-embed"
script config ``leverJobsOptions = {accountName: "{site}"}``.
"""

import re
from decimal import Decimal, InvalidOperation
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

PAGE_SIZE = 100
MAX_PAGES = 500  # 50k postings; protects against an API that ignores `skip`
_SITE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_EU_PREFIX = "eu:"


def _api_base(board_identifier: str) -> tuple[str, str]:
    if board_identifier.startswith(_EU_PREFIX):
        return "https://api.eu.lever.co/v0/postings", board_identifier[len(_EU_PREFIX) :]
    return "https://api.lever.co/v0/postings", board_identifier


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@register
class LeverConnector(BaseConnector):
    platform = "lever"
    company_name_sources = (re.compile(r"^(?P<name>.+)$"),)
    html_fingerprints = (
        re.compile(
            r"""leverJobsOptions\s*=\s*\{[^}]*?accountName["']?\s*:\s*["'](?P<id>[A-Za-z0-9._-]+)["']"""
        ),
    )

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        site: str | None = None
        eu = host in {"jobs.eu.lever.co", "api.eu.lever.co"}
        if host in {"jobs.lever.co", "jobs.eu.lever.co"} and segments:
            site = segments[0]
        elif host in {"api.lever.co", "api.eu.lever.co"} and segments[:2] == ["v0", "postings"]:
            site = segments[2] if len(segments) >= 3 else None
        if site and _SITE.match(site):
            return f"{_EU_PREFIX}{site}" if eu else site
        return None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        if board_identifier.startswith(_EU_PREFIX):
            return f"https://jobs.eu.lever.co/{board_identifier[len(_EU_PREFIX) :]}"
        return f"https://jobs.lever.co/{board_identifier}"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        base, site = _api_base(board_identifier)
        postings: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            batch = await self.http.get_json(
                f"{base}/{site}",
                params={"mode": "json", "skip": page * PAGE_SIZE, "limit": PAGE_SIZE},
            )
            if not isinstance(batch, list):
                raise ConnectorError("lever: expected a JSON list of postings")
            new = [p for p in batch if p.get("id") not in seen]
            if batch and not new:
                raise ConnectorError("lever: pagination returned only repeated postings")
            seen.update(str(p.get("id")) for p in batch)
            postings.extend(new)
            if len(batch) < PAGE_SIZE:
                return [self._map(p) for p in postings]
        raise ConnectorError(f"lever: more than {MAX_PAGES} pages; refusing a partial result")

    @staticmethod
    def _map(p: dict[str, Any]) -> RawJob:
        categories = p.get("categories") or {}
        parts = [p.get("description") or ""]
        for section in p.get("lists") or []:
            parts.append(f"<h3>{section.get('text', '')}</h3><ul>{section.get('content', '')}</ul>")
        parts.append(p.get("additional") or "")
        description_html = "\n".join(part for part in parts if part) or None

        salary = p.get("salaryRange") or {}
        all_locations = categories.get("allLocations") or []
        location_raw = categories.get("location") or ("; ".join(all_locations) or None)

        return RawJob(
            external_id=str(p["id"]),
            title=str(p.get("text") or "").strip(),
            description_html=description_html,
            location_raw=location_raw,
            country=p.get("country") or None,
            remote_type=remote_type_from_value(p.get("workplaceType")),
            employment_type=normalize_employment_type(categories.get("commitment")),
            salary_min=_decimal(salary.get("min")),
            salary_max=_decimal(salary.get("max")),
            salary_currency=salary.get("currency") or None,
            salary_period=normalize_salary_period(salary.get("interval")),
            posted_at=parse_datetime(p.get("createdAt")),
            source_url=p.get("hostedUrl"),
            apply_url=p.get("applyUrl"),
        )
