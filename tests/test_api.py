from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.models import Job, JobStatus, RemoteType, Source


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    # Bypass the lifespan (which builds its own engine) and inject the test session factory.
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed(session: AsyncSession) -> None:
    source = Source(
        platform="fake", board_identifier="acme", base_url="https://jobs.fake.test/acme"
    )
    session.add(source)
    await session.flush()
    session.add_all(
        [
            Job(
                source_id=source.id,
                external_id="1",
                title="Senior Python Engineer",
                company="Acme",
                description_text="FastAPI and PostgreSQL",
                remote_type=RemoteType.REMOTE,
                country="DE",
                content_hash="a",
            ),
            Job(
                source_id=source.id,
                external_id="2",
                title="Product Designer",
                company="Acme",
                description_text="Figma",
                content_hash="b",
            ),
            Job(
                source_id=source.id,
                external_id="3",
                title="Python Intern",
                company="Acme",
                status=JobStatus.CLOSED,
                content_hash="c",
            ),
        ]
    )
    await session.commit()


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.json() == {"status": "ok"}


async def test_full_text_search(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)

    body = (await client.get("/jobs", params={"q": "python"})).json()
    assert body["total"] == 1  # closed intern posting excluded by default
    assert body["items"][0]["title"] == "Senior Python Engineer"

    body = (await client.get("/jobs", params={"q": "postgresql"})).json()  # matches description
    assert [j["external_id"] for j in body["items"]] == ["1"]

    body = (await client.get("/jobs", params={"q": "python", "status": "closed"})).json()
    assert [j["external_id"] for j in body["items"]] == ["3"]

    body = (await client.get("/jobs", params={"remote_type": "remote", "country": "de"})).json()
    assert [j["external_id"] for j in body["items"]] == ["1"]


async def test_job_detail_and_sources(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    assert (await client.get("/jobs/1")).json()["description_text"] == "FastAPI and PostgreSQL"
    assert (await client.get("/jobs/999")).status_code == 404
    assert [s["board_identifier"] for s in (await client.get("/sources")).json()] == ["acme"]
