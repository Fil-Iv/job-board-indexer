"""Browse by place (jobindex/api/browse.py): countries → regions/cities → search."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.api.browse import search_url
from jobindex.models import Job, JobStatus, Source

T0 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


PLACES = [  # city, region, country
    ("Austin", "TX", "US"),
    ("Austin", "TX", "US"),
    ("Dallas", "TX", "US"),
    ("Portland", "OR", "US"),
    ("Portland", "ME", "US"),
    (None, None, "US"),
    ("Berlin", "Berlin", "DE"),
    (None, None, None),
]


async def seed(session: AsyncSession) -> None:
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    for i, (city, region, country) in enumerate(PLACES):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title=f"Job {i}",
                city=city,
                region=region,
                country=country,
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
            city="Paris",
            country="FR",
            status=JobStatus.CLOSED,
            first_seen_at=T0,
            last_seen_at=T0,
            content_hash="c",
        )
    )
    await session.commit()


def links(page: str, prefix: str) -> list[str]:
    return [
        h.replace("&amp;", "&") for h in re.findall(r'href="([^"]+)"', page) if h.startswith(prefix)
    ]


async def test_countries(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    response = await client.get("/browse")
    assert response.status_code == 200
    assert "s-maxage=600" in response.headers["cache-control"]
    page = response.text
    assert links(page, "/browse/") == ["/browse/US", "/browse/DE"]  # by count; FR closed
    assert "United States" in page
    assert "Germany" in page
    assert 'aria-current="page">Browse' in page


async def test_country_regions_and_cities(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    page = (await client.get("/browse/us")).text
    assert "6 open jobs" in page
    hrefs = links(page, "/?location=")
    locations = [parse_qs(urlsplit(h).query)["location"][0] for h in hrefs]
    assert locations[0] == "US"  # "search all jobs in United States"
    assert locations[1:4] == ["TX, US", "ME, US", "OR, US"]  # regions by count, then name
    assert locations[4:] == [
        "Austin, TX, US",
        "Dallas, TX, US",
        "Portland, ME, US",
        "Portland, OR, US",
    ]


@pytest.mark.parametrize(
    ("location", "expected"),
    [(search_url("US"), 6), (search_url("TX", "US"), 3), (search_url("Portland", "OR", "US"), 1)],
)
async def test_links_lead_to_matching_search(
    client: httpx.AsyncClient, session: AsyncSession, location: str, expected: int
) -> None:
    await seed(session)
    query = parse_qs(urlsplit(location).query)["location"][0]
    body = (await client.get("/jobs", params={"location": query})).json()
    assert body["total"] == expected


async def test_unknown_or_empty_country(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    empty = await client.get("/browse/FR")
    assert empty.status_code == 200
    assert "No open jobs in France right now." in empty.text
    assert (await client.get("/browse/1x")).status_code == 404
    assert (await client.get("/browse/USA")).status_code == 404


async def test_remote_regions(client: httpx.AsyncClient, session: AsyncSession) -> None:
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    for i, raw in enumerate(["Remote - EMEA", "Europe", "Remote (Europe)"]):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title=f"Job {i}",
                location_raw=raw,
                first_seen_at=T0,
                last_seen_at=T0,
                content_hash=str(i),
            )
        )
    await session.commit()
    page = (await client.get("/browse")).text
    assert "Remote, by region" in page
    assert "Remote – Europe" in page
    assert "Remote – EMEA" in page
    assert links(page, "/?location=") == ["/?location=EMEA", "/?location=Europe"]
