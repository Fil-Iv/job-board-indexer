"""HTTP API: job search (Postgres full-text, deduplicated), sources, stats, health."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from jobindex import __version__
from jobindex.api.deps import SessionDep
from jobindex.api.schemas import (
    EmploymentType,
    FailureOut,
    HealthOut,
    JobDetailOut,
    JobSearchOut,
    PlatformStats,
    SourceOut,
    StatsOut,
    StatusFilter,
)
from jobindex.api.search import MAX_PAGE_SIZE, SearchParams, get_opening, group_id, search
from jobindex.api.ui import router as ui_router
from jobindex.config import get_settings
from jobindex.db import create_engine, create_session_factory
from jobindex.models import FetchRun, FetchStatus, Job, JobStatus, RemoteType, Source


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    engine = create_engine(settings.database_url)
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


DESCRIPTION = """
Search job postings collected from ATS platforms (Greenhouse, Lever, Workday, …) and
job-board platforms.

* An **opening** posted on several sources is returned once; `sources` lists every
  listing with its original URL and apply URL.
* Postings are **open** while the latest successful crawl of their source lists them.
  A failed crawl never closes anything — see `GET /stats` for crawl health.
"""

app = FastAPI(
    title="JobIndex",
    version=__version__,
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "jobs", "description": "Search and read openings."},
        {"name": "sources", "description": "Crawled job boards and crawl health."},
        {"name": "ops", "description": "Health check for load balancers and Docker."},
    ],
)


app.include_router(ui_router)


def search_params(
    q: Annotated[
        str | None,
        Query(
            description='Full-text query (web-search syntax): `python "data engineer" -senior`.'
            " Ranked by title, then company, then description."
        ),
    ] = None,
    company: Annotated[str | None, Query(description="Company name contains (any case).")] = None,
    location: Annotated[
        str | None,
        Query(
            description="City, region or country (name or ISO code); comma-separate to combine,"
            " e.g. `Austin, TX` or `Berlin, Germany`."
        ),
    ] = None,
    remote_type: RemoteType | None = None,
    employment_type: EmploymentType | None = None,
    salary_min: Annotated[
        Decimal | None,
        Query(ge=0, description="Upper end of the published salary range is at least this."),
    ] = None,
    salary_currency: Annotated[
        str | None, Query(min_length=3, max_length=3, description="ISO 4217, e.g. `USD`.")
    ] = None,
    posted_since: Annotated[
        date | None,
        Query(description="Posted on or after this date (first-seen date if none is given)."),
    ] = None,
    status: Annotated[
        StatusFilter, Query(description="`any` returns open and closed openings.")
    ] = StatusFilter.OPEN,
    platform: Annotated[str | None, Query(description="Only this platform, e.g. `lever`.")] = None,
    source_id: int | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
) -> SearchParams:
    return SearchParams(
        q=q.strip() if q and q.strip() else None,
        company=company or None,
        location=location or None,
        remote_type=remote_type,
        employment_type=employment_type.value if employment_type else None,
        salary_min=salary_min,
        salary_currency=salary_currency,
        posted_since=posted_since,
        status=status,
        platform=platform or None,
        source_id=source_id,
        page=page,
        size=size,
    )


@app.get("/health", response_model=HealthOut, tags=["ops"], summary="Health check")
async def health(session: SessionDep) -> HealthOut | JSONResponse:
    """`200` when the API and the database answer, `503` otherwise."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    return HealthOut(status="ok", database="ok")


@app.get("/jobs", response_model=JobSearchOut, tags=["jobs"], summary="Search openings")
async def search_jobs(
    session: SessionDep, params: Annotated[SearchParams, Depends(search_params)]
) -> JobSearchOut:
    """One result per opening (deduplicated across sources), newest first, or by
    relevance when `q` is given. Filters match if any listing of the opening matches."""
    return await search(session, params)


@app.get(
    "/jobs/{job_id}",
    response_model=JobDetailOut,
    tags=["jobs"],
    summary="Get one opening",
    responses={404: {"description": "No posting with this ID."}},
)
async def get_job(job_id: int, session: SessionDep) -> JobDetailOut:
    """The opening with its description. Any listing's `job_id` resolves to its opening."""
    opening = await get_opening(session, job_id)
    if opening is None:
        raise HTTPException(status_code=404, detail="job not found")
    return opening


@app.get("/sources", response_model=list[SourceOut], tags=["sources"], summary="List sources")
async def list_sources(
    session: SessionDep,
    platform: str | None = None,
    failing: Annotated[
        bool | None, Query(description="`true`: only sources whose last crawl failed.")
    ] = None,
) -> list[SourceOut]:
    """Every registered job board with its crawl state."""
    open_jobs = (
        select(Job.source_id, func.count().label("n"))
        .where(Job.status == JobStatus.OPEN)
        .group_by(Job.source_id)
        .subquery()
    )
    stmt = (
        select(Source, func.coalesce(open_jobs.c.n, 0))
        .outerjoin(open_jobs, open_jobs.c.source_id == Source.id)
        .order_by(Source.id)
    )
    if platform:
        stmt = stmt.where(Source.platform == platform)
    if failing is not None:
        stmt = stmt.where(
            Source.consecutive_failures > 0 if failing else Source.consecutive_failures == 0
        )
    rows = await session.execute(stmt)
    return [
        SourceOut.model_validate({**_source_dict(source), "open_jobs": n}) for source, n in rows
    ]


def _source_dict(source: Source) -> dict[str, object]:
    return {c.key: getattr(source, c.key) for c in Source.__table__.columns}


@app.get("/stats", response_model=StatsOut, tags=["sources"], summary="Index and crawl stats")
async def stats(session: SessionDep) -> StatsOut:
    """Jobs per platform, last crawl times and recent crawl failures."""
    is_open = Job.status == JobStatus.OPEN
    job_counts = {
        platform: (open_n, total)
        for platform, open_n, total in await session.execute(
            select(
                Source.platform,
                func.count().filter(is_open),
                func.count(),
            )
            .join(Source, Source.id == Job.source_id)
            .group_by(Source.platform)
        )
    }
    active = Source.active.is_(True)
    failing = active & (Source.consecutive_failures > 0)
    platforms = []
    for row in await session.execute(
        select(
            Source.platform,
            func.count(),
            func.count().filter(active),
            func.count().filter(failing),
            func.max(Source.last_success_at),
            func.max(Source.last_failure_at),
        )
        .group_by(Source.platform)
        .order_by(Source.platform)
    ):
        open_n, total = job_counts.get(row[0], (0, 0))
        platforms.append(
            PlatformStats(
                platform=row[0],
                sources=row[1],
                active_sources=row[2],
                failing_sources=row[3],
                open_jobs=open_n,
                total_jobs=total,
                last_success_at=row[4],
                last_failure_at=row[5],
            )
        )

    open_openings = await session.scalar(
        select(func.count(func.distinct(group_id()))).where(is_open)
    )
    runs = (
        await session.execute(
            select(
                func.max(FetchRun.finished_at),
                func.count().filter(FetchRun.status == FetchStatus.RUNNING),
            )
        )
    ).one()
    failures = await session.execute(
        select(FetchRun, Source.platform, Source.board_identifier)
        .join(Source, Source.id == FetchRun.source_id)
        .where(FetchRun.status == FetchStatus.FAILED)
        .order_by(FetchRun.started_at.desc())
        .limit(20)
    )
    return StatsOut(
        sources=sum(p.sources for p in platforms),
        active_sources=sum(p.active_sources for p in platforms),
        failing_sources=sum(p.failing_sources for p in platforms),
        open_jobs=sum(p.open_jobs for p in platforms),
        open_openings=open_openings or 0,
        total_jobs=sum(p.total_jobs for p in platforms),
        running_crawls=runs[1],
        last_crawl_at=runs[0],
        platforms=platforms,
        recent_failures=[
            FailureOut(
                fetch_run_id=run.id,
                source_id=run.source_id,
                platform=platform,
                board_identifier=board,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error=run.error,
            )
            for run, platform, board in failures
        ],
    )
