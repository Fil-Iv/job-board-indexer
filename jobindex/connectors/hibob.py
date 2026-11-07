"""HiBob career sites (``{company}.careers.hibob.com``; public JSON behind the SPA).

List: GET https://{company}.careers.hibob.com/api/job-ad
      with header ``companyIdentifier: {company}`` (what the careers SPA sends; without it
      the API answers 401, as it does for unknown companies)
      -> {filterGroups, jobOpeningMetadata,
          jobAdDetails: [{id (uuid), title, department, employmentType, site, country,
                          description, responsibilities, requirements, benefits (HTML),
                          sectionLabels{field: heading}, publishedAt, workspaceType
                          (Hybrid|Remote|On site), postingLocations[{city, region,
                          country, displayValue}], payTransparencyMinSalary/MaxSalary/
                          SalaryCurrency/SalaryPayPeriod ("Annual", "Monthly 14")}]}
      Every published job ad with descriptions, one request (verified on sapiens and
      liquidia, 2026-11-07).
Job page: https://{company}.careers.hibob.com/jobs/{id} (SPA route).

Board identifier: the subdomain, e.g. ``sapiens``.

URL formats recognised:
    {company}.careers.hibob.com[/jobs/{id}]
Custom-domain career pages: links/iframes to the above (generic HTML detection).
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

_SUFFIX = ".careers.hibob.com"
_COMPANY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Section fields in page order; headings come from ``sectionLabels`` (else these).
_SECTIONS = (
    ("description", None),
    ("responsibilities", "Responsibilities"),
    ("requirements", "Requirements"),
    ("benefits", "Benefits"),
)


def _description(ad: dict[str, Any]) -> str | None:
    labels = ad.get("sectionLabels") or {}
    parts = []
    for field, default in _SECTIONS:
        html = ad.get(field)
        if not html:
            continue
        heading = labels.get(field) or default
        parts.append(f"<h3>{heading}</h3>{html}" if heading else str(html))
    return "\n".join(parts) or None


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@register
class HiBobConnector(BaseConnector):
    platform = "hibob"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(_SUFFIX):
            return None
        company = host.removesuffix(_SUFFIX)
        return company if _COMPANY.match(company) else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}{_SUFFIX}/"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = f"https://{board_identifier}{_SUFFIX}/api/job-ad"
        response = await self.http.get(
            url, headers={"Accept": "application/json", "companyIdentifier": board_identifier}
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ConnectorError(f"hibob: {url} is not JSON") from exc
        ads = data.get("jobAdDetails") if isinstance(data, dict) else None
        if not isinstance(ads, list):
            raise ConnectorError("hibob: response has no 'jobAdDetails' list")
        return [self._map(ad, board_identifier) for ad in ads]

    @staticmethod
    def _map(ad: dict[str, Any], board_identifier: str) -> RawJob:
        if not ad.get("id"):
            raise ConnectorError("hibob: job ad without 'id'")
        locations = [loc for loc in ad.get("postingLocations") or [] if isinstance(loc, dict)]
        names = [loc.get("displayValue") or loc.get("name") for loc in locations]
        location_raw = "; ".join(dict.fromkeys(str(n) for n in names if n)) or ad.get("site")
        single = locations[0] if len(locations) == 1 else {}
        low = _amount(ad.get("payTransparencyMinSalary"))
        high = _amount(ad.get("payTransparencyMaxSalary"))
        has_pay = low is not None or high is not None
        url = f"https://{board_identifier}{_SUFFIX}/jobs/{ad['id']}"
        return RawJob(
            external_id=str(ad["id"]),
            title=str(ad.get("title") or "").strip(),
            description_html=_description(ad),
            location_raw=location_raw or None,
            city=single.get("city") or None,
            region=single.get("region") or None,
            country=single.get("country") or ad.get("country") or None,
            remote_type=remote_type_from_value(ad.get("workspaceType")),
            employment_type=normalize_employment_type(ad.get("employmentType")),
            salary_min=low,
            salary_max=high,
            salary_currency=(ad.get("payTransparencySalaryCurrency") or None) if has_pay else None,
            salary_period=normalize_salary_period(ad.get("payTransparencySalaryPayPeriod"))
            if has_pay
            else None,
            posted_at=parse_datetime(ad.get("publishedAt")),
            source_url=url,
            apply_url=url,
        )
