"""HTTP API: health check, job search (Postgres full-text) and sources."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex import __version__
from jobindex.api.schemas import JobDetailOut, JobOut, JobSearchOut, SourceOut
from jobindex.config import get_settings
from jobindex.db import create_engine, create_session_factory
from jobindex.http import HttpClient
from jobindex.models import Job, JobStatus, RemoteType, Source
from jobindex.scheduler import build_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    engine = create_engine(settings.database_url)
    app.state.session_factory = create_session_factory(engine)

    scheduler = None
    http: HttpClient | None = None
    if settings.scheduler_enabled:
        http = HttpClient.from_settings(settings)
        scheduler = build_scheduler(app.state.session_factory, http, settings)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if http is not None:
            await http.aclose()
        await engine.dispose()


app = FastAPI(title="JobIndex", version=__version__, lifespan=lifespan)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs", response_model=JobSearchOut)
async def search_jobs(
    session: SessionDep,
    q: Annotated[str | None, Query(description="Web-search syntax, e.g. python -senior")] = None,
    status: JobStatus | None = JobStatus.OPEN,
    remote_type: RemoteType | None = None,
    country: str | None = None,
    source_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobSearchOut:
    stmt = select(Job)
    if status is not None:
        stmt = stmt.where(Job.status == status)
    if remote_type is not None:
        stmt = stmt.where(Job.remote_type == remote_type)
    if country is not None:
        stmt = stmt.where(func.lower(Job.country) == country.lower())
    if source_id is not None:
        stmt = stmt.where(Job.source_id == source_id)

    if q:
        tsquery = func.websearch_to_tsquery("english", q)
        stmt = stmt.where(Job.search_vector.op("@@")(tsquery))
        order = [func.ts_rank(Job.search_vector, tsquery).desc(), Job.id.desc()]
    else:
        order = [Job.posted_at.desc().nulls_last(), Job.id.desc()]

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(stmt.order_by(*order).limit(limit).offset(offset))
    return JobSearchOut(total=total or 0, items=[JobOut.model_validate(job) for job in rows.all()])


@app.get("/jobs/{job_id}", response_model=JobDetailOut)
async def get_job(job_id: int, session: SessionDep) -> JobDetailOut:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobDetailOut.model_validate(job)


@app.get("/sources", response_model=list[SourceOut])
async def list_sources(session: SessionDep) -> list[SourceOut]:
    rows = await session.scalars(select(Source).order_by(Source.id))
    return [SourceOut.model_validate(source) for source in rows.all()]
