"""Job detail page /job/{id} (jobindex/api/job_page.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.dedup import run_dedup
from jobindex.models import Job, JobChange, RemoteType, Source
from jobindex.models.enums import ChangeCause

T0 = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
T1 = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def seed(session: AsyncSession) -> tuple[int, int]:
    session.add_all(
        [
            Source(id=1, platform="greenhouse", board_identifier="acme", base_url="https://a"),
            Source(
                id=2, platform="wpjobmanager", board_identifier="jobs.example", base_url="https://b"
            ),
        ]
    )
    await session.flush()
    common = {
        "title": "Data Engineer",
        "company": "Acme",
        "city": "Berlin",
        "country": "DE",
        "content_hash": "x",
    }
    first = Job(
        source_id=1,
        external_id="g1",
        description_text="Build pipelines.\nUse <script>alert(1)</script> nothing.",
        description_html="<p>Build pipelines.</p><script>alert(1)</script>",
        remote_type=RemoteType.HYBRID,
        employment_type="full_time",
        salary_min=Decimal(70000),
        salary_max=Decimal(90000),
        salary_currency="EUR",
        salary_period="year",
        posted_at=T0,
        first_seen_at=T0,
        last_seen_at=T1,
        last_checked_at=T1,
        source_url="https://boards.greenhouse.io/acme/jobs/1",
        apply_url="https://boards.greenhouse.io/acme/jobs/1#app",
        **common,
    )
    second = Job(
        source_id=2,
        external_id="w1",
        first_seen_at=T1,
        last_seen_at=T1,
        last_checked_at=T1,
        source_url="https://jobs.example/job/1",
        **common,
    )
    session.add_all([first, second])
    await session.flush()
    session.add_all(
        [
            JobChange(
                job_id=first.id,
                changed_at=T1,
                field="title",
                old_value="Data Eng",
                new_value="Data Engineer",
                cause=ChangeCause.UPSTREAM,
            ),
            JobChange(
                job_id=first.id,
                changed_at=T1,
                field="description_text",
                old_value="a" * 500,
                new_value="b" * 500,
                cause=ChangeCause.PARSER,
            ),
            JobChange(
                job_id=second.id,
                changed_at=T1,
                field="status",
                old_value="closed",
                new_value="open",
                cause=ChangeCause.UPSTREAM,
            ),
        ]
    )
    await session.commit()
    await run_dedup(session, now=T1)
    return first.id, second.id


async def test_job_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    first, second = await seed(session)
    response = await client.get(f"/job/{first}")
    assert response.status_code == 200
    page = response.text
    assert "<title>Data Engineer – Acme – JobForge</title>" in page
    assert "EUR" in page
    assert "70,000–90,000" in page
    assert "full time" in page
    assert "hybrid" in page
    assert "2026-11-10 09:30 UTC" in page  # posted / first seen
    assert "2026-11-12 14:00 UTC" in page  # last checked
    # deduplication made visible: both listings, with names and links
    assert "Found on 2 sites" in page
    assert "Greenhouse" in page
    assert "WP Job Manager" in page
    assert 'href="https://boards.greenhouse.io/acme/jobs/1"' in page
    assert 'href="https://jobs.example/job/1"' in page
    # change history of both listings; long fields summarised
    assert "<del>Data Eng</del> → <ins>Data Engineer</ins>" in page
    assert "<del>closed</del> → <ins>open</ins>" in page
    assert "text changed" in page
    assert "a" * 200 not in page
    # the description is plain text: crawled HTML is never rendered
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    # any posting ID of the opening works
    assert "Found on 2 sites" in (await client.get(f"/job/{second}")).text


async def test_cards_link_to_the_job_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    first, _ = await seed(session)
    page = (await client.get("/?q=engineer&status=any")).text
    assert f'<a href="/job/{first}">Data Engineer</a>' in page
    assert f'href="/job/{first}#listings"' in page  # "2 sources" badge


async def test_unknown_job(client: httpx.AsyncClient) -> None:
    response = await client.get("/job/123456")
    assert response.status_code == 404
    assert "Job not found" in response.text
    assert (await client.get("/job/abc")).status_code == 422
