"""Personio recruiting pages (``{company}.jobs.personio.de``; public XML feed).

List: GET https://{company}.jobs.personio.de/xml
      -> <workzag-jobs><position><id/><subcompany/><office/><additionalOffices><office/>...
         <department/><name/><jobDescriptions><jobDescription><name/><value>CDATA HTML
         </value></jobDescription>...</jobDescriptions><employmentType/><schedule/>
         <seniority/><createdAt/><salaryInformation><min/><max/><currencyCode/><type/>
         </salaryInformation></position>...</workzag-jobs>
      Every published position, with descriptions, in one response.

Board identifier: the Personio subdomain, e.g. ``tado``.

Quirks (verified 2026-11-07):
* Do not pass ``?language=``: positions not written in that language come back with empty
  descriptions (thermondo with language=en: 7 of 196 described). Without it every position
  is in the account's own language.
* ``{company}.jobs.personio.de`` and ``.jobs.personio.com`` serve the same accounts.
* Unknown subdomains serve a deny-all robots.txt (and 404): the HttpClient refuses them.

Parsed with defusedxml (no entity expansion on untrusted input).

URL formats recognised:
    {company}.jobs.personio.de[/job/{id}[...]]    {company}.jobs.personio.com[/...]
    {company}.jobs.personio.de/xml
Custom-domain career pages: links/iframes to the above (generic HTML detection).
"""

import re
from decimal import Decimal, InvalidOperation
from html import escape
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException, ElementTree

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.normalize import (
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    split_url,
)

_HOST = re.compile(r"^(?P<company>[a-z0-9][a-z0-9-]{0,99})\.jobs\.personio\.(?:de|com)$")
_RESERVED = {"www", "api", "app", "help"}
_SALARY_PERIOD = {
    "yearly": "year",
    "monthly": "month",
    "weekly": "week",
    "daily": "day",
    "hourly": "hour",
}


def _text(element: Element | None, path: str) -> str | None:
    found = element.find(path) if element is not None else None
    value = (found.text or "").strip() if found is not None else ""
    return value or None


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _description(position: Element) -> str | None:
    parts = []
    for section in position.findall("./jobDescriptions/jobDescription"):
        body = _text(section, "value")
        if not body:
            continue
        name = _text(section, "name")
        if name:
            parts.append(f"<h3>{escape(name)}</h3>")
        parts.append(body)
    return "\n".join(parts) or None


@register
class PersonioConnector(BaseConnector):
    platform = "personio"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        match = _HOST.match(host)
        if not match or match["company"] in _RESERVED:
            return None
        return match["company"]

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.jobs.personio.de"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        response = await self.http.get(f"https://{board_identifier}.jobs.personio.de/xml")
        try:
            root = ElementTree.fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise ConnectorError(f"personio: feed is not valid/safe XML ({exc!r})") from exc
        if root.tag != "workzag-jobs":
            raise ConnectorError(f"personio: unexpected root element <{root.tag}>")
        return [self._map(p, board_identifier) for p in root.findall("position")]

    @staticmethod
    def _map(position: Element, board_identifier: str) -> RawJob:
        job_id = _text(position, "id")
        if not job_id:
            raise ConnectorError("personio: position without <id>")
        offices = [_text(position, "office")]
        offices += [(o.text or "").strip() for o in position.findall("./additionalOffices/office")]
        location_raw = "; ".join(dict.fromkeys(o for o in offices if o)) or None

        salary = position.find("salaryInformation")
        salary_min = _decimal(_text(salary, "min"))
        salary_max = _decimal(_text(salary, "max"))
        has_salary = salary_min is not None or salary_max is not None
        period_raw = _text(salary, "type") or ""
        period = _SALARY_PERIOD.get(period_raw) or normalize_salary_period(period_raw)

        # "schedule" (full-time/part-time) is more telling than "employmentType" (permanent);
        # internships/trainee/freelance come from employmentType.
        employment = normalize_employment_type(_text(position, "employmentType"))
        if employment in (None, "full_time"):
            employment = normalize_employment_type(_text(position, "schedule")) or employment
        url = f"https://{board_identifier}.jobs.personio.de/job/{job_id}"
        return RawJob(
            external_id=job_id,
            title=_text(position, "name") or "",
            company=_text(position, "subcompany"),
            description_html=_description(position),
            location_raw=location_raw,
            employment_type=employment,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=_text(salary, "currencyCode") if has_salary else None,
            salary_period=period if has_salary else None,
            posted_at=parse_datetime(_text(position, "createdAt")),
            source_url=url,
            apply_url=f"{url}#apply",
        )
