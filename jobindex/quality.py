"""Data-quality report: how often each job field is filled, per platform.

``python -m jobindex.cli fill-rates [--markdown]`` (``make fill-rates``). Counts **open**
postings only: closed tombstones of sitemap platforms (a 404 page) legitimately carry
nothing but a title.

Required fields (every posting must have them; a gap is a connector bug):
    title, company, description, location, source_url, apply_url
Optional fields (filled when the platform publishes them):
    salary (min or max), employment_type, remote (remote_type known), posted_at
Tracked by the crawler (never empty by schema): first_seen_at, last_checked_at.
"""

from dataclasses import dataclass

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from jobindex.models import Job, JobStatus, RemoteType, Source

REQUIRED = ("title", "company", "description", "location", "source_url", "apply_url")
OPTIONAL = ("salary", "employment_type", "remote", "posted_at")
TRACKED = ("first_seen_at", "last_checked_at")
FIELDS = REQUIRED + OPTIONAL + TRACKED


def _filled(
    text: InstrumentedAttribute[str] | InstrumentedAttribute[str | None],
) -> ColumnElement[bool]:
    return func.coalesce(func.length(func.trim(text)), 0) > 0


def _conditions() -> dict[str, ColumnElement[bool]]:
    return {
        "title": _filled(Job.title),
        "company": _filled(Job.company),
        "description": _filled(Job.description_text),
        "location": _filled(Job.location_raw)
        | Job.country.is_not(None)
        | (Job.remote_type == RemoteType.REMOTE),
        "source_url": _filled(Job.source_url),
        "apply_url": _filled(Job.apply_url),
        "salary": Job.salary_min.is_not(None) | Job.salary_max.is_not(None),
        "employment_type": Job.employment_type.is_not(None),
        "remote": Job.remote_type != RemoteType.UNKNOWN,
        "posted_at": Job.posted_at.is_not(None),
        "first_seen_at": Job.first_seen_at.is_not(None),
        "last_checked_at": Job.last_checked_at.is_not(None),
    }


@dataclass(frozen=True, slots=True)
class PlatformFill:
    platform: str
    open_jobs: int
    filled: dict[str, int]

    def rate(self, field: str) -> float:
        return self.filled[field] / self.open_jobs if self.open_jobs else 0.0

    def gaps(self) -> list[str]:
        """Required fields missing on at least one open posting."""
        return [f for f in REQUIRED if self.filled[f] < self.open_jobs]


async def fill_rates(session: AsyncSession) -> list[PlatformFill]:
    conditions = _conditions()
    columns = [func.sum(cond.cast(Integer)).label(name) for name, cond in conditions.items()]
    rows = await session.execute(
        select(Source.platform, func.count(), *columns)
        .join(Source, Source.id == Job.source_id)
        .where(Job.status == JobStatus.OPEN)
        .group_by(Source.platform)
        .order_by(Source.platform)
    )
    return [
        PlatformFill(
            platform=row[0],
            open_jobs=row[1],
            filled={name: int(value or 0) for name, value in zip(conditions, row[2:], strict=True)},
        )
        for row in rows
    ]


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%" if value < 1 else "100%"


def render_markdown(rows: list[PlatformFill]) -> str:
    header = ["Platform", "Open jobs", *FIELDS]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        cells = [row.platform, str(row.open_jobs)]
        for field in FIELDS:
            cell = _pct(row.rate(field))
            cells.append(
                f"**{cell}**" if field in REQUIRED and row.filled[field] < row.open_jobs else cell
            )
        lines.append("| " + " | ".join(cells) + " |")
    total = sum(r.open_jobs for r in rows)
    cells = ["**all**", str(total)]
    for field in FIELDS:
        filled = sum(r.filled[field] for r in rows)
        cells.append(_pct(filled / total) if total else "—")
    lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render_text(rows: list[PlatformFill]) -> str:
    width = max([len(r.platform) for r in rows] + [8])
    short = [f[:8] for f in FIELDS]
    out = [f"{'platform':<{width}} {'open':>6} " + " ".join(f"{s:>8}" for s in short)]
    for row in rows:
        rates = " ".join(f"{_pct(row.rate(f)):>8}" for f in FIELDS)
        out.append(f"{row.platform:<{width}} {row.open_jobs:>6} {rates}")
    gaps = [(r.platform, r.gaps()) for r in rows if r.gaps()]
    out.append("")
    out.append(
        "required-field gaps: "
        + ("; ".join(f"{p}: {', '.join(g)}" for p, g in gaps) if gaps else "none")
    )
    return "\n".join(out) + "\n"
