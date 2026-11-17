"""Platform coverage page at ``/coverage``: every required platform (58 ATS, 20 job
boards) with its status, the hosted index's boards for it (open jobs, last successful
crawl, current failure) and, for blocked platforms, the recorded evidence.

Static facts come from the catalog snapshot (``jobindex.catalog``); live numbers from the
``sources`` and ``jobs`` tables. Cached like ``/pulse``.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from jobindex.api.deps import SessionDep
from jobindex.api.pulse import CACHE_SECONDS
from jobindex.api.ui import header_stats, templates
from jobindex.catalog import catalog
from jobindex.models import Job, JobStatus, Source

STATUSES = ("live", "partial", "blocked", "not built")
TYPE_TITLES = {"ats": "Applicant tracking systems", "jobboard": "Job-board platforms"}


@dataclass(slots=True)
class Board:
    id: int
    board: str
    url: str
    open_jobs: int
    last_success_at: datetime | None
    failures: int
    last_error: str | None


@dataclass(slots=True)
class PlatformRow:
    key: str
    name: str
    type: str
    status: str
    partial: str | None
    examples: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    boards: list[Board] = field(default_factory=list)

    @property
    def open_jobs(self) -> int:
        return sum(b.open_jobs for b in self.boards)

    @property
    def last_success_at(self) -> datetime | None:
        return max((b.last_success_at for b in self.boards if b.last_success_at), default=None)


router = APIRouter(include_in_schema=False)


@router.get("/coverage", response_class=HTMLResponse)
async def coverage_page(request: Request, session: SessionDep) -> HTMLResponse:
    open_jobs = (
        select(Job.source_id, func.count().label("n"))
        .where(Job.status == JobStatus.OPEN)
        .group_by(Job.source_id)
        .subquery()
    )
    boards: dict[str, list[Board]] = {}
    for source, n in await session.execute(
        select(Source, func.coalesce(open_jobs.c.n, 0))
        .outerjoin(open_jobs, open_jobs.c.source_id == Source.id)
        .where(Source.active.is_(True))
        .order_by(Source.platform, func.coalesce(open_jobs.c.n, 0).desc(), Source.id)
    ):
        boards.setdefault(source.platform, []).append(
            Board(
                id=source.id,
                board=source.board_identifier,
                url=source.base_url,
                open_jobs=n,
                last_success_at=source.last_success_at,
                failures=source.consecutive_failures,
                last_error=source.last_error,
            )
        )
    rows = [
        PlatformRow(
            key=p["key"],
            name=p["name"],
            type=p["type"],
            status=p["status"],
            partial=p["partial"],
            examples=p["examples"],
            blocks=p["blocks"],
            boards=boards.get(p["key"], []),
        )
        for p in catalog()["platforms"]
    ]
    counts = {s: sum(r.status == s for r in rows) for s in STATUSES}
    groups = [(TYPE_TITLES[t], [r for r in rows if r.type == t]) for t in TYPE_TITLES]
    response = templates.TemplateResponse(
        request,
        "coverage.html",
        {
            "nav": "coverage",
            "stats": await header_stats(session),
            "rows": rows,
            "groups": groups,
            "counts": counts,
            "statuses": STATUSES,
        },
    )
    response.headers["Cache-Control"] = (
        f"public, max-age=0, s-maxage={CACHE_SECONDS}, stale-while-revalidate={CACHE_SECONDS}"
    )
    return response
