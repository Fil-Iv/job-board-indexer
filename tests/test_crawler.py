from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.registry import Registry
from jobindex.crawler import CrawlResult, crawl_source
from jobindex.http import FetchError, HttpClient
from jobindex.models import FetchRun, FetchStatus, Job, JobChange, JobStatus, RemoteType, Source
from tests.fakes import FakeConnector, FakeDetailConnector

Factory = async_sessionmaker[AsyncSession]


async def _make_source(session: AsyncSession, platform: str = "fake") -> int:
    source = Source(
        platform=platform,
        board_identifier="acme",
        company_name="Acme Inc",
        base_url="https://jobs.fake.test/acme",
    )
    session.add(source)
    await session.commit()
    return source.id


def _raw(external_id: str, title: str = "Engineer", **kw: Any) -> RawJob:
    return RawJob(external_id=external_id, title=title, **kw)


async def _crawl(factory: Factory, source_id: int, registry: Registry) -> CrawlResult:
    async with HttpClient(user_agent="test") as http:
        return await crawl_source(source_id, session_factory=factory, http=http, registry=registry)


def _counts(r: CrawlResult) -> tuple[int, int, int, int]:
    return (r.jobs_found, r.jobs_new, r.jobs_updated, r.jobs_closed)


async def _jobs(session: AsyncSession) -> dict[str, Job]:
    session.expire_all()
    return {j.external_id: j for j in (await session.scalars(select(Job))).all()}


async def _changes(session: AsyncSession) -> list[tuple[str, str | None, str | None]]:
    rows = (await session.scalars(select(JobChange).order_by(JobChange.id))).all()
    return [(c.field, c.old_value, c.new_value) for c in rows]


async def test_first_crawl_inserts_normalized_jobs(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _make_source(session)
    FakeConnector.result = [
        _raw(
            "1",
            description_html="<p>Build <b>things</b>. Salary range: $100,000 - $120,000.</p>",
            location_raw="Remote (US)",
        ),
        _raw("2", location_raw="Berlin, Germany", salary_min=Decimal("50000")),
    ]
    result = await _crawl(session_factory, source_id, fake_registry)

    assert result.status is FetchStatus.SUCCESS
    assert _counts(result) == (2, 2, 0, 0)
    jobs = await _jobs(session)
    one = jobs["1"]
    assert one.company == "Acme Inc"  # falls back to the source's company name
    assert one.description_text == "Build things. Salary range: $100,000 - $120,000."
    assert (one.remote_type, one.country) == (RemoteType.REMOTE, "US")
    assert (one.salary_min, one.salary_max, one.salary_currency) == (
        Decimal(100000),
        Decimal(120000),
        "USD",
    )
    assert (jobs["2"].city, jobs["2"].country) == ("Berlin", "DE")
    assert all(job.status is JobStatus.OPEN for job in jobs.values())
    assert all(len(job.content_hash) == 64 for job in jobs.values())

    source = await session.get(Source, source_id)
    assert source is not None
    assert source.last_success_at is not None
    assert source.consecutive_failures == 0


async def test_recrawl_tracks_changes_closes_and_reopens(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _make_source(session)
    FakeConnector.result = [_raw("1"), _raw("2")]
    await _crawl(session_factory, source_id, fake_registry)

    # "1" changes title, "2" disappears, "3" is new.
    FakeConnector.result = [_raw("1", title="Senior Engineer"), _raw("3")]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert _counts(result) == (2, 1, 1, 1)
    jobs = await _jobs(session)
    assert jobs["1"].title == "Senior Engineer"
    assert jobs["2"].status is JobStatus.CLOSED
    assert jobs["2"].closed_at is not None

    # "2" comes back.
    FakeConnector.result = [_raw("1", title="Senior Engineer"), _raw("2"), _raw("3")]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert _counts(result) == (3, 0, 1, 0)
    jobs = await _jobs(session)
    assert jobs["2"].status is JobStatus.OPEN
    assert jobs["2"].closed_at is None

    assert await _changes(session) == [
        ("title", "Engineer", "Senior Engineer"),
        ("status", "open", "closed"),
        ("status", "closed", "open"),
    ]


async def test_cosmetic_changes_update_silently(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _make_source(session)
    FakeConnector.result = [_raw("1", source_url="https://a?utm=1", description_html="<p>Hi</p>")]
    await _crawl(session_factory, source_id, fake_registry)
    before = (await _jobs(session))["1"].content_hash

    # New tracking param and different markup, same text: no change recorded.
    FakeConnector.result = [
        _raw("1", source_url="https://a?utm=2", description_html="<div>Hi</div>")
    ]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert _counts(result) == (1, 0, 0, 0)
    job = (await _jobs(session))["1"]
    assert job.source_url == "https://a?utm=2"
    assert job.description_html == "<div>Hi</div>"
    assert job.content_hash == before
    assert await _changes(session) == []


async def test_failed_fetch_never_closes_jobs(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _make_source(session)
    FakeConnector.result = [_raw("1"), _raw("2")]
    await _crawl(session_factory, source_id, fake_registry)

    for exc in (
        FetchError("HTTP 503", url="https://x", status_code=503),
        ConnectorError("unexpected payload"),
    ):
        FakeConnector.result = exc
        result = await _crawl(session_factory, source_id, fake_registry)
        assert result.status is FetchStatus.FAILED

    jobs = await _jobs(session)
    assert all(job.status is JobStatus.OPEN for job in jobs.values())
    assert await _changes(session) == []

    source = await session.get(Source, source_id)
    assert source is not None
    assert source.consecutive_failures == 2
    assert source.last_failure_at is not None
    assert source.last_error == "ConnectorError: unexpected payload"
    runs = (await session.scalars(select(FetchRun).order_by(FetchRun.id))).all()
    assert [r.status for r in runs] == [FetchStatus.SUCCESS, FetchStatus.FAILED, FetchStatus.FAILED]
    assert all(r.finished_at is not None for r in runs)

    # A later success resets the failure counter.
    FakeConnector.result = [_raw("1"), _raw("2")]
    await _crawl(session_factory, source_id, fake_registry)
    session.expire_all()
    source = await session.get(Source, source_id)
    assert source is not None
    assert (source.consecutive_failures, source.last_error) == (0, None)


async def test_database_error_is_recorded_as_failure(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _make_source(session)
    FakeConnector.result = [_raw("1", salary_currency="TOO-LONG")]  # violates varchar(3)
    result = await _crawl(session_factory, source_id, fake_registry)
    assert result.status is FetchStatus.FAILED
    assert await _jobs(session) == {}


async def test_unknown_platform_is_recorded_as_failure(
    session: AsyncSession, session_factory: Factory
) -> None:
    source_id = await _make_source(session)
    result = await _crawl(session_factory, source_id, Registry())
    assert result.status is FetchStatus.FAILED
    assert "UnknownPlatformError" in (result.error or "")


async def test_details_fetched_only_for_new_or_changed_postings(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _make_source(session, platform="fake-detail")
    FakeDetailConnector.result = [_raw("1"), _raw("2"), _raw("gone")]
    FakeDetailConnector.details = {"1": "<p>One</p>", "2": "<p>Two</p>", "gone": None}
    result = await _crawl(session_factory, source_id, fake_registry)
    assert _counts(result) == (2, 2, 0, 0)  # "gone" vanished between list and detail
    assert sorted(FakeDetailConnector.detail_calls) == ["1", "2", "gone"]

    FakeDetailConnector.detail_calls = []
    FakeDetailConnector.details = {"1": "<p>One v2</p>", "2": "<p>Two v2</p>"}
    FakeDetailConnector.result = [_raw("1"), _raw("2", title="Lead Engineer")]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert FakeDetailConnector.detail_calls == ["2"]  # "1" unchanged in the list: reused
    jobs = await _jobs(session)
    assert jobs["1"].description_text == "One"
    assert jobs["2"].description_text == "Two v2"
    assert _counts(result) == (2, 0, 1, 0)
