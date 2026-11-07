"""Cross-source deduplication (jobindex/dedup). Cases are modelled on real listings: the
same opening on an employer's ATS board and on a job board that re-lists it."""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.dedup import Posting, cluster, run_dedup
from jobindex.dedup.keys import company_key, title_key, url_key
from jobindex.models import Job, JobStatus, RemoteType, Source

T0 = datetime(2026, 10, 1, tzinfo=UTC)
_ids = count(1)


def posting(source_id: int, **kw: Any) -> Posting:
    defaults: dict[str, Any] = {
        "id": next(_ids),
        "source_id": source_id,
        "company": "Acme",
        "title": "Backend Engineer",
        "city": "Berlin",
        "country": "DE",
        "remote_type": RemoteType.ONSITE,
        "posted_at": T0,
        "first_seen_at": T0,
        "last_seen_at": T0 + timedelta(days=10),
        "status": JobStatus.OPEN,
    }
    return Posting(**(defaults | kw))


def groups(postings: list[Posting]) -> set[frozenset[int]]:
    by_head: dict[int, set[int]] = {}
    for job_id, head in cluster(postings).items():
        by_head.setdefault(head, set()).add(job_id)
    return {frozenset(g) for g in by_head.values()}


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Acme GmbH", "ACME"),
        ("The Acme Company, Inc.", "acme"),
        ("HelloFresh SE", "Hello Fresh"),
        ("Société Générale", "Societe Generale"),
        ("Procter & Gamble", "Procter and Gamble"),
    ],
)
def test_company_key_matches(a: str, b: str) -> None:
    assert company_key(a) == company_key(b) != ""


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Sr. Backend Engineer (m/w/d)", "senior backend engineer"),
        ("Senior Backend Engineer (f/m/x) - Remote", "senior backend engineer"),
        ("Werkstudent Marketing (all genders)", "werkstudent marketing"),
        ("Data Analyst (Hybrid)", "data analyst"),
        ("Jr Dev, Front-End", "junior developer front end"),
        ("Chargé d'affaires H/F", "charge d affaires"),
    ],
)
def test_title_key(raw: str, key: str) -> None:
    assert title_key(raw) == key


def test_url_key_ignores_tracking_but_keeps_identifiers() -> None:
    assert url_key("https://www.acme.com/jobs/123/?utm_source=niceboard&ref=x#apply") == (
        "acme.com/jobs/123"
    )
    gh = "https://boards.greenhouse.io/acme/jobs?gh_jid=42"
    assert url_key(gh + "&gh_src=abc") == url_key(gh) != url_key(gh.replace("42", "43"))


def test_same_opening_on_ats_and_job_board_is_merged() -> None:
    ats = posting(1, company="Acme GmbH", title="Sr. Backend Engineer (m/w/d)")
    board = posting(
        2, company="ACME", title="Senior Backend Engineer", posted_at=T0 + timedelta(days=3)
    )
    assert groups([ats, board]) == {frozenset({ats.id, board.id})}


def test_same_title_different_city_stays_separate() -> None:
    berlin = posting(1)
    munich = posting(2, city="München")
    assert len(groups([berlin, munich])) == 2


def test_postings_of_one_source_are_never_merged() -> None:
    # Two requisitions for the same role on one board, plus a job board copy of one of them.
    req1, req2, copy = posting(1), posting(1), posting(2)
    result = groups([req1, req2, copy])
    assert result == {frozenset({req1.id, copy.id}), frozenset({req2.id})}


def test_remote_rules() -> None:
    anywhere = posting(1, remote_type=RemoteType.REMOTE, city=None, country=None)
    us = posting(2, remote_type=RemoteType.REMOTE, city=None, country="US")
    ca = posting(3, remote_type=RemoteType.REMOTE, city=None, country="CA")
    # "Remote" matches "Remote, US" but must not chain on to "Remote, CA".
    result = groups([anywhere, us, ca])
    assert frozenset({anywhere.id, us.id}) in result
    assert frozenset({ca.id}) in result

    onsite = posting(4, city="Berlin")
    remote_berlin = posting(5, remote_type=RemoteType.REMOTE)
    assert len(groups([onsite, remote_berlin])) == 2


def test_repost_months_later_is_a_new_opening() -> None:
    old = posting(1, posted_at=T0, status=JobStatus.CLOSED)
    new = posting(2, posted_at=T0 + timedelta(days=90))
    assert len(groups([old, new])) == 2


def test_without_posted_dates_lifetimes_must_overlap() -> None:
    seen = posting(1, posted_at=None, first_seen_at=T0, last_seen_at=T0 + timedelta(days=5))
    overlapping = posting(2, posted_at=None, first_seen_at=T0 + timedelta(days=2))
    much_later = posting(
        3,
        posted_at=None,
        first_seen_at=T0 + timedelta(days=120),
        last_seen_at=T0 + timedelta(days=125),
    )
    result = groups([seen, overlapping, much_later])
    assert frozenset({seen.id, overlapping.id}) in result
    assert frozenset({much_later.id}) in result


def test_shared_apply_url_is_a_strong_signal() -> None:
    # The job board retitled the posting and lost the city, but links to the ATS posting.
    ats = posting(
        1, title="Backend Engineer, Payments", apply_url="https://jobs.lever.co/acme/1b2c3d/apply"
    )
    board = posting(
        2,
        title="Software Engineer (Backend)",
        city=None,
        country=None,
        apply_url="https://jobs.lever.co/acme/1b2c3d/apply?utm_source=jobboard",
    )
    assert groups([ats, board]) == {frozenset({ats.id, board.id})}


def test_generic_apply_url_shared_within_a_source_proves_nothing() -> None:
    careers = "https://acme.com/careers"
    a = posting(1, title="Accountant", apply_url=careers)
    b = posting(1, title="Designer", apply_url=careers)
    c = posting(2, title="Recruiter", apply_url=careers)
    assert len(groups([a, b, c])) == 3


def test_canonical_prefers_open_then_first_seen() -> None:
    closed = posting(1, status=JobStatus.CLOSED, first_seen_at=T0 - timedelta(days=1))
    later = posting(2, first_seen_at=T0 + timedelta(days=1))
    earlier = posting(3, first_seen_at=T0)
    heads = set(cluster([closed, later, earlier]).values())
    assert heads == {earlier.id}


async def test_run_dedup_fills_canonical_job_id(session: AsyncSession) -> None:
    ats = Source(
        platform="greenhouse",
        board_identifier="acme",
        base_url="https://x/acme",
        company_name="Acme GmbH",
    )
    board = Source(
        platform="niceboard", board_identifier="jobs.example", base_url="https://jobs.example"
    )
    session.add_all([ats, board])
    await session.flush()

    def job(source: Source, ext: str, title: str, city: str, company: str | None) -> Job:
        return Job(
            source_id=source.id,
            external_id=ext,
            title=title,
            company=company,
            city=city,
            country="DE",
            posted_at=T0,
            content_hash=ext,
        )

    session.add_all(
        [
            job(ats, "1", "Backend Engineer (m/w/d)", "Berlin", None),  # company from source
            job(ats, "2", "Backend Engineer (m/w/d)", "Hamburg", None),
            job(board, "3", "Backend Engineer", "Berlin", "ACME"),
        ]
    )
    await session.commit()

    result = await run_dedup(session, now=T0 + timedelta(days=1))
    assert (result.postings, result.openings, result.updated) == (3, 2, 3)
    result_rows = await session.execute(select(Job.external_id, Job.canonical_job_id))
    rows = {external_id: head for external_id, head in result_rows}
    assert rows == {"1": 1, "2": 2, "3": 1}

    again = await run_dedup(session, now=T0 + timedelta(days=1))
    assert again.updated == 0  # idempotent
