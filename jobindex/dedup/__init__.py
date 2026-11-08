"""Cross-source deduplication: fills ``jobs.canonical_job_id``.

The same opening is often listed on several sources (an ATS board and a job board that
re-lists it, or two ATS boards of one employer). ``run_dedup()`` groups such postings
(rules in ``jobindex.dedup.cluster``) and points every posting of a group at one
*canonical* posting; a posting with no duplicate points at itself. The API returns one
result per canonical posting together with the links of all its sources.

It runs after every crawl (CLI and scheduler) and can be run by hand with
``python -m jobindex.cli dedup``. Each run recomputes the groups of all open postings and
of postings closed within the date window, so it is idempotent and repairs itself after
connector or rule changes. Older closed postings keep their last assignment.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import literal_column, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ColumnClause

from jobindex.dedup.cluster import (
    DEFAULT_DATE_WINDOW,
    Posting,
    cluster,
    description_words,
    fuzzy_candidates,
)
from jobindex.models import Job, JobStatus, Source
from jobindex.models.base import utcnow

__all__ = ["DEFAULT_DATE_WINDOW", "DedupResult", "Posting", "cluster", "run_dedup"]

log = logging.getLogger(__name__)

#: pg_advisory_xact_lock key: one dedup run at a time across all processes.
_LOCK_KEY = 0x6A6F6264  # "jobd"


#: URLs in the description, extracted by Postgres (descriptions are not loaded).
_LINKED_URLS: ColumnClause[list[str] | None] = literal_column(
    "ARRAY(SELECT DISTINCT m[1] FROM regexp_matches(jobs.description_html, "
    "'https?://[^\\s\"''<>()]+', 'g') AS m LIMIT 50)"
)


def _chunks(ids: list[int], size: int) -> list[list[int]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


@dataclass(frozen=True, slots=True)
class DedupResult:
    postings: int  # postings considered
    openings: int  # distinct groups among them
    updated: int  # rows whose canonical_job_id changed


async def run_dedup(
    session: AsyncSession,
    *,
    window: timedelta = DEFAULT_DATE_WINDOW,
    now: datetime | None = None,
) -> DedupResult:
    """Recompute ``canonical_job_id`` and commit. Concurrent runs wait for each other."""
    now = now or utcnow()
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
    rows = await session.execute(
        select(
            Job.id,
            Job.source_id,
            Job.company,
            Source.company_name,
            Job.title,
            Job.city,
            Job.country,
            Job.remote_type,
            Job.posted_at,
            Job.first_seen_at,
            Job.last_seen_at,
            Job.status,
            Job.apply_url,
            Job.source_url,
            Job.canonical_job_id,
            _LINKED_URLS.label("linked_urls"),
        )
        .join(Source, Source.id == Job.source_id)
        .where(or_(Job.status == JobStatus.OPEN, Job.closed_at >= now - window))
    )
    postings: list[Posting] = []
    current: dict[int, int | None] = {}
    for row in rows:
        current[row.id] = row.canonical_job_id
        postings.append(
            Posting(
                id=row.id,
                source_id=row.source_id,
                company=row.company or row.company_name,
                title=row.title,
                city=row.city,
                country=row.country,
                remote_type=row.remote_type,
                posted_at=row.posted_at,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                status=row.status,
                apply_url=row.apply_url,
                source_url=row.source_url,
                linked_urls=tuple(
                    u.replace("&amp;", "&").rstrip(".,;") for u in row.linked_urls or ()
                ),
            )
        )

    candidates = fuzzy_candidates(postings)
    descriptions: dict[int, frozenset[str]] = {}
    for chunk in _chunks(sorted(candidates), 1000):
        for job_id, text_ in await session.execute(
            select(Job.id, Job.description_text).where(Job.id.in_(chunk))
        ):
            descriptions[job_id] = description_words(text_)
    canonical = cluster(postings, window, descriptions)
    changes = [
        {"id": job_id, "canonical_job_id": head}
        for job_id, head in canonical.items()
        if current[job_id] != head
    ]
    if changes:
        await session.execute(update(Job), changes)
    await session.commit()  # also releases the advisory lock

    result = DedupResult(len(postings), len(set(canonical.values())), len(changes))
    log.info(
        "dedup: %d postings -> %d openings (%d rows updated)",
        result.postings,
        result.openings,
        result.updated,
    )
    return result
