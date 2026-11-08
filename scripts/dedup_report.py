"""Show what deduplication merged and what it deliberately kept apart.

Prints (Markdown):
1. every opening listed on more than one source, with each listing;
2. *near misses*: open postings on different sources with the same company and title
   keys that were NOT merged, with the rule that kept them apart (different city,
   remote vs on-site, dates too far apart, or the merge would join two postings of one
   source). These are the false-merge checks: read them to confirm they really are
   different openings.

Usage:
    python scripts/dedup_report.py [--limit 30]
"""

import argparse
import asyncio
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from jobindex.config import get_settings
from jobindex.db import create_engine, create_session_factory
from jobindex.dedup.keys import company_key, locations_compatible, title_key
from jobindex.models import Job, JobStatus, Source


def _reason(a: Job, b: Job, window: timedelta) -> str:
    if not locations_compatible(a.remote_type, a.city, a.country, b.remote_type, b.city, b.country):
        return "locations differ"
    if a.posted_at and b.posted_at and abs(a.posted_at - b.posted_at) > window:
        return f"posted {abs(a.posted_at - b.posted_at).days} days apart"
    return "seen in non-overlapping periods, or the merge would join two postings of one source"


def _row(job: Job, platform: str) -> str:
    where = job.city or job.location_raw or job.remote_type.value
    posted = job.posted_at.date().isoformat() if job.posted_at else "—"
    cells = [str(job.id), platform, job.company or "", job.title, where, posted, job.source_url]
    return "| " + " | ".join(c or "" for c in cells) + " |"


HEADER = (
    "| Job | Platform | Company | Title | Location | Posted | Source URL |\n"
    "|---|---|---|---|---|---|---|"
)


async def main(limit: int) -> None:
    window = timedelta(days=get_settings().dedup_date_window_days)
    engine = create_engine()
    async with create_session_factory(engine)() as session:
        rows = (
            await session.execute(
                select(Job, Source.platform, Source.company_name)
                .join(Source, Source.id == Job.source_id)
                .where(Job.status == JobStatus.OPEN)
            )
        ).all()
    await engine.dispose()

    groups: dict[int, list[tuple[Job, str]]] = defaultdict(list)
    blocks: dict[tuple[str, str], list[tuple[Job, str]]] = defaultdict(list)
    for job, platform, company_name in rows:
        job.company = job.company or company_name
        groups[job.canonical_job_id or job.id].append((job, platform))
        key = (company_key(job.company), title_key(job.title))
        if all(key):
            blocks[key].append((job, platform))

    header = HEADER
    merged = [g for g in groups.values() if len({j.source_id for j, _ in g}) > 1]
    print(f"## Merged openings ({len(merged)} with listings on several sources)\n")
    for group in merged[:limit]:
        print(header)
        for job, platform in sorted(group, key=lambda g: g[0].id):
            print(_row(job, platform))
        print()

    print("## Near misses (same company + title on different sources, kept apart)\n")
    shown = 0
    for block in blocks.values():
        for i, (a, pa) in enumerate(block):
            for b, pb in block[i + 1 :]:
                same_group = (a.canonical_job_id or a.id) == (b.canonical_job_id or b.id)
                if a.source_id == b.source_id or same_group or shown >= limit:
                    continue
                shown += 1
                reason = _reason(a, b, window)
                print(header)
                print(_row(a, pa))
                print(_row(b, pb))
                print(f"\nKept apart: **{reason}**\n")
    if not shown:
        print("none\n")

    # Distinct roles that share a company + title on ONE source: never merged (one per
    # store/city, or several head-count requisitions).
    same_source: dict[tuple[str, str, int], list[tuple[Job, str]]] = defaultdict(list)
    for (company, title), block in blocks.items():
        for job, platform in block:
            same_source[company, title, job.source_id].append((job, platform))
    repeated = sorted((g for g in same_source.values() if len(g) > 1), key=len, reverse=True)
    kept = sum(len({j.canonical_job_id or j.id for j, _ in g}) == len(g) for g in repeated)
    print(
        f"## Same company + title on one source ({len(repeated)} titles, "
        f"{sum(map(len, repeated))} postings; kept separate: {kept} of {len(repeated)})\n"
    )
    for group in repeated[: min(limit, 5)]:
        print(header)
        for job, platform in sorted(group, key=lambda g: g[0].id)[:4]:
            print(_row(job, platform))
        print()

    # Same title at different employers: never merged (company key differs).
    by_title: dict[str, set[str]] = defaultdict(set)
    for company, title in blocks:
        by_title[title].add(company)
    shared = sorted(((t, c) for t, c in by_title.items() if len(c) > 1), key=lambda x: -len(x[1]))
    print(
        f"## Same title at different employers: {len(shared)} titles (kept separate); "
        "most common:\n"
    )
    for title, companies in shared[: min(limit, 10)]:
        print(f"* `{title}`: {len(companies)} employers")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=30)
    asyncio.run(main(parser.parse_args().limit))
