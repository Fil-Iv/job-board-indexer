"""Browse by place: ``/browse`` (countries) → ``/browse/{CC}`` (regions and cities) →
the search page filtered to that place.

Uses the normalised location fields of open postings (``country`` ISO code, ``region``,
``city``); links go to ``/?location=City, Region, CC``, which the search's location
filter matches part by part. Counts are openings (deduplicated across sources).
"""

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from jobindex.api.deps import SessionDep
from jobindex.api.pulse import CACHE_SECONDS, country_name
from jobindex.api.search import group_id
from jobindex.api.ui import header_stats, templates
from jobindex.models import Job, JobStatus

MAX_REGIONS = 100
MAX_CITIES = 200


@dataclass(slots=True)
class Place:
    label: str
    count: int
    href: str
    detail: str | None = None  # e.g. the region of a city


def search_url(*parts: str | None) -> str:
    return "/?" + urlencode({"location": ", ".join(p for p in parts if p)})


def _cached(response: HTMLResponse) -> HTMLResponse:
    response.headers["Cache-Control"] = (
        f"public, max-age=0, s-maxage={CACHE_SECONDS}, stale-while-revalidate={CACHE_SECONDS}"
    )
    return response


router = APIRouter(include_in_schema=False)


@router.get("/browse", response_class=HTMLResponse)
async def browse_countries(request: Request, session: SessionDep) -> HTMLResponse:
    openings = func.count(func.distinct(group_id()))
    rows = await session.execute(
        select(Job.country, openings)
        .where(Job.status == JobStatus.OPEN, Job.country.is_not(None))
        .group_by(Job.country)
        .order_by(openings.desc(), Job.country)
    )
    countries = [Place(country_name(code), n, f"/browse/{code}", code) for code, n in rows]
    return _cached(
        templates.TemplateResponse(
            request,
            "browse.html",
            {
                "nav": "browse",
                "stats": await header_stats(session),
                "country": None,
                "countries": countries,
            },
        )
    )


@router.get("/browse/{code}", response_class=HTMLResponse)
async def browse_country(
    request: Request,
    session: SessionDep,
    code: Annotated[str, Path(description="ISO 3166-1 alpha-2 country code.")],
) -> HTMLResponse:
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise HTTPException(status_code=404, detail="unknown country")
    code = code.upper()
    openings = func.count(func.distinct(group_id()))
    in_country = (Job.status == JobStatus.OPEN, Job.country == code)

    total = await session.scalar(select(openings).where(*in_country)) or 0
    regions = [
        Place(region, n, search_url(region, code))
        for region, n in await session.execute(
            select(Job.region, openings)
            .where(*in_country, Job.region.is_not(None), Job.region != "")
            .group_by(Job.region)
            .order_by(openings.desc(), Job.region)
            .limit(MAX_REGIONS)
        )
    ]
    cities = [
        Place(city, n, search_url(city, region, code), region or None)
        for city, region, n in await session.execute(
            select(Job.city, Job.region, openings)
            .where(*in_country, Job.city.is_not(None), Job.city != "")
            .group_by(Job.city, Job.region)
            .order_by(openings.desc(), Job.city)
            .limit(MAX_CITIES)
        )
    ]
    return _cached(
        templates.TemplateResponse(
            request,
            "browse.html",
            {
                "nav": "browse",
                "stats": await header_stats(session),
                "country": Place(country_name(code), total, search_url(code), code),
                "regions": regions,
                "cities": cities,
            },
        )
    )
