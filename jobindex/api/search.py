"""Job search shared by the JSON API and the HTML page.

Results are *openings*: postings are grouped by ``coalesce(canonical_job_id, id)``
(see ``jobindex.dedup``). Filters apply to every posting of a group, so an opening
matches when any of its listings matches (e.g. only one source publishes the salary).
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api.schemas import (
    JobDetailOut,
    JobOut,
    JobSearchOut,
    JobSourceOut,
    StatusFilter,
)
from jobindex.models import Job, JobStatus, RemoteType, Source
from jobindex.normalize import normalize_country

MAX_PAGE_SIZE = 100


@dataclass(slots=True)
class SearchParams:
    q: str | None = None
    company: str | None = None
    location: str | None = None
    remote_type: RemoteType | None = None
    employment_type: str | None = None
    salary_min: Decimal | None = None
    salary_currency: str | None = None
    posted_since: date | None = None
    status: StatusFilter = StatusFilter.OPEN
    platform: str | None = None
    source_id: int | None = None
    page: int = 1
    size: int = 20


def group_id() -> ColumnElement[int]:
    return func.coalesce(Job.canonical_job_id, Job.id)


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _location_filter(location: str) -> ColumnElement[bool]:
    """``"Berlin"``, ``"CA"``, ``"Germany"`` or ``"Austin, TX, US"``: every comma-separated
    part must match the city, region or country (country names become ISO codes)."""
    conditions = []
    for part in (p.strip() for p in location.split(",")):
        if not part:
            continue
        options = [func.lower(Job.city) == part.lower(), func.lower(Job.region) == part.lower()]
        country = normalize_country(part)
        if country:
            options.append(Job.country == country)
        conditions.append(or_(*options))
    return and_(*conditions)


def _filters(p: SearchParams) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = []
    if p.status is not StatusFilter.ANY:
        where.append(Job.status == JobStatus(p.status.value))
    if p.q:
        where.append(Job.search_vector.op("@@")(func.websearch_to_tsquery("english", p.q)))
    if p.company:
        where.append(Job.company.ilike(_like(p.company.strip())))
    if p.location and p.location.strip(", "):
        where.append(_location_filter(p.location))
    if p.remote_type is not None:
        where.append(Job.remote_type == p.remote_type)
    if p.employment_type:
        where.append(Job.employment_type == p.employment_type)
    if p.salary_min is not None:
        where.append(func.coalesce(Job.salary_max, Job.salary_min) >= p.salary_min)
    if p.salary_currency:
        where.append(Job.salary_currency == p.salary_currency.upper())
    if p.posted_since is not None:
        since = datetime.combine(p.posted_since, time.min, tzinfo=UTC)
        where.append(func.coalesce(Job.posted_at, Job.first_seen_at) >= since)
    if p.platform:
        where.append(Source.platform == p.platform)
    if p.source_id is not None:
        where.append(Job.source_id == p.source_id)
    return where


async def search(session: AsyncSession, p: SearchParams) -> JobSearchOut:
    gid = group_id().label("gid")
    stmt: Select[Any] = (
        select(gid).join(Source, Source.id == Job.source_id).where(*_filters(p)).group_by(gid)
    )
    if p.q:
        rank = func.ts_rank(Job.search_vector, func.websearch_to_tsquery("english", p.q))
        order = [func.max(rank).desc(), gid.desc()]
    else:
        newest = func.max(func.coalesce(Job.posted_at, Job.first_seen_at))
        order = [newest.desc(), gid.desc()]

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page_ids = list(
        await session.scalars(stmt.order_by(*order).limit(p.size).offset((p.page - 1) * p.size))
    )
    openings = await load_openings(session, page_ids)
    return JobSearchOut(
        total=total,
        page=p.page,
        size=p.size,
        pages=math.ceil(total / p.size) if total else 0,
        items=[JobOut.model_validate(openings[i]) for i in page_ids if i in openings],
    )


async def get_opening(session: AsyncSession, job_id: int) -> JobDetailOut | None:
    """The opening a posting belongs to (any of its postings' IDs works)."""
    head = await session.scalar(select(group_id()).where(Job.id == job_id))
    if head is None:
        return None
    openings = await load_openings(session, [head], detail=True)
    return JobDetailOut.model_validate(openings[head]) if head in openings else None


_MERGED_FIELDS = (
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "posted_at",
)
_SALARY_FIELDS = ("salary_min", "salary_max", "salary_currency", "salary_period")


async def load_openings(
    session: AsyncSession, heads: list[int], *, detail: bool = False
) -> dict[int, dict[str, Any]]:
    """Build opening dicts (validated by ``JobOut``) for the given canonical IDs."""
    if not heads:
        return {}
    rows = await session.execute(
        select(Job, Source.platform, Source.board_identifier, group_id().label("gid"))
        .join(Source, Source.id == Job.source_id)
        .where(group_id().in_(heads))
        .order_by(Job.first_seen_at, Job.id)
    )
    members: dict[int, list[tuple[Job, str, str]]] = {}
    for job, platform, board, gid in rows:
        members.setdefault(gid, []).append((job, platform, board))

    result: dict[int, dict[str, Any]] = {}
    for head, group in members.items():
        jobs = [j for j, _, _ in group]
        canonical = next((j for j in jobs if j.id == head), jobs[0])
        out = {
            "id": head,
            "title": canonical.title,
            "company": canonical.company,
            "location_raw": canonical.location_raw,
            "city": canonical.city,
            "region": canonical.region,
            "country": canonical.country,
            "remote_type": canonical.remote_type,
            "status": (
                JobStatus.OPEN
                if any(j.status is JobStatus.OPEN for j in jobs)
                else JobStatus.CLOSED
            ),
            "first_seen_at": min(j.first_seen_at for j in jobs),
            "last_seen_at": max(j.last_seen_at for j in jobs),
            "closed_at": None
            if any(j.status is JobStatus.OPEN for j in jobs)
            else max((j.closed_at for j in jobs if j.closed_at), default=None),
            "sources": [
                JobSourceOut(
                    job_id=j.id,
                    source_id=j.source_id,
                    platform=platform,
                    board_identifier=board,
                    status=j.status,
                    source_url=j.source_url,
                    apply_url=j.apply_url,
                    first_seen_at=j.first_seen_at,
                    last_seen_at=j.last_seen_at,
                )
                for j, platform, board in group
            ],
        }
        # Fields the canonical posting lacks are taken from another listing; the salary
        # fields always come together from one listing.
        salary_from = next(
            (j for j in [canonical, *jobs] if j.salary_min is not None or j.salary_max),
            canonical,
        )
        for name in _MERGED_FIELDS:
            if name in _SALARY_FIELDS:
                out[name] = getattr(salary_from, name)
            else:
                out[name] = next(
                    (getattr(j, name) for j in [canonical, *jobs] if getattr(j, name)), None
                )
        if detail:
            described = next((j for j in [canonical, *jobs] if j.description_text), canonical)
            out["description_html"] = described.description_html
            out["description_text"] = described.description_text
        result[head] = out
    return result
