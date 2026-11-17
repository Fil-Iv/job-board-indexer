"""Market pulse page at ``/pulse``: where the open jobs are, who hires, what pays.

Server-rendered, bar charts in plain CSS (widths in percent, no JavaScript). The
numbers come from a handful of aggregate queries over open postings and are cached for
``CACHE_SECONDS`` both in the process and by the CDN (``Cache-Control: s-maxage``), so
the database sees at most one round of them per 10 minutes per instance.

Counts are *openings* (postings deduplicated across sources, see ``jobindex.dedup``),
except "by platform" and salaries, which count *postings* (an opening listed on two
platforms counts for both).
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import pycountry
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.api.deps import SessionDep
from jobindex.api.search import group_id
from jobindex.api.ui import HeaderStats, header_stats, templates
from jobindex.models import Job, JobStatus, RemoteType, Source

CACHE_SECONDS = 600
TOP_COUNTRIES = 20
TOP_COMPANIES = 20
TOP_SALARY_GROUPS = 8
MIN_SALARY_SAMPLE = 5


@lru_cache(maxsize=512)
def country_name(code: str | None) -> str:
    """Display name for an ISO 3166-1 alpha-2 code (the code itself if unknown)."""
    if not code:
        return "Not stated"
    country = pycountry.countries.get(alpha_2=code.upper())
    if country is None:
        return code
    return str(getattr(country, "common_name", None) or country.name)


@dataclass(slots=True)
class Bar:
    label: str
    count: int
    pct: float  # of the largest bar in the chart (bar width)
    share: float  # of the chart total
    href: str | None = None
    key: str | None = None


@dataclass(slots=True)
class SalaryBand:
    currency: str
    period: str
    count: int
    p25: Decimal
    median: Decimal
    p75: Decimal
    p90: Decimal

    def pos(self, value: Decimal) -> float:
        """Position on a 0..p90 track, in percent."""
        return float(min(value / self.p90, Decimal(1)) * 100) if self.p90 > 0 else 0.0


@dataclass(slots=True)
class Pulse:
    generated_at: datetime
    open_openings: int
    new_24h: int
    new_7d: int
    countries: list[Bar] = field(default_factory=list)
    platforms: list[Bar] = field(default_factory=list)
    workplace: list[Bar] = field(default_factory=list)
    companies: list[Bar] = field(default_factory=list)
    salaries: list[SalaryBand] = field(default_factory=list)


def bars(rows: list[tuple[str, int]], total: int | None = None, **extra: Any) -> list[Bar]:
    top = max((n for _, n in rows), default=0)
    total = total if total is not None else sum(n for _, n in rows)
    return [
        Bar(label=label, count=n, pct=100 * n / top if top else 0, share=n / total if total else 0)
        for label, n in rows
    ]


def _openings() -> ColumnElement[int]:
    return func.count(func.distinct(group_id()))


async def compute_pulse(session: AsyncSession, now: datetime | None = None) -> Pulse:
    now = now or datetime.now(UTC)
    is_open = Job.status == JobStatus.OPEN
    posted = func.coalesce(Job.posted_at, Job.first_seen_at)

    totals = (
        await session.execute(
            select(
                _openings(),
                func.count(func.distinct(group_id())).filter(posted >= now - timedelta(hours=24)),
                func.count(func.distinct(group_id())).filter(posted >= now - timedelta(days=7)),
            ).where(is_open)
        )
    ).one()
    open_openings = totals[0] or 0

    by_country = list(
        await session.execute(
            select(Job.country, _openings().label("n"))
            .where(is_open)
            .group_by(Job.country)
            .order_by(_openings().desc(), Job.country)
        )
    )
    known = [(c, n) for c, n in by_country if c]
    top_countries = known[:TOP_COUNTRIES]
    rest = sum(n for _, n in known[TOP_COUNTRIES:])
    unknown = sum(n for c, n in by_country if not c)
    country_rows = [(country_name(c), n) for c, n in top_countries]
    countries = bars(country_rows, open_openings)
    for bar, (code, _) in zip(countries, top_countries, strict=True):
        bar.key = code
        bar.href = f"/browse/{code}"
    if rest:
        countries.append(Bar("Other countries", rest, 0, rest / open_openings, href="/browse"))
    if unknown:
        countries.append(Bar("Not stated", unknown, 0, unknown / open_openings))
    for bar in countries[len(top_countries) :]:
        bar.pct = 100 * bar.count / countries[0].count if countries[0].count else 0

    platform_rows = [
        (p, n)
        for p, n in await session.execute(
            select(Source.platform, func.count())
            .select_from(Job)
            .join(Source, Source.id == Job.source_id)
            .where(is_open)
            .group_by(Source.platform)
            .order_by(func.count().desc(), Source.platform)
        )
    ]
    platforms = bars(platform_rows)
    for bar in platforms:
        bar.href = f"/?platform={bar.label}"

    by_type: dict[RemoteType, int] = {
        r: n
        for r, n in await session.execute(
            select(Job.remote_type, _openings()).where(is_open).group_by(Job.remote_type)
        )
    }
    order = [RemoteType.REMOTE, RemoteType.HYBRID, RemoteType.ONSITE, RemoteType.UNKNOWN]
    labels = {RemoteType.UNKNOWN: "Not stated"}
    workplace = bars(
        [(labels.get(r, r.value.capitalize()), by_type.get(r, 0)) for r in order],
        sum(by_type.values()),
    )
    for bar, r in zip(workplace, order, strict=True):
        bar.key = r.value
        if r is not RemoteType.UNKNOWN:
            bar.href = f"/?remote_type={r.value}"

    company_rows = [
        (c, n)
        for c, n in await session.execute(
            select(Job.company, _openings())
            .where(is_open, Job.company.is_not(None), Job.company != "")
            .group_by(Job.company)
            .order_by(_openings().desc(), Job.company)
            .limit(TOP_COMPANIES)
        )
    ]
    companies = bars(company_rows, open_openings)
    for bar in companies:
        bar.href = "/?" + urlencode({"company": bar.label})

    mid = func.coalesce((Job.salary_min + Job.salary_max) / 2, Job.salary_max, Job.salary_min)
    pct = [func.percentile_cont(p).within_group(mid) for p in (0.25, 0.5, 0.75, 0.9)]
    salaries = [
        SalaryBand(cur, period, n, Decimal(p25), Decimal(med), Decimal(p75), Decimal(p90))
        for cur, period, n, p25, med, p75, p90 in await session.execute(
            select(Job.salary_currency, Job.salary_period, func.count(), *pct)
            .where(
                is_open,
                Job.salary_currency.is_not(None),
                Job.salary_period.is_not(None),
                mid > 0,
            )
            .group_by(Job.salary_currency, Job.salary_period)
            .having(func.count() >= MIN_SALARY_SAMPLE)
            .order_by(func.count().desc())
            .limit(TOP_SALARY_GROUPS)
        )
    ]

    return Pulse(
        generated_at=now,
        open_openings=open_openings,
        new_24h=totals[1] or 0,
        new_7d=totals[2] or 0,
        countries=countries,
        platforms=platforms,
        workplace=workplace,
        companies=companies,
        salaries=salaries,
    )


_cache: dict[str, tuple[float, Pulse, HeaderStats]] = {}


async def cached_pulse(session: AsyncSession) -> tuple[Pulse, HeaderStats]:
    hit = _cache.get("pulse")
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]
    pulse, stats = await compute_pulse(session), await header_stats(session)
    _cache["pulse"] = (time.monotonic(), pulse, stats)
    return pulse, stats


router = APIRouter(include_in_schema=False)


@router.get("/pulse", response_class=HTMLResponse)
async def pulse_page(request: Request, session: SessionDep) -> HTMLResponse:
    pulse, stats = await cached_pulse(session)
    response = templates.TemplateResponse(
        request, "pulse.html", {"pulse": pulse, "stats": stats, "nav": "pulse"}
    )
    response.headers["Cache-Control"] = (
        f"public, max-age=0, s-maxage={CACHE_SECONDS}, stale-while-revalidate={CACHE_SECONDS}"
    )
    return response
