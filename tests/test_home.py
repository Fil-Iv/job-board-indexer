"""Home-page world map / globe hero and site icon (templates/_globe.html, ui.favicon)."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api import places
from jobindex.api.app import app
from jobindex.models import Job, Source


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_map_on_the_landing_page_only(client: httpx.AsyncClient) -> None:
    landing = (await client.get("/")).text
    assert 'id="atlas"' in landing
    assert 'id="globe"' in landing
    assert "/places.json" in landing
    for query in ("/?q=python", "/?location=Berlin", "/?page=2"):
        assert 'id="atlas"' not in (await client.get(query)).text, query


async def test_map_assets_are_pinned_with_integrity(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    assert "<script defer src=" in page
    for tag in re.findall(r"<script[^>]*\bsrc=[^>]*>", page):
        assert re.search(r'src="https://cdn\.jsdelivr\.net/npm/[a-z0-9-]+@\d+\.\d+\.\d+/', tag), tag
        assert 'integrity="sha384-' in tag, tag
    atlas = re.search(r'"(https://cdn\.jsdelivr\.net/npm/world-atlas@[\d.]+/[^"]+)"', page)
    assert atlas is not None
    assert 'ATLAS_SRI = "sha384-' in page


async def test_favicon(client: httpx.AsyncClient) -> None:
    response = await client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.text.startswith("<svg")
    assert 'href="/favicon.svg"' in (await client.get("/browse")).text


async def test_map_toggle_and_briefing(client: httpx.AsyncClient, session: AsyncSession) -> None:
    places._cache.clear()
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    session.add(Source(id=1, platform="greenhouse", board_identifier="a", base_url="https://a"))
    await session.flush()
    for i, cc in enumerate(["US", "US", "DE"]):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title="T",
                country=cc,
                first_seen_at=t0,
                last_seen_at=t0,
                content_hash=str(i),
            )
        )
    await session.commit()
    page = (await client.get("/")).text
    places._cache.clear()
    assert '<div class="view-toggle" role="group" aria-label="View" hidden>' in page
    # the map is the main view, the globe the second one
    assert page.index('data-view="map" aria-pressed="true"') < page.index('data-view="globe"')
    assert '<div class="atlas" id="atlas" hidden>' in page
    assert "geoNaturalEarth1" in page
    assert "Countries with open jobs (2):" in page
    assert page.index("/browse/US") < page.index("/browse/DE")  # by count
    assert "United States&nbsp;<b>2</b>" in page
    assert "Countries with open jobs" not in (await client.get("/?q=x")).text
