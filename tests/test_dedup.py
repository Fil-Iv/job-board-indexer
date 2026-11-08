"""Cross-source deduplication (jobindex/dedup). Cases are modelled on real listings: the
same opening on an employer's ATS board and on a job board that re-lists it."""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.dedup import Posting, cluster, run_dedup
from jobindex.dedup.cluster import description_words, overlap
from jobindex.dedup.keys import company_key, posting_keys, title_key, url_key
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


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (  # Workday: /details/ link (job boards) vs /job/{location}/ (the API's URL)
            "https://umd.wd1.myworkdayjobs.com/en-US/UMCES/details/Assistant-Professor_JR105179",
            "https://umd.wd1.myworkdayjobs.com/UMCES/job/UMCES-IMET/Assistant-Professor_JR105179",
        ),
        (
            "https://wd3.myworkdaysite.com/recruiting/pwc/Global_Careers/job/NY/Analyst_123",
            "https://wd3.myworkdaysite.com/en-US/recruiting/pwc/Global_Careers/details/A_123",
        ),
        (
            "https://jobs.lever.co/acme/0b2b2d5e-1b44-4c1a-9a43-8e2a9f2b3c4d",
            "https://jobs.lever.co/acme/0b2b2d5e-1b44-4c1a-9a43-8e2a9f2b3c4d/apply",
        ),
        (
            "https://boards.greenhouse.io/gitlab/jobs/123?gh_src=x",
            "https://job-boards.greenhouse.io/gitlab/jobs/123",
        ),
        (
            "https://ssp.applytojob.com/apply/53cgB6lfr4/Biochemistry-Director?referrer=x",
            "https://ssp.applytojob.com/apply/53cgB6lfr4",
        ),
        (
            "https://faculty-acme.icims.com/jobs/39228/doctoral-research-faculty/job?mode=view",
            "https://faculty-acme.icims.com/jobs/39228/job",
        ),
    ],
)
def test_posting_keys_match_url_variants_of_one_posting(a: str, b: str) -> None:
    assert posting_keys(a) & posting_keys(b)


def test_posting_keys_keep_different_postings_apart() -> None:
    one = "https://umd.wd1.myworkdayjobs.com/UMCES/job/X/Assistant-Professor_JR105179"
    other = "https://umd.wd1.myworkdayjobs.com/UMCES/job/X/Assistant-Professor_JR105195"
    other_site = "https://umd.wd1.myworkdayjobs.com/UMCP/job/X/Assistant-Professor_JR105179"
    assert not posting_keys(one) & posting_keys(other)
    assert not posting_keys(one) & posting_keys(other_site)


def test_description_link_to_another_sources_posting_merges() -> None:
    """Real case (2026-11-08): Science Careers re-lists a UMCES Workday requisition under a
    longer title, a different city and company spelling, and links to it in the text."""
    ats = posting(
        1,
        company=None,
        title="Assistant Professor-Microbiome and Symbiosis",
        city="UMCES Institute of Marine and Environmental Technology",
        source_url="https://umd.wd1.myworkdayjobs.com/UMCES/job/UMCES-IMET/"
        "Assistant-Professor-Microbiome-and-Symbiosis_JR105179",
    )
    board = posting(
        2,
        company="University of MD Center For Environmental Science",
        title="Assistant Professor-Microbiome and Symbiosis Studies",
        city="Baltimore",
        source_url="https://jobs.sciencecareers.org/job/680993/x/",
        linked_urls=(
            "https://umd.wd1.myworkdayjobs.com/en-US/UMCES/details/"
            "Assistant-Professor-Microbiome-and-Symbiosis_JR105179",
            "https://www.umces.edu/",
        ),
    )
    assert groups([ats, board]) == {frozenset({ats.id, board.id})}


def test_description_linking_several_postings_of_a_source_proves_nothing() -> None:
    a = posting(1, title="Chemist", source_url="https://acme.applytojob.com/apply/AAAAAA11")
    b = posting(1, title="Physicist", source_url="https://acme.applytojob.com/apply/BBBBBB22")
    roundup = posting(
        2,
        title="Summer teaching opportunities",
        linked_urls=(
            "https://acme.applytojob.com/apply/AAAAAA11/Chemist",
            "https://acme.applytojob.com/apply/BBBBBB22/Physicist",
        ),
    )
    assert len(groups([a, b, roundup])) == 3


async def test_run_dedup_follows_links_in_descriptions(session: AsyncSession) -> None:
    ats = Source(platform="workday", board_identifier="umd/UMCES", base_url="https://x")
    board = Source(platform="madgex", board_identifier="sc/jobs", base_url="https://y")
    session.add_all([ats, board])
    await session.flush()
    workday = "https://umd.wd1.myworkdayjobs.com/UMCES/job/IMET/Professor_JR105179"
    session.add_all(
        [
            Job(
                source_id=ats.id,
                external_id="w",
                title="Professor",
                source_url=workday,
                city="IMET",
                content_hash="w",
            ),
            Job(
                source_id=board.id,
                external_id="m",
                title="Professor of Symbiosis",
                company="UMCES",
                city="Baltimore",
                description_html=(
                    '<p>Apply <a href="https://umd.wd1.myworkdayjobs.com/en-US/UMCES/details/'
                    'Professor_JR105179?q=a&amp;b=c">here</a>.</p>'
                ),
                content_hash="m",
            ),
        ]
    )
    await session.commit()
    result = await run_dedup(session, now=T0)
    assert (result.postings, result.openings) == (2, 1)


def test_board_relisting_a_still_open_requisition_months_later_merges() -> None:
    """Real case (2026-11-08): JPMorgan's Oracle requisition posted 2026-08-07 and still
    listed; the ALA job board re-lists it on 2026-11-02 (87 days later)."""
    ats = posting(1, posted_at=T0, last_seen_at=T0 + timedelta(days=100))
    board = posting(2, posted_at=T0 + timedelta(days=87), last_seen_at=T0 + timedelta(days=100))
    assert len(groups([ats, board])) == 1


def test_new_york_city_is_new_york() -> None:
    columbia = posting(1, city="New York")
    board = posting(2, city="New York City")
    assert len(groups([columbia, board])) == 1
    assert len(groups([posting(3, city="New York"), posting(4, city="York")])) == 2


def test_same_generic_title_with_different_descriptions_stays_separate() -> None:
    """Real case (2026-11-08): Columbia's "Lecturer in Discipline" on Science Careers
    (Psychology) and on IMDiversity (Ethnicity and Race) - same title, city and dates."""
    psych = posting(1, title="Lecturer in Discipline", city="New York City")
    race = posting(2, title="Lecturer in Discipline", city="New York")
    words = {
        psych.id: description_words(
            "The Psychology Department invites applications for a lecturer position in "
            "cognitive neuroscience research methods statistics undergraduate courses"
        ),
        race.id: description_words(
            "The Center for the Study of Ethnicity and Race invites applications for a "
            "lecturer teaching comparative ethnic studies seminars"
        ),
    }
    assert len(groups([psych, race])) == 1  # without descriptions: merged
    by_head: dict[int, set[int]] = {}
    for job_id, head in cluster([psych, race], descriptions=words).items():
        by_head.setdefault(head, set()).add(job_id)
    assert len(by_head) == 2


def test_overlap_of_relisted_description() -> None:
    original = description_words("Join a dynamic legal team that shapes consumer banking")
    relisted = description_words(
        "Senior Counsel at JPMorgan. Join a dynamic legal team that shapes consumer banking"
    )
    assert overlap(original, relisted) == 1.0
    assert overlap(original, frozenset()) is None
