"""Salaries without a currency (jobindex/currency.py): the location's currency is shown,
flagged as inferred, or "currency not stated"; stored data and the JSON API are unchanged."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.api.mcp import SearchJobsArgs, search_jobs
from jobindex.currency import infer_currency
from jobindex.models import Job, Source

T0 = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("country", "currency"),
    [
        ("IN", "INR"),
        ("in", "INR"),
        ("US", "USD"),
        ("DE", "EUR"),
        ("IE", "EUR"),
        ("GB", "GBP"),
        ("CH", "CHF"),
        ("JP", "JPY"),
        ("SG", "SGD"),
        ("XX", None),
        (None, None),
        ("", None),
    ],
)
def test_infer_currency(country: str | None, currency: str | None) -> None:
    assert infer_currency(country) == currency


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def seed(session: AsyncSession) -> dict[str, int]:
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    rows = {  # title: (currency, country)
        "Python Developer": (None, "IN"),  # Tower Research Capital, Gurugram: 1,000,000 / year
        "Quant Researcher": (None, None),
        "Backend Engineer": ("USD", "IN"),  # a published currency always wins
    }
    ids = {}
    for i, (title, (currency, country)) in enumerate(rows.items()):
        job = Job(
            source_id=1,
            external_id=str(i),
            title=title,
            company="Tower Research Capital",
            country=country,
            salary_min=Decimal(1_000_000),
            salary_max=Decimal(1_000_000),
            salary_currency=currency,
            salary_period="year",
            first_seen_at=T0,
            last_seen_at=T0,
            content_hash=str(i),
        )
        session.add(job)
        await session.flush()
        ids[title] = job.id
    await session.commit()
    return ids


async def test_pages(client: httpx.AsyncClient, session: AsyncSession) -> None:
    ids = await seed(session)
    india = (await client.get(f"/job/{ids['Python Developer']}")).text
    assert "INR" in india
    assert "1,000,000" in india
    assert "(currency inferred from the location)" in india
    unknown = (await client.get(f"/job/{ids['Quant Researcher']}")).text
    assert "(currency not stated)" in unknown
    stated = (await client.get(f"/job/{ids['Backend Engineer']}")).text
    assert "USD" in stated
    assert "currency inferred" not in stated
    assert "currency not stated" not in stated
    cards = (await client.get("/?q=developer")).text
    assert "(currency inferred from the location)" in cards


async def test_api_keeps_the_published_value(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    ids = await seed(session)
    body = (await client.get(f"/jobs/{ids['Python Developer']}")).json()
    assert body["salary_currency"] is None


async def test_mcp_and_rss_flag_the_inference(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)
    found = await search_jobs(session, SearchJobsArgs(q="python"))
    salary = found["jobs"][0]["salary"]
    assert (salary["currency"], salary["currency_inferred"]) == ("INR", True)
    rss = (await client.get("/jobs.rss", params={"q": "quant"})).text
    assert "(currency not stated)" in rss
