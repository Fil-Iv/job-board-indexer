"""Map points for the home-page globe (jobindex/api/places.py, ``GET /places.json``)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select
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


async def test_places_filtered_by_search(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed(session)
    # jobs 3-5 are Portland (US); "Job 3" matches only the first of them by title
    body = (await client.get("/places.json", params={"q": "Job 3", "location": "US"})).json()
    assert [(p["name"], p["count"]) for p in body["places"]] == [("Portland", 1)]
    assert set(body["countries"]) == {"840"}
    country = body["countries"]["840"]
    # links narrow the current search instead of going to /browse
    assert parse_qs(urlsplit(country["href"]).query) == {"q": ["Job 3"], "location": ["US"]}
    point = parse_qs(urlsplit(body["places"][0]["href"]).query)
    assert point["q"] == ["Job 3"]
    assert point["location"] == ["Portland, OR, US"]
    # the page's lenient parsing: invalid values are ignored, not a 422
    assert (await client.get("/places.json", params={"remote_type": "nope"})).status_code == 200


async def test_compact_map_on_search_results(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)
    page = (await client.get("/", params={"q": "job", "location": "GB"})).text
    assert 'class="hero compact"' in page
    assert 'data-places="/places.json?q=job&amp;location=GB"' in page
    assert "Where these jobs are" in page
    assert 'data-view="globe"' in page  # the globe toggle stays
    assert "Countries with open jobs" not in page
    nothing = (await client.get("/", params={"q": "zzzz-no-match"})).text
    assert 'id="hero"' not in nothing  # no map over an empty result


async def seed_areas(session: AsyncSession) -> None:
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    rows = [  # location_raw, country
        ("Remote - EMEA", None),
        ("Europe", None),
        ("Remote, EU", None),
        ("APAC - Remote", None),
        ("Remote, Global", None),
        ("Remote - Bulgaria", "BG"),  # has a country: no area
        ("Remote", None),  # names no region: no area
    ]
    for i, (raw, country) in enumerate(rows):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title=f"Job {i}",
                location_raw=raw,
                country=country,
                first_seen_at=T0,
                last_seen_at=T0,
                content_hash=str(i),
            )
        )
    await session.commit()


async def test_area_column(session: AsyncSession) -> None:
    await seed_areas(session)
    rows = (await session.execute(select(Job.location_raw, Job.area).order_by(Job.id))).all()
    assert [tuple(r) for r in rows] == [
        ("Remote - EMEA", "emea"),
        ("Europe", "europe"),
        ("Remote, EU", "europe"),
        ("APAC - Remote", "apac"),
        ("Remote, Global", "worldwide"),
        ("Remote - Bulgaria", None),
        ("Remote", None),
    ]


async def test_areas_on_the_map(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await seed_areas(session)
    areas = {a["code"]: a for a in (await client.get("/places.json")).json()["areas"]}
    assert list(areas) == ["emea", "europe", "apac", "worldwide"]  # geo.AREAS order
    europe = areas["europe"]
    assert (europe["label"], europe["count"], europe["href"]) == (
        "Remote – Europe",
        2,
        "/?location=Europe",
    )
    assert "100" in europe["countries"]  # Bulgaria is highlighted with Europe
    assert "840" not in europe["countries"]
    assert set(europe["countries"]) < set(areas["emea"]["countries"])
    assert areas["worldwide"]["countries"] == []  # listed, nothing to highlight
    for area in areas.values():  # the links find exactly the counted postings
        location = parse_qs(urlsplit(area["href"]).query)["location"][0]
        found = (await client.get("/jobs", params={"location": location})).json()["total"]
        assert found == area["count"], area["code"]
