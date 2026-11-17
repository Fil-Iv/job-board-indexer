"""Crawl status page /status (jobindex/api/status_page.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.api.status_page import kept_open_example
from jobindex.models import FetchRun, FetchStatus, Job, JobStatus, Source

T0 = datetime(2026, 11, 15, 8, tzinfo=UTC)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _run(source: int, start: datetime, status: FetchStatus, **kw: object) -> FetchRun:
    return FetchRun(
        source_id=source,
        started_at=start,
        finished_at=start + timedelta(seconds=12),
        status=status,
        **kw,
    )


async def seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Source(
                id=1,
                platform="greenhouse",
                board_identifier="acme",
                base_url="https://a",
                last_success_at=T0,
            ),
            Source(
                id=2,
                platform="lever",
                board_identifier="beta",
                base_url="https://b",
                last_success_at=T0,
                consecutive_failures=2,
                last_error="FetchError: HTTP 503 from https://api.lever.co/v0/postings/beta",
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            _run(
                1, T0, FetchStatus.SUCCESS, jobs_found=5, jobs_new=2, jobs_updated=1, jobs_closed=1
            ),
            _run(2, T0, FetchStatus.SUCCESS, jobs_found=3, jobs_new=3),
            _run(2, T0 + timedelta(hours=1), FetchStatus.FAILED, error="HTTP 503"),
            _run(
                2,
                T0 + timedelta(hours=2),
                FetchStatus.FAILED,
                error="FetchError: HTTP 503 from https://api.lever.co/v0/postings/beta",
            ),
        ]
    )
    for i in range(3):  # lever's jobs, last seen by the successful run, still open
        session.add(
            Job(
                source_id=2,
                external_id=str(i),
                title="Engineer",
                first_seen_at=T0,
                last_seen_at=T0,
                content_hash=str(i),
            )
        )
    session.add(
        Job(
            source_id=1,
            external_id="c",
            title="Closed",
            status=JobStatus.CLOSED,
            first_seen_at=T0,
            last_seen_at=T0,
            content_hash="c",
        )
    )
    await session.commit()


async def test_kept_open_example(session: AsyncSession) -> None:
    await seed(session)
    example = await kept_open_example(session)
    assert example is not None
    assert example.run.platform == "lever"
    assert example.run.started_at == T0 + timedelta(hours=2)  # the latest failed run
    assert example.kept_open == 3
    assert example.last_success_at == T0


async def test_no_example_without_failures(session: AsyncSession) -> None:
    assert await kept_open_example(session) is None


async def test_status_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    response = await client.get("/status")
    assert response.status_code == 200
    assert "s-maxage=120" in response.headers["cache-control"]
    page = response.text
    assert 'aria-current="page">Status' in page
    assert "A failed fetch never closes jobs" in page
    assert "Latest real example:" in page
    assert "<b>3</b> open jobs that this run could not see" in page
    assert "Lever.co · beta" in page
    # failing now
    failing = page[page.index('id="failing"') : page.index('id="recent"')]
    assert "Failing now (1)" in failing
    assert "HTTP 503" in failing
    # latest per source: one row each, failed first; success shows the counts
    per_source = page[page.index('id="sources"') :]
    assert per_source.count("<tr") == 3  # header + 2 sources
    assert per_source.index("Lever.co") < per_source.index("Greenhouse")
    assert '<td data-label="Closed" class="num">1</td>' in per_source
    # recent runs: all four, failed ones without counts
    recent = page[page.index('id="recent"') : page.index('id="sources"')]
    assert recent.count('class="failed-row"') == 2
    assert "12 s" in recent
