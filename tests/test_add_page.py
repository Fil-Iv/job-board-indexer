"""/add page and POST /sources (jobindex/api/add_page.py)."""

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from jobindex.api import add_page
from jobindex.api.app import app
from jobindex.config import get_settings
from jobindex.db import create_session_factory, normalize_url
from jobindex.models import PendingSource, Source

LEVER = "https://jobs.lever.co/acme"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Like the public demo: no live checks (no crawling from the website)."""
    monkeypatch.setenv("INTAKE_LIVE_CHECKS", "false")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()
    add_page.check_limiter._hits.clear()
    add_page.write_limiter._hits.clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], env: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_form(client: httpx.AsyncClient) -> None:
    page = (await client.get("/add")).text
    assert 'aria-current="page">Add a board' in page
    assert '<form method="post" action="/add"' in page
    assert "Admin token" not in page


async def test_check_and_request(client: httpx.AsyncClient, session: AsyncSession) -> None:
    response = await client.post("/add", data={"url": LEVER, "action": "check"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    page = response.text
    assert "Lever.co" in page
    assert "URL pattern" in page
    assert "checked by the crawler" in page  # robots.txt / dry run, demo is read-only
    assert 'value="request">Request indexing' in page
    assert await session.scalar(select(PendingSource)) is None  # a check stores nothing

    page = (await client.post("/add", data={"url": LEVER, "action": "request"})).text
    assert "Request <b>#1</b> is queued for review." in page
    row = await session.scalar(select(PendingSource))
    assert row is not None
    assert (row.url, row.status, row.platform, row.board_identifier) == (
        LEVER,
        "pending",
        "lever",
        "acme",
    )
    again = (await client.post("/add", data={"url": LEVER, "action": "request"})).text
    assert "already in the review queue" in again
    status = (await client.get("/add", params={"request_id": 1})).text
    assert "Request #1" in status
    assert "pending" in status


async def test_bad_url(client: httpx.AsyncClient) -> None:
    response = await client.post("/add", data={"url": "javascript:alert(1)", "action": "check"})
    assert response.status_code == 400
    assert "doesn&#39;t look like a web address" in response.text


async def test_save_needs_token(
    client: httpx.AsyncClient, session: AsyncSession, env: pytest.MonkeyPatch
) -> None:
    denied = await client.post("/add", data={"url": LEVER, "action": "save", "token": "x"})
    assert denied.status_code == 403
    env.setenv("ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    assert "Admin token" in (await client.post("/add", data={"url": LEVER})).text
    wrong = await client.post("/add", data={"url": LEVER, "action": "save", "token": "nope"})
    assert wrong.status_code == 403
    ok = await client.post("/add", data={"url": LEVER, "action": "save", "token": "s3cret"})
    assert "Source <b>#" in ok.text
    assert await session.scalar(select(Source.board_identifier)) == "acme"


async def test_page_rate_limit(client: httpx.AsyncClient) -> None:
    for i in range(add_page.WRITES_PER_10_MINUTES):
        url = f"https://jobs.lever.co/a{i}"
        response = await client.post("/add", data={"url": url, "action": "request"})
        assert response.status_code == 200
    limited = await client.post("/add", data={"url": LEVER, "action": "request"})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


async def test_api_queue(client: httpx.AsyncClient) -> None:
    response = await client.post("/sources", json={"url": LEVER})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert (body["platform"], body["platform_name"], body["board_identifier"]) == (
        "lever",
        "Lever.co",
        "acme",
    )
    assert body["detected_by"] == "url"
    assert body["live_checked"] is False
    assert body["pending_id"] == 1
    again = (await client.post("/sources", json={"url": LEVER})).json()
    assert (again["status"], again["pending_id"]) == ("already_queued", 1)
    custom = (await client.post("/sources", json={"url": "acme.com/careers"})).json()
    assert (custom["status"], custom["platform"]) == ("queued", None)  # crawler checks it


async def test_api_save(client: httpx.AsyncClient, env: pytest.MonkeyPatch) -> None:
    assert (await client.post("/sources", json={"url": LEVER, "save": True})).status_code == 403
    env.setenv("ADMIN_TOKEN", "s3cret")
    get_settings.cache_clear()
    headers = {"Authorization": "Bearer s3cret"}
    created = await client.post("/sources", json={"url": LEVER, "save": True}, headers=headers)
    assert created.status_code == 201
    assert created.json()["status"] == "created"
    exists = await client.post("/sources", json={"url": LEVER, "save": True}, headers=headers)
    assert (exists.status_code, exists.json()["status"]) == (200, "exists")
    env.setenv("DATABASE_READ_ONLY", "true")  # the Vercel deployment can't save
    get_settings.cache_clear()
    other = {"url": "https://jobs.lever.co/beta", "save": True}
    assert (await client.post("/sources", json=other, headers=headers)).status_code == 403


async def test_api_errors_and_docs(client: httpx.AsyncClient) -> None:
    bad = await client.post("/sources", json={"url": "ftp://x.example/"})
    assert bad.status_code == 400
    schema = (await client.get("/openapi.json")).json()
    operation = schema["paths"]["/sources"]["post"]
    assert operation["summary"] == "Add a career page"
    assert {"201", "202", "403", "429"} <= set(operation["responses"])


async def test_writes_through_a_read_only_engine(migrated_db: str) -> None:
    """On Vercel every transaction is READ ONLY; the queue insert opts out explicitly."""
    engine = create_async_engine(
        normalize_url(migrated_db), execution_options={"postgresql_readonly": True}
    )
    insert = text("INSERT INTO pending_sources (url, submitted_at) VALUES ('x', now())")
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await session.execute(insert)
        app.state.session_factory = factory
        request = Request({"type": "http", "app": app, "headers": [], "method": "POST"})
        async with add_page.writable_session(request) as session:
            session.add(PendingSource(url="https://ok.example/jobs"))
            await session.commit()
        async with factory() as session:
            assert await session.scalar(select(PendingSource.url)) == "https://ok.example/jobs"
    finally:
        await engine.dispose()
