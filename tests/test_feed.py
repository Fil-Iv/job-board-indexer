"""RSS feed of a search (jobindex/api/feed.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from xml.etree.ElementTree import Element

import httpx
import pytest
from defusedxml.ElementTree import fromstring
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.models import Job, RemoteType, Source
from tests.test_api import _seed

T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def feed(client: httpx.AsyncClient, **params: str) -> Element:
    response = await client.get("/jobs.rss", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/rss+xml; charset=utf-8"
    root: Element = fromstring(response.content)
    assert root.tag == "rss"
    return root


def items(root: Element) -> list[Element]:
    return root.findall("./channel/item")


async def test_feed_items(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    root = await feed(client, q="python", location="Germany")
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == 'JobForge: "python", Germany'
    assert channel.findtext("link") == "http://test/?q=python&location=Germany"
    self_link = channel.find("{http://www.w3.org/2005/Atom}link")
    assert self_link is not None
    assert self_link.get("href") == "http://test/jobs.rss?q=python&location=Germany"

    [item] = items(root)
    assert item.findtext("title") == "Senior Python Engineer at Acme"
    assert item.findtext("link") == "https://boards.greenhouse.io/acme/jobs/1"
    assert item.findtext("guid") == "jobforge-opening-1"
    assert item.findtext("pubDate") == "Thu, 01 Oct 2026 00:00:00 GMT"
    assert item.findtext("description") == (
        "DE · remote · full time · EUR 70,000–90,000 / year · via Greenhouse, Niceboard"
    )


async def test_same_filters_as_search_page(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await _seed(session)
    for params in ({}, {"location": "austin, TX"}, {"status": "closed"}, {"remote_type": "remote"}):
        page_total = (await client.get("/jobs", params=params)).json()["total"]
        assert len(items(await feed(client, **params))) == page_total
    # Invalid values are ignored, like on the search page (no 422).
    assert len(items(await feed(client, remote_type="moon", posted_since="x"))) == 2


async def test_newest_first_even_with_query(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    session.add(Source(id=1, platform="lever", board_identifier="a", base_url="https://a"))
    await session.flush()
    for i, title in enumerate(["Python Python Python", "Python dev", "Senior Python"]):
        when = T0 + timedelta(days=i)
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title=title,
                remote_type=RemoteType.ONSITE,
                posted_at=when,
                first_seen_at=when,
                last_seen_at=when,
                content_hash=str(i),
                description_text="bad \x0b char",
                company="X\x01Co",
            )
        )
    await session.commit()
    root = await feed(client, q="python")  # control characters are dropped, XML parses
    assert [i.findtext("guid") for i in items(root)] == [
        "jobforge-opening-3",
        "jobforge-opening-2",
        "jobforge-opening-1",
    ]
    assert items(root)[0].findtext("category") == "XCo"


async def test_empty_feed_and_page_link(client: httpx.AsyncClient) -> None:
    root = await feed(client)
    assert items(root) == []
    assert root.findtext("./channel/title") == "JobForge: newest jobs"
    page = (await client.get("/", params={"q": "go", "page": "2"})).text
    assert 'href="/jobs.rss?q=go"' in page
    assert 'type="application/rss+xml"' in page
