"""Server-rendered search page at ``/`` (Jinja2, no build step).

It runs the same search as ``GET /jobs``. Form values are parsed leniently: an empty or
invalid field is ignored instead of producing a 422 error page.

The page has two themes, both in ``templates/index.html`` (plain CSS, no build step): the
default "Dark Fantasy" look (rendered server-side as ``data-theme="fantasy"``) and a clean
light look, switched by a header button and remembered in ``localStorage``.
The choice is purely client-side, so CDN caching still works.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api.deps import SessionDep
from jobindex.api.schemas import EmploymentType, StatusFilter
from jobindex.api.search import SearchParams, group_id, search
from jobindex.models import Job, JobStatus, RemoteType, Source

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PAGE_SIZE = 20


def safe_url(url: str | None) -> str | None:
    """Only http(s) links from crawled data reach an ``href`` (no ``javascript:``)."""
    if url and urlsplit(url).scheme.lower() in {"http", "https"}:
        return url
    return None


templates.env.filters["safe_url"] = safe_url


def _enum[E: (RemoteType, EmploymentType, StatusFilter)](
    cls: type[E], value: str | None, default: E | None = None
) -> E | None:
    try:
        return cls(value) if value else default
    except ValueError:
        return default


def _decimal(value: str | None) -> Decimal | None:
    try:
        number = Decimal(value.replace(",", "").strip()) if value else None
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() and number >= 0 else None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _page(value: str | None) -> int:
    return int(value) if value and value.isdigit() and 0 < int(value) < 10_000 else 1


def _platform(value: str | None) -> str | None:
    """Connector keys are lowercase ``[a-z0-9_]``; anything else matches nothing anyway."""
    value = (value or "").strip().lower()
    return value if value and len(value) <= 64 and value.replace("_", "").isalnum() else None


def form_params(request: Request) -> SearchParams:
    qp = request.query_params
    employment = _enum(EmploymentType, qp.get("employment_type"))
    return SearchParams(
        q=(qp.get("q") or "").strip() or None,
        company=(qp.get("company") or "").strip() or None,
        location=(qp.get("location") or "").strip() or None,
        remote_type=_enum(RemoteType, qp.get("remote_type")),
        employment_type=employment.value if employment else None,
        salary_min=_decimal(qp.get("salary_min")),
        posted_since=_date(qp.get("posted_since")),
        status=_enum(StatusFilter, qp.get("status"), StatusFilter.OPEN) or StatusFilter.OPEN,
        platform=_platform(qp.get("platform")),
        page=_page(qp.get("page")),
        size=PAGE_SIZE,
    )


@dataclass(slots=True)
class HeaderStats:
    """Live totals in the page header (the same numbers as ``GET /stats``)."""

    open_openings: int
    platforms: list[tuple[str, int]]  # (platform, open postings), platforms with open postings
    last_crawl_at: datetime | None


async def header_stats(session: AsyncSession) -> HeaderStats:
    is_open = Job.status == JobStatus.OPEN
    per_platform = await session.execute(
        select(Source.platform, func.count())
        .select_from(Job)
        .join(Source, Source.id == Job.source_id)
        .where(is_open)
        .group_by(Source.platform)
        .order_by(Source.platform)
    )
    openings = await session.scalar(select(func.count(func.distinct(group_id()))).where(is_open))
    last_crawl = await session.scalar(select(func.max(Source.last_success_at)))
    return HeaderStats(
        open_openings=openings or 0,
        platforms=[(platform, n) for platform, n in per_platform],
        last_crawl_at=last_crawl,
    )


router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: SessionDep,
    params: Annotated[SearchParams, Depends(form_params)],
) -> HTMLResponse:
    results = await search(session, params)
    stats = await header_stats(session)
    form = {k: v for k, v in request.query_params.items() if k != "page" and v}

    def page_url(page: int) -> str:
        return "/?" + urlencode({**form, "page": page})

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "form": form,
            "params": params,
            "results": results,
            "stats": stats,
            "page_url": page_url,
            "remote_types": [r.value for r in RemoteType if r is not RemoteType.UNKNOWN],
            "employment_types": [e.value for e in EmploymentType],
        },
    )
