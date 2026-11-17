"""Map points for the home-page globe (jobindex/api/places.py, ``GET /places.json``)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api import places
from jobindex.api.app import app
from jobindex.models import Job, JobStatus, Source

T0 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    places._cache.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    places._cache.clear()


PLACES = [  # city, region, country
    ("London", None, "GB"),
    ("LONDON", "LONDON", "GB"),
    ("London", "England", "GB"),
    ("Portland", "OR", "US"),
    ("Portland", "OR", "US"),
    ("Portland", "ME", "US"),
    ("Köln", None, "DE"),
    ("Kyiv | Київ", None, "UA"),
    ("Israel", "Tel Aviv", "IL"),
    ("Atlantis", None, "US"),  # no such city: left off the map
    (None, None, "FR"),  # country only: counted per country, no point
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
            external_id="closed",
            title="Closed",
            city="Paris",
            country="FR",
            status=JobStatus.CLOSED,
            first_seen_at=T0,
            last_seen_at=T0,
            content_hash="closed",
        )
    )
    await session.commit()


@pytest.mark.parametrize(
    ("city", "region", "country", "expected"),
    [
        ("London", None, "GB", ("London", "GB")),
        ("LONDON", "LONDON", "GB", ("London", "GB")),
        ("Köln", None, "DE", ("Köln", "DE")),
        ("KOELN", None, "DE", ("Köln", "DE")),
        ("Cologne", None, "DE", ("Köln", "DE")),  # alternate name
        ("Portland", "ME", "US", ("Portland", "US", "ME")),  # region breaks the tie
        ("Portland", "OR", "US", ("Portland", "US", "OR")),
        ("Portland", None, "US", ("Portland", "US", "OR")),  # most populous
        ("Kyiv | Київ", None, "UA", ("Kyiv", "UA")),
        ("Cairo - ETIC", None, "EG", ("Cairo", "EG")),
        ("Israel", "Tel Aviv", "IL", ("Tel Aviv", "IL")),  # country in city, city in region
        ("Bengaluru Millenia", None, "IN", ("Bengaluru", "IN")),  # leading words
        ("Makati", None, "PH", ("Makati City", "PH")),
        ("London", None, "CA", ("London", "CA")),  # matched within the country
        ("Atlantis", None, "US", None),
        ("London", None, None, None),
        (None, None, "GB", None),
    ],
)
def test_locate(
    city: str | None,
    region: str | None,
    country: str | None,
    expected: tuple[str, ...] | None,
) -> None:
    found = places.locate(city, region, country)
    if expected is None:
        assert found is None
        return
    assert found is not None
    assert (found.name, found.country) == expected[:2]
    if len(expected) == 3:
        assert found.admin1 == expected[2]
    assert -90 <= found.lat <= 90
    assert -180 <= found.lng <= 180


async def test_places_json(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    response = await client.get("/places.json")
    assert response.status_code == 200
    assert "s-maxage=600" in response.headers["cache-control"]
    body = response.json()
    names = [(p["name"], p["country"], p["count"]) for p in body["places"]]
    assert names == [  # by openings, then name; Atlantis and FR (no city) left out
        ("London", "GB", 3),  # three spellings, one city
        ("Portland", "US", 2),
        ("Kyiv", "UA", 1),
        ("Köln", "DE", 1),
        ("Portland", "US", 1),
        ("Tel Aviv", "IL", 1),
    ]
    london = body["places"][0]
    assert london["country_name"] == "United Kingdom"
    assert 51 < london["lat"] < 52
    assert -1 < london["lng"] < 1
    countries = body["countries"]
    assert countries["826"] == {
        "code": "GB",
        "name": "United Kingdom",
        "count": 3,
        "href": "/browse/GB",
    }
    assert countries["840"]["count"] == 4  # Portland x3 + Atlantis
    assert countries["250"]["count"] == 1  # FR: open country-only posting, closed Paris not
    assert "GeoNames" in body["attribution"]


async def test_place_links_find_the_postings(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)
    body = (await client.get("/places.json")).json()
    for point in body["places"]:
        location = parse_qs(urlsplit(point["href"]).query)["location"][0]
        found = (await client.get("/jobs", params={"location": location})).json()["total"]
        assert found == point["count"], (point["name"], location)


async def test_cached_in_process(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    first = (await client.get("/places.json")).json()
    session.add(
        Job(
            source_id=1,
            external_id="new",
            title="New",
            city="Berlin",
            country="DE",
            first_seen_at=T0,
            last_seen_at=T0,
            content_hash="new",
        )
    )
    await session.commit()
    assert (await client.get("/places.json")).json() == first
    places._cache.clear()
    assert any(p["name"] == "Berlin" for p in (await client.get("/places.json")).json()["places"])


async def test_empty_index(client: httpx.AsyncClient) -> None:
    body = (await client.get("/places.json")).json()
    assert body["places"] == []
    assert body["countries"] == {}
