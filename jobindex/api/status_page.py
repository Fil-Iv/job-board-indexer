"""Crawl status page at ``/status``: every source's latest crawl (time, result, jobs
found / new / updated / closed), the most recent runs, the sources failing right now and
a live example of the rule that a failed fetch never closes jobs.

The example is the latest failed run of a source that still has open jobs which that
run did not see (``last_seen_at`` before the run): they stayed open because the fetch
failed. Numbers come straight from ``fetch_runs``, ``sources`` and ``jobs``.
"""

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api.deps import SessionDep
from jobindex.api.ui import header_stats, templates
from jobindex.models import FetchRun, FetchStatus, Job, JobStatus, Source

CACHE_SECONDS = 120
RECENT_RUNS = 40


@dataclass(slots=True)
class Run:
    id: int
    source_id: int
    platform: str
    board: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    found: int
    new: int
    updated: int
    closed: int
    error: str | None

    @property
    def seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True)
class KeptOpen:
    """A failed run whose source's open jobs were left untouched."""

    run: Run
    kept_open: int  # open jobs of the source the failed run did not see
    last_success_at: datetime | None


def _run(run: FetchRun, platform: str, board: str) -> Run:
    return Run(
        id=run.id,
        source_id=run.source_id,
        platform=platform,
        board=board,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value,
        found=run.jobs_found,
        new=run.jobs_new,
        updated=run.jobs_updated,
        closed=run.jobs_closed,
        error=run.error,
    )


router = APIRouter(include_in_schema=False)


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request, session: SessionDep) -> HTMLResponse:
    with_source = select(FetchRun, Source.platform, Source.board_identifier).join(
        Source, Source.id == FetchRun.source_id
    )
    latest = [
        _run(*row)
        for row in await session.execute(
            with_source.distinct(FetchRun.source_id).order_by(
                FetchRun.source_id, FetchRun.started_at.desc(), FetchRun.id.desc()
            )
        )
    ]
    latest.sort(key=lambda r: (r.status != FetchStatus.FAILED.value, r.platform, r.board))
    recent = [
        _run(*row)
        for row in await session.execute(
            with_source.order_by(FetchRun.started_at.desc(), FetchRun.id.desc()).limit(RECENT_RUNS)
        )
    ]
    failing = list(
        await session.scalars(
            select(Source)
            .where(Source.active.is_(True), Source.consecutive_failures > 0)
            .order_by(Source.consecutive_failures.desc(), Source.platform, Source.id)
        )
    )
    example = await kept_open_example(session)
    totals = {
        "runs": sum(1 for _ in latest),
        "ok": sum(r.status == FetchStatus.SUCCESS.value for r in latest),
        "failed": sum(r.status == FetchStatus.FAILED.value for r in latest),
        "running": sum(r.status == FetchStatus.RUNNING.value for r in latest),
    }
    response = templates.TemplateResponse(
        request,
        "status.html",
        {
            "nav": "status",
            "stats": await header_stats(session),
            "latest": latest,
            "recent": recent,
            "failing": failing,
            "example": example,
            "totals": totals,
        },
    )
    response.headers["Cache-Control"] = (
        f"public, max-age=0, s-maxage={CACHE_SECONDS}, stale-while-revalidate={CACHE_SECONDS}"
    )
    return response


async def kept_open_example(session: AsyncSession) -> KeptOpen | None:
    """The latest failed run after which the source still had open jobs it did not
    see: proof that a failure closed nothing."""
    not_seen = (
        select(func.count())
        .where(
            Job.source_id == FetchRun.source_id,
            Job.status == JobStatus.OPEN,
            Job.last_seen_at < FetchRun.started_at,
        )
        .correlate(FetchRun)
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(
                FetchRun,
                Source.platform,
                Source.board_identifier,
                not_seen,
                Source.last_success_at,
            )
            .join(Source, Source.id == FetchRun.source_id)
            .where(FetchRun.status == FetchStatus.FAILED, not_seen > 0)
            .order_by(FetchRun.started_at.desc(), FetchRun.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    run, platform, board, kept, last_success = row
    return KeptOpen(run=_run(run, platform, board), kept_open=kept, last_success_at=last_success)
