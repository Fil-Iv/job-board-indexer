"""Split active sources into shards for parallel crawl jobs (GitHub Actions matrix,
.github/workflows/crawl.yml) and summarise what a crawl did.

Planning rules:

* All sources of one **platform** go to the same shard. Many platforms share one API
  host (``boards-api.greenhouse.io``, ``api.lever.co``, ...); ``HttpClient`` enforces
  the per-host rate limit only within one process, so splitting a platform across
  parallel jobs would multiply the request rate against that host.
* Platforms are spread over the shards by estimated duration (longest first, onto the
  least loaded shard). A source's estimate is the duration of its latest finished run
  (a run cut off by the deadline counts with the time it got), or
  ``DEFAULT_ESTIMATE_SECONDS`` if it was never crawled.
* Within a shard, sources are ordered longest first, so slow boards start early.
"""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.models import FetchRun, FetchStatus, Job, JobStatus, Source

DEFAULT_ESTIMATE_SECONDS = 120.0


@dataclass(slots=True)
class Shard:
    index: int  # 1-based
    source_ids: list[int] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    estimated_seconds: float = 0.0


async def source_estimates(session: AsyncSession) -> list[tuple[int, str, float]]:
    """``(source id, platform, estimated seconds)`` of every active source."""
    latest = (
        select(
            FetchRun.source_id,
            func.extract("epoch", FetchRun.finished_at - FetchRun.started_at).label("seconds"),
            func.row_number()
            .over(partition_by=FetchRun.source_id, order_by=FetchRun.started_at.desc())
            .label("n"),
        )
        .where(FetchRun.finished_at.is_not(None))
        .subquery()
    )
    rows = await session.execute(
        select(Source.id, Source.platform, latest.c.seconds)
        .outerjoin(latest, (latest.c.source_id == Source.id) & (latest.c.n == 1))
        .where(Source.active.is_(True))
        .order_by(Source.id)
    )
    return [
        (sid, platform, float(seconds) if seconds is not None else DEFAULT_ESTIMATE_SECONDS)
        for sid, platform, seconds in rows
    ]


def plan_shards(estimates: list[tuple[int, str, float]], shards: int) -> list[Shard]:
    """Longest-processing-time-first bin packing of platforms onto ``shards`` shards."""
    if shards < 1:
        raise ValueError("shards must be >= 1")
    by_platform: dict[str, list[tuple[int, float]]] = {}
    for sid, platform, seconds in estimates:
        by_platform.setdefault(platform, []).append((sid, seconds))
    result = [Shard(i + 1) for i in range(shards)]
    platforms = sorted(by_platform.items(), key=lambda item: (-sum(s for _, s in item[1]), item[0]))
    for platform, sources in platforms:
        target = min(result, key=lambda shard: (shard.estimated_seconds, shard.index))
        target.platforms.append(platform)
        target.estimated_seconds += sum(seconds for _, seconds in sources)
    for shard in result:
        members = [
            (sid, seconds) for platform in shard.platforms for sid, seconds in by_platform[platform]
        ]
        shard.source_ids = [sid for sid, _ in sorted(members, key=lambda m: (-m[1], m[0]))]
        shard.platforms.sort()
    return result


@dataclass(frozen=True, slots=True)
class PlatformRuns:
    platform: str
    succeeded: int
    failed: int
    jobs_found: int
    jobs_new: int
    jobs_updated: int
    jobs_closed: int


@dataclass(frozen=True, slots=True)
class CrawlSummary:
    since: datetime
    platforms: list[PlatformRuns]
    failures: list[tuple[str, str, str | None]]  # platform, board, error
    open_jobs: int
    sources: int


async def crawl_summary(session: AsyncSession, since: datetime) -> CrawlSummary:
    """Fetch runs started since ``since``, per platform, plus the index totals."""
    ok = FetchRun.status == FetchStatus.SUCCESS
    rows = await session.execute(
        select(
            Source.platform,
            func.count().filter(ok),
            func.count().filter(FetchRun.status == FetchStatus.FAILED),
            func.coalesce(func.sum(FetchRun.jobs_found).filter(ok), 0),
            func.coalesce(func.sum(FetchRun.jobs_new).filter(ok), 0),
            func.coalesce(func.sum(FetchRun.jobs_updated).filter(ok), 0),
            func.coalesce(func.sum(FetchRun.jobs_closed).filter(ok), 0),
        )
        .join(Source, Source.id == FetchRun.source_id)
        .where(FetchRun.started_at >= since)
        .group_by(Source.platform)
        .order_by(Source.platform)
    )
    failures = await session.execute(
        select(Source.platform, Source.board_identifier, FetchRun.error)
        .join(Source, Source.id == FetchRun.source_id)
        .where(FetchRun.started_at >= since, FetchRun.status == FetchStatus.FAILED)
        .order_by(Source.platform, Source.board_identifier)
    )
    open_jobs = await session.scalar(
        select(func.count()).select_from(Job).where(Job.status == JobStatus.OPEN)
    )
    sources = await session.scalar(
        select(func.count()).select_from(Source).where(Source.active.is_(True))
    )
    return CrawlSummary(
        since,
        [PlatformRuns(row[0], *(int(v) for v in row[1:])) for row in rows],
        [(p, b, e) for p, b, e in failures],
        int(open_jobs or 0),
        int(sources or 0),
    )


def render_summary(summary: CrawlSummary) -> str:
    """Markdown for the GitHub Actions job summary."""
    runs = summary.platforms
    ok = sum(p.succeeded for p in runs)
    failed = sum(p.failed for p in runs)
    lines = [
        f"### Crawl since {summary.since:%Y-%m-%d %H:%M} UTC",
        "",
        f"**{ok} succeeded, {failed} failed** · {summary.sources} active sources · "
        f"**{summary.open_jobs:,} open postings** in the index",
        "",
        "| Platform | OK | Failed | Found | New | Updated | Closed |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {p.platform} | {p.succeeded} | {p.failed} | {p.jobs_found:,} | {p.jobs_new:,} "
        f"| {p.jobs_updated:,} | {p.jobs_closed:,} |"
        for p in runs
    ]
    lines.append(
        f"| **total** | {ok} | {failed} | {sum(p.jobs_found for p in runs):,} "
        f"| {sum(p.jobs_new for p in runs):,} | {sum(p.jobs_updated for p in runs):,} "
        f"| {sum(p.jobs_closed for p in runs):,} |"
    )
    if summary.failures:
        lines += ["", "<details><summary>Failures</summary>", ""]
        for platform, board, error in summary.failures:
            first_line = (error or "").splitlines()[0][:300] if error else ""
            lines.append(f"- `{platform}` `{board}`: {first_line.replace('|', '/')}")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"
