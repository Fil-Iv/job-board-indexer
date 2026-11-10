"""Extract schema.org ``JobPosting`` data from HTML (JSON-LD and microdata).

Many job pages describe themselves for search engines (Google for Jobs) with a
schema.org JobPosting, either as JSON-LD (``<script type="application/ld+json">``) or as
microdata (``itemscope itemtype="http://schema.org/JobPosting"``). Both are turned into
the same plain-dict shape (property name -> value; nested items -> dicts) and then into a
:class:`~jobindex.connectors.base.RawJob` by :func:`to_raw_job`.

Used by the SmartRecruiters HTML path and the generic ``jsonld`` job-board connector.
"""

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobindex.connectors.base import RawJob
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    html_to_text,
    normalize_country,
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    parse_location,
    remote_type_from_value,
)

log = logging.getLogger(__name__)

Item = dict[str, Any]

_NOTE = re.compile(r"\s*\([^)]*\)")
_VALUE_ATTRS = {"meta": "content", "a": "href", "link": "href", "time": "datetime", "img": "src"}


def _is_job_posting(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    types = item.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.rsplit("/", 1)[-1] == "JobPosting" for t in types)


def _walk_jsonld(node: Any) -> list[Item]:
    found: list[Item] = []
    if isinstance(node, list):
        for child in node:
            found += _walk_jsonld(child)
    elif isinstance(node, dict):
        if _is_job_posting(node):
            found.append(node)
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                found += _walk_jsonld(node[key])
    return found


def _jsonld_postings(tree: LexborHTMLParser) -> list[Item]:
    postings: list[Item] = []
    for script in tree.css('script[type="application/ld+json"]'):
        text = script.text(deep=True, strip=False).strip()
        if not text:
            continue
        try:
            data = json.loads(text, strict=False)  # tolerate raw newlines in strings
        except ValueError:
            log.debug("skipping invalid JSON-LD block")
            continue
        postings += _walk_jsonld(data)
    return postings


def _microdata_value(node: LexborNode) -> Any:
    attr = _VALUE_ATTRS.get(node.tag or "")
    if attr and node.attributes.get(attr) is not None:
        return node.attributes[attr]
    if node.attributes.get("content") is not None:
        return node.attributes["content"]
    return node.inner_html or ""


def _microdata_scope(scope: LexborNode) -> Item:
    item: Item = {"@type": (scope.attributes.get("itemtype") or "").split()}

    def visit(node: LexborNode) -> None:
        for child in node.iter(include_text=False):
            props = (child.attributes.get("itemprop") or "").split()
            nested = "itemscope" in child.attributes
            if props:
                value: Any = _microdata_scope(child) if nested else _microdata_value(child)
                for prop in props:
                    existing = item.get(prop)
                    if existing is None:
                        item[prop] = value
                    elif isinstance(existing, list):
                        existing.append(value)
                    else:
                        item[prop] = [existing, value]
            if not nested:  # properties of a nested item belong to that item
                visit(child)

    visit(scope)
    return item


def _microdata_postings(tree: LexborHTMLParser) -> list[Item]:
    postings: list[Item] = []
    for scope in tree.css("[itemscope][itemtype]"):
        types = (scope.attributes.get("itemtype") or "").split()
        if any(t.rsplit("/", 1)[-1] == "JobPosting" for t in types):
            postings.append(_microdata_scope(scope))
    return postings


def count_job_postings(html: str) -> tuple[int, int]:
    """(JSON-LD JobPostings, microdata JobPostings) on the page."""
    tree = LexborHTMLParser(html)
    return len(_jsonld_postings(tree)), len(_microdata_postings(tree))


def extract_job_postings(html: str) -> list[Item]:
    """All JobPosting items on the page (JSON-LD first, then microdata)."""
    tree = LexborHTMLParser(html)
    return _jsonld_postings(tree) + _microdata_postings(tree)


# ---------------------------------------------------------------------------
# Item -> RawJob
# ---------------------------------------------------------------------------


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text(value: Any) -> str | None:
    value = _first(value)
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or value.get("@id")
    if value is None:
        return None
    text = html_to_text(unescape(str(value)))
    return text or None


def _decimal(value: Any) -> Decimal | None:
    value = _first(value)
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _places(item: Item) -> list[tuple[str | None, str | None, str | None]]:
    """(locality, region, country) for each jobLocation."""
    raw = item.get("jobLocation")
    places = raw if isinstance(raw, list) else [raw] if raw else []
    result: list[tuple[str | None, str | None, str | None]] = []
    for place in places:
        if not isinstance(place, dict):
            result.append((_text(place), None, None))
            continue
        address = place.get("address") or {}
        if isinstance(address, list):
            address = address[0] if address else {}
        if not isinstance(address, dict):
            result.append((_text(address), None, None))
            continue
        result.append(
            _clean_address(
                _text(address.get("addressLocality")),
                _text(address.get("addressRegion")),
                _text(address.get("addressCountry")),
            )
        )
    return result


def _clean_address(
    locality: str | None, region: str | None, country: str | None
) -> tuple[str | None, str | None, str | None]:
    """Undo two common address quirks so the location reads "City, Region, Country".

    * Eightfold: ``addressRegion`` repeats the country code (``"KA,IN"`` with country IN).
    * JBoard: the whole location in ``addressLocality`` (``"Austin, Texas, United States"``)
      and no region/country (JobKit: "New York, NY (Flatiron)"); it is split by
      :func:`~jobindex.normalize.parse_location`, without the parenthesised note.
    """
    if region and "," in region:
        head, _, tail = region.rpartition(",")
        tail_country = normalize_country(tail.strip())
        if head.strip() and tail_country and normalize_country(country) in (None, tail_country):
            region, country = head.strip(), country or tail.strip()
    if locality and "," in locality and not region and not country:
        # JobKit adds a neighbourhood note: "New York, NY (Flatiron)".
        locality = _NOTE.sub("", locality).strip()
        loc = parse_location(locality)
        if loc.city:
            return loc.city, loc.region, loc.country
    return locality, region, country


def is_expired(item: Item, now: datetime | None = None) -> bool:
    """True if ``validThrough`` is in the past (the posting should not be indexed)."""
    valid_through = parse_datetime(_text(item.get("validThrough")))
    return valid_through is not None and valid_through < (now or datetime.now(UTC))


def to_raw_job(item: Item, *, external_id: str, source_url: str | None) -> RawJob:
    """Map a JobPosting item to a RawJob (un-normalised; call ``.normalized()`` later)."""
    places = _places(item)
    city, region, country = places[0] if places else (None, None, None)
    location_raw = "; ".join(", ".join(p for p in place if p) for place in places) or None

    location_type = (_text(item.get("jobLocationType")) or "").upper()
    remote_type = RemoteType.REMOTE if location_type == "TELECOMMUTE" else RemoteType.UNKNOWN

    salary_min = salary_max = None
    currency = period = None
    salary = _first(item.get("baseSalary"))
    if isinstance(salary, dict):
        currency = _text(salary.get("currency"))
        value = salary.get("value")
        if isinstance(value, dict):
            salary_min = _decimal(value.get("minValue")) or _decimal(value.get("value"))
            salary_max = _decimal(value.get("maxValue")) or salary_min
            period = normalize_salary_period(_text(value.get("unitText")))
        else:
            salary_min = salary_max = _decimal(value)
        if salary_min is None:
            currency = period = None

    organization = _first(item.get("hiringOrganization"))
    company = (
        _text(organization.get("name")) if isinstance(organization, dict) else _text(organization)
    )
    description = _first(item.get("description"))
    url = _text(item.get("url"))
    return RawJob(
        external_id=external_id,
        title=_text(item.get("title")) or "",
        company=company,
        description_html=unescape(str(description)) if description else None,
        location_raw=location_raw,
        city=city,
        region=region,
        country=country,
        remote_type=remote_type,
        employment_type=normalize_employment_type(_text(item.get("employmentType"))),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
        posted_at=parse_datetime(_text(item.get("datePosted"))),
        source_url=source_url or url,
        apply_url=url or source_url,
    )


__all__ = [
    "count_job_postings",
    "extract_job_postings",
    "is_expired",
    "remote_type_from_value",
    "to_raw_job",
]
