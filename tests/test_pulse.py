"""Market pulse page (jobindex/api/pulse.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api import pulse
from jobindex.api.app import app
from jobindex.dedup import run_dedup
from jobindex.models import Job, JobStatus, RemoteType, Source

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    pulse._cache.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    pulse._cache.clear()


async def seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"),
            Source(id=2, platform="lever", board_identifier="b", base_url="https://b"),
        ]
    )
    await session.flush()
    jobs = []
    for i in range(10):  # 6 US (Acme), 3 DE (Beta), 1 without a country
        country = "US" if i < 6 else "DE" if i < 9 else None
        posted = NOW - timedelta(hours=2 if i < 2 else 72 if i < 5 else 24 * 30)
        jobs.append(
            Job(
                source_id=1 if i < 7 else 2,
                external_id=str(i),
                title=f"Engineer {i}",
                company="Acme" if i < 6 else "Beta",
                country=country,
                remote_type=[RemoteType.REMOTE, RemoteType.HYBRID][i % 2]
                if i < 4
                else RemoteType.UNKNOWN,
                salary_min=Decimal(100_000 + 10_000 * i) if i < 6 else None,
                salary_max=Decimal(120_000 + 10_000 * i) if i < 6 else None,
                salary_currency="USD" if i < 6 else None,
                salary_period="year" if i < 6 else None,
                posted_at=posted,
                first_seen_at=posted,
                last_seen_at=NOW,
                content_hash=str(i),
            )
        )
    jobs.append(  # closed: ignored everywhere
        Job(
            source_id=1,
            external_id="x",
            title="Old",
            company="Zed",
            country="FR",
            status=JobStatus.CLOSED,
            first_seen_at=NOW,
            last_seen_at=NOW,
            content_hash="x",
        )
    )
    session.add_all(jobs)
    await session.commit()
    await run_dedup(session, now=NOW)


async def test_compute_pulse(session: AsyncSession) -> None:
    await seed(session)
    p = await pulse.compute_pulse(session, now=NOW)
    assert (p.open_openings, p.new_24h, p.new_7d) == (10, 2, 5)

    assert [(b.label, b.count, b.key) for b in p.countries] == [
        ("United States", 6, "US"),
        ("Germany", 3, "DE"),
        ("Not stated", 1, None),
    ]
    assert p.countries[0].pct == 100
    assert p.countries[0].share == pytest.approx(0.6)
    assert p.countries[0].href == "/browse/US"

    assert [(b.label, b.count) for b in p.platforms] == [("greenhouse", 7), ("lever", 3)]
    assert [(b.key, b.count) for b in p.workplace] == [
        ("remote", 2),
        ("hybrid", 2),
        ("onsite", 0),
        ("unknown", 6),
    ]
    assert sum(b.share for b in p.workplace) == pytest.approx(1)
    assert [(b.label, b.count, b.href) for b in p.companies] == [
        ("Acme", 6, "/?company=Acme"),
        ("Beta", 4, "/?company=Beta"),
    ]
    [band] = p.salaries
    assert (band.currency, band.period, band.count) == ("USD", "year", 6)
    assert band.median == Decimal(135_000)  # midpoints 110k..160k
    assert band.p25 < band.median < band.p75 <= band.p90
    assert band.pos(band.p90) == 100


async def test_pulse_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    response = await client.get("/pulse")
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public, max-age=0, s-maxage=600")
    page = response.text
    for section in ("workplace", "salaries", "countries", "companies", "platforms"):
        assert f'id="{section}"' in page
    assert '<a href="/browse/US">United States</a>' in page
    assert "USD / year" in page
    assert 'aria-current="page">Pulse' in page


async def test_pulse_is_cached(client: httpx.AsyncClient, session: AsyncSession) -> None:
    assert "<b>0</b><span>open jobs" in (await client.get("/pulse")).text
    await seed(session)
    assert "<b>0</b><span>open jobs" in (await client.get("/pulse")).text  # cached
    pulse._cache.clear()
    assert "<b>10</b><span>open jobs" in (await client.get("/pulse")).text


async def test_pulse_empty_index(client: httpx.AsyncClient) -> None:
    page = (await client.get("/pulse")).text
    assert "Not enough postings publish a salary yet." in page
