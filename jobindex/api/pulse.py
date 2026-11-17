"""Market pulse page at ``/pulse``: where the open jobs are, who hires, what pays.

Server-rendered, bar charts in plain CSS (widths in percent, no JavaScript). The
numbers come from a handful of aggregate queries over open postings and are cached for
``CACHE_SECONDS`` both in the process and by the CDN (``Cache-Control: s-maxage``), so
the database sees at most one round of them per 10 minutes per instance.

Counts are *openings* (postings deduplicated across sources, see ``jobindex.dedup``),
except "by platform" and salaries, which count *postings* (an opening listed on two
platforms counts for both). Companies are grouped by ``dedup.keys.company_key`` ("East
West Bank" == "EastWest Bank") and shown with their most common spelling.

The hosted demo stores at most ``MAX_JOBS_PER_SOURCE`` postings per source, so a big
employer's count stops there; the page says so, and marks companies with a capped
source "200+".
"""

import time
from collections import Counter
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
from jobindex.config import get_settings
from jobindex.dedup.keys import company_key
from jobindex.models import Job, JobStatus, RemoteType, Source
from jobindex.platform_names import platform_name

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
    capped: bool = False  # a source of it hit the per-source cap: the real count is higher


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
    cap: int = 0  # MAX_JOBS_PER_SOURCE of the index (0 = no cap)


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
        bar.key = bar.label
        bar.label = platform_name(bar.key)
        bar.href = f"/?platform={bar.key}"

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

    cap = get_settings().max_jobs_per_source
    companies = await _top_companies(session, cap, open_openings)

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
        cap=cap,
    )


async def _top_companies(session: AsyncSession, cap: int, open_openings: int) -> list[Bar]:
    """Top companies by distinct open openings across all their sources, spellings
    grouped by ``company_key``."""
    is_open = Job.status == JobStatus.OPEN
    has_company = (Job.company.is_not(None), Job.company != "")
    openings: dict[str, set[int]] = {}
    spellings: dict[str, Counter[str]] = {}
    for company, opening, n in await session.execute(
        select(Job.company, group_id(), func.count())
        .where(is_open, *has_company)
        .group_by(Job.company, group_id())
    ):
        key = company_key(company)
        if not key:
            continue
        openings.setdefault(key, set()).add(opening)
        spellings.setdefault(key, Counter())[company] += n
    capped: set[str] = set()
    if cap > 0:
        capped = {
            company_key(company)
            for (company,) in await session.execute(
                select(Job.company)
                .where(is_open, *has_company)
                .group_by(Job.company, Job.source_id)
                .having(func.count() >= cap)
            )
        }

    def display(key: str) -> str:  # most common spelling; ties: alphabetical
        return min(spellings[key].items(), key=lambda kv: (-kv[1], kv[0]))[0]

    top = sorted(openings, key=lambda k: (-len(openings[k]), display(k).casefold()))
    rows = [(display(k), len(openings[k])) for k in top[:TOP_COMPANIES]]
    companies = bars(rows, open_openings)
    for bar, key in zip(companies, top, strict=False):
        bar.key = key
        bar.capped = key in capped
        # the shortest spelling, which the search's company filter finds in the others
        shortest = min(spellings[key], key=lambda s: (len(s), s))
        bar.href = "/?" + urlencode({"company": shortest})
    return companies


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
