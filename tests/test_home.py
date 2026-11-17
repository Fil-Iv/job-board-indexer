"""Home-page hero globe and site icon (templates/_globe.html, ui.favicon)."""

import re
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_globe_on_the_landing_page_only(client: httpx.AsyncClient) -> None:
    landing = (await client.get("/")).text
    assert 'id="astrolabe"' in landing
    assert "/places.json" in landing
    for query in ("/?q=python", "/?location=Berlin", "/?page=2"):
        assert 'id="astrolabe"' not in (await client.get(query)).text, query


async def test_globe_assets_are_pinned_with_integrity(client: httpx.AsyncClient) -> None:
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
