"""RSS 2.0 feed of any search: ``/jobs.rss`` takes the same query parameters as the
search page (parsed just as leniently) and lists the newest matching openings, so a
search can be followed in a feed reader without an account."""

import re
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Annotated
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Depends, Request, Response

from jobindex.api.deps import SessionDep
from jobindex.api.schemas import JobOut
from jobindex.api.search import SearchParams, search
from jobindex.api.ui import form_params, public_url, safe_url
from jobindex.catalog import platform_name

FEED_SIZE = 50
ATOM = "http://www.w3.org/2005/Atom"
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _text(value: str) -> str:
    """Crawled text may contain control characters, which XML 1.0 forbids."""
    return _XML_INVALID.sub("", value)


def _rfc822(value: datetime) -> str:
    return format_datetime(value.astimezone(UTC), usegmt=True)


def feed_title(p: SearchParams) -> str:
    parts = [f'"{p.q}"' if p.q else None, p.company, p.location]
    if p.remote_type is not None:
        parts.append(p.remote_type.value)
    if p.employment_type:
        parts.append(p.employment_type.replace("_", " "))
    if p.platform:
        parts.append(f"on {platform_name(p.platform)}")
    described = ", ".join(x for x in parts if x)
    return f"JobForge: {described}" if described else "JobForge: newest jobs"


def _item(channel: Element, job: JobOut, search_page: str) -> None:
    item = SubElement(channel, "item")
    title = f"{job.title} at {job.company}" if job.company else job.title
    SubElement(item, "title").text = _text(title)
    main = job.sources[0] if job.sources else None
    link = (safe_url(main.source_url) or safe_url(main.apply_url)) if main else None
    SubElement(item, "link").text = link or search_page
    SubElement(item, "guid", isPermaLink="false").text = f"jobforge-opening-{job.id}"
    SubElement(item, "pubDate").text = _rfc822(job.posted_at or job.first_seen_at)
    details = []
    place = ", ".join(p for p in (job.city, job.region, job.country) if p) or job.location_raw
    if place:
        details.append(place)
    if job.remote_type.value != "unknown":
        details.append(job.remote_type.value)
    if job.employment_type:
        details.append(job.employment_type.replace("_", " "))
    if job.salary_min is not None or job.salary_max is not None:
        low, high = job.salary_min, job.salary_max
        amount = "–".join(f"{v:,.0f}" for v in dict.fromkeys(x for x in (low, high) if x))
        period = f" / {job.salary_period}" if job.salary_period else ""
        details.append(f"{job.salary_currency or ''} {amount}{period}".strip())
    names = dict.fromkeys(platform_name(s.platform) for s in job.sources)
    details.append("via " + ", ".join(names))
    SubElement(item, "description").text = _text(" · ".join(details))
    if job.company:
        SubElement(item, "category").text = _text(job.company)


router = APIRouter(include_in_schema=False)


@router.get("/jobs.rss")
async def jobs_feed(
    request: Request,
    session: SessionDep,
    params: Annotated[SearchParams, Depends(form_params)],
) -> Response:
    params.page, params.size, params.newest_first = 1, FEED_SIZE, True
    results = await search(session, params)
    query = urlencode({k: v for k, v in request.query_params.items() if k != "page" and v})
    search_page = public_url(request, "/" + (f"?{query}" if query else ""))
    self_url = public_url(request, "/jobs.rss" + (f"?{query}" if query else ""))

    rss = Element("rss", version="2.0", attrib={"xmlns:atom": ATOM})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = _text(feed_title(params))
    SubElement(channel, "link").text = search_page
    SubElement(
        channel, "description"
    ).text = f"Newest of {results.total:,} open jobs matching this JobForge search."
    SubElement(channel, "atom:link", href=self_url, rel="self", type="application/rss+xml")
    SubElement(channel, "ttl").text = "60"
    if results.items:
        newest = max(j.posted_at or j.first_seen_at for j in results.items)
        SubElement(channel, "lastBuildDate").text = _rfc822(newest)
    for job in results.items:
        _item(channel, job, search_page)

    body = b'<?xml version="1.0" encoding="utf-8"?>\n' + tostring(rss, encoding="unicode").encode()
    return Response(body, media_type="application/rss+xml; charset=utf-8")
