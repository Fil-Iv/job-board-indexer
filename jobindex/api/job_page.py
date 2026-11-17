"""Job detail page at ``/job/{id}``: one opening with its description, pay, type,
workplace, dates, status, change history (``job_changes``) and every listing it was
found on (cross-source deduplication made visible). Any posting ID of the opening works.

The description is shown as plain text (``description_text``): the crawled HTML is
untrusted and is never rendered.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from jobindex.api.deps import SessionDep
from jobindex.api.search import get_opening
from jobindex.api.ui import header_stats, templates
from jobindex.models import Job, JobChange, Source

MAX_CHANGES = 100
VALUE_CHARS = 160
#: Long fields: the history says that they changed, not the full old/new text.
LONG_FIELDS = frozenset({"description_html", "description_text"})


@dataclass(slots=True)
class Change:
    at: datetime
    field: str
    old: str | None
    new: str | None
    cause: str
    platform: str
    job_id: int


def _short(value: str | None) -> str | None:
    if value is None or len(value) <= VALUE_CHARS:
        return value
    return value[:VALUE_CHARS] + "…"


router = APIRouter(include_in_schema=False)


@router.get("/job/{job_id}", response_class=HTMLResponse)
async def job_page(
    request: Request,
    session: SessionDep,
    job_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
) -> HTMLResponse:
    opening = await get_opening(session, job_id)
    stats = await header_stats(session)
    if opening is None:
        return templates.TemplateResponse(
            request, "job.html", {"job": None, "stats": stats, "nav": None}, status_code=404
        )
    member_ids = [s.job_id for s in opening.sources]
    last_checked = await session.scalar(
        select(func.max(Job.last_checked_at)).where(Job.id.in_(member_ids))
    )
    changes = [
        Change(
            at=change.changed_at,
            field=change.field,
            old=None if change.field in LONG_FIELDS else _short(change.old_value),
            new=None if change.field in LONG_FIELDS else _short(change.new_value),
            cause=change.cause.value,
            platform=platform,
            job_id=change.job_id,
        )
        for change, platform in await session.execute(
            select(JobChange, Source.platform)
            .join(Job, Job.id == JobChange.job_id)
            .join(Source, Source.id == Job.source_id)
            .where(JobChange.job_id.in_(member_ids))
            .order_by(JobChange.changed_at.desc(), JobChange.id.desc())
            .limit(MAX_CHANGES)
        )
    ]
    return templates.TemplateResponse(
        request,
        "job.html",
        {
            "job": opening,
            "stats": stats,
            "nav": None,
            "last_checked": last_checked,
            "changes": changes,
            "max_changes": MAX_CHANGES,
        },
    )
