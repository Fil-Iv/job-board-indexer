from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.dedup import run_dedup
from jobindex.models import FetchRun, FetchStatus, Job, JobStatus, RemoteType, Source

T0 = datetime(2026, 10, 1, tzinfo=UTC)


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
    """Acme's Greenhouse board + a job board that re-lists one Acme posting."""
    ats = Source(platform="greenhouse", board_identifier="acme", base_url="https://x/acme", id=1)
    board = Source(
        platform="niceboard",
        board_identifier="jobs.example",
        base_url="https://jobs.example",
        id=2,
        consecutive_failures=2,
        last_error="HTTP 503",
    )
    session.add_all([ats, board])
    await session.flush()
    common = {"company": "Acme", "posted_at": T0, "first_seen_at": T0, "last_seen_at": T0}
    session.add_all(
        [
            Job(
                source_id=1,
                external_id="1",
                title="Senior Python Engineer",
                description_text="FastAPI and PostgreSQL",
                remote_type=RemoteType.REMOTE,
                country="DE",
                employment_type="full_time",
                source_url="https://boards.greenhouse.io/acme/jobs/1",
                apply_url="https://boards.greenhouse.io/acme/jobs/1#app",
                content_hash="a",
                **common,
            ),
            Job(  # the same opening on the job board, with a salary
                source_id=2,
                external_id="n1",
                title="Senior Python Engineer",
                remote_type=RemoteType.REMOTE,
                country="DE",
                salary_min=Decimal(70000),
                salary_max=Decimal(90000),
                salary_currency="EUR",
                salary_period="year",
                source_url="https://jobs.example/job/senior-python",
                content_hash="b",
                **common,
            ),
            Job(
                source_id=1,
                external_id="2",
                title="Product Designer",
                description_text="Figma",
                city="Austin",
                region="TX",
                country="US",
                content_hash="c",
                **common,
            ),
            Job(
                source_id=1,
                external_id="3",
                title="Python Intern",
                status=JobStatus.CLOSED,
                content_hash="d",
                **common,
            ),
        ]
    )
    session.add(FetchRun(source_id=2, status=FetchStatus.FAILED, error="HTTP 503", finished_at=T0))
    await session.commit()
    await run_dedup(session, now=T0)


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_search_returns_one_result_per_opening(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await _seed(session)
    body = (await client.get("/jobs", params={"q": "python"})).json()
    assert body["total"] == 1  # two listings of one opening; closed intern excluded
    item = body["items"][0]
    assert item["id"] == 1
    assert [s["platform"] for s in item["sources"]] == ["greenhouse", "niceboard"]
    assert item["sources"][0]["apply_url"] == "https://boards.greenhouse.io/acme/jobs/1#app"
    assert item["salary_max"] == "90000.00"  # taken from the listing that has it

    # A filter matching only the job-board listing still returns the opening.
    body = (await client.get("/jobs", params={"salary_min": 80000})).json()
    assert [j["id"] for j in body["items"]] == [1]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, [1, 3]),
        ({"q": "postgresql"}, [1]),  # description match
        ({"q": "python", "status": "closed"}, [4]),
        ({"q": "python", "status": "any"}, [1, 4]),
        ({"location": "Germany"}, [1]),
        ({"location": "austin, TX"}, [3]),
        ({"location": "Austin, DE"}, []),
        ({"remote_type": "remote"}, [1]),
        ({"employment_type": "full_time"}, [1]),
        ({"company": "acm"}, [1, 3]),
        ({"salary_min": 95000}, []),
        ({"salary_currency": "usd"}, []),
        ({"posted_since": "2026-10-02"}, []),
        ({"posted_since": "2026-09-30"}, [1, 3]),
        ({"platform": "niceboard"}, [1]),
    ],
)
async def test_search_filters(
    client: httpx.AsyncClient,
    session: AsyncSession,
    params: dict[str, str | int],
    expected: list[int],
) -> None:
    await _seed(session)
    response = await client.get("/jobs", params=params)
    assert response.status_code == 200, response.text
    assert sorted(j["id"] for j in response.json()["items"]) == expected


async def test_pagination(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    first = (await client.get("/jobs", params={"size": 1})).json()
    second = (await client.get("/jobs", params={"size": 1, "page": 2})).json()
    assert (first["total"], first["pages"]) == (2, 2)
    assert [first["items"][0]["id"], second["items"][0]["id"]] == [3, 1]
    assert (await client.get("/jobs", params={"size": 101})).status_code == 422


async def test_job_detail(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    detail = (await client.get("/jobs/1")).json()
    assert detail["description_text"] == "FastAPI and PostgreSQL"
    assert len(detail["sources"]) == 2
    assert (await client.get("/jobs/2")).json()["id"] == 1  # duplicate resolves to opening
    assert (await client.get("/jobs/999")).status_code == 404


async def test_sources_and_stats(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    sources = (await client.get("/sources")).json()
    assert [(s["board_identifier"], s["open_jobs"]) for s in sources] == [
        ("acme", 2),
        ("jobs.example", 1),
    ]
    failing = (await client.get("/sources", params={"failing": "true"})).json()
    assert [s["id"] for s in failing] == [2]

    stats = (await client.get("/stats")).json()
    assert (stats["open_jobs"], stats["open_openings"], stats["failing_sources"]) == (3, 2, 1)
    assert [(p["platform"], p["open_jobs"]) for p in stats["platforms"]] == [
        ("greenhouse", 2),
        ("niceboard", 1),
    ]
    assert stats["recent_failures"][0]["error"] == "HTTP 503"


async def test_openapi_is_documented(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    for path in ("/jobs", "/jobs/{job_id}", "/sources", "/stats", "/health"):
        operation = spec["paths"][path]["get"]
        assert operation["summary"], path
        assert operation["description"], path
        for param in operation.get("parameters", []):
            where = f"{path} {param['name']}"
            assert param.get("description"), where
            assert param.get("examples") or param["schema"].get("examples"), where


async def test_search_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    html = (await client.get("/", params={"q": "python", "salary_min": "", "page": "x"})).text
    assert "1 opening" in html
    assert 'href="https://boards.greenhouse.io/acme/jobs/1#app"' in html  # apply link
    assert 'href="https://jobs.example/job/senior-python"' in html  # second source
    assert "Product Designer" not in html

    html = (await client.get("/", params={"size": "1", "location": "Austin"})).text
    assert "Product Designer" in html
    assert "Senior Python Engineer" not in html


async def test_search_page_link_labels(client: httpx.AsyncClient, session: AsyncSession) -> None:
    """Separate posting/apply links when they differ, one "posting · apply" link otherwise."""
    session.add(Source(platform="greenhouse", board_identifier="x", base_url="https://x", id=1))
    await session.flush()
    url = "https://job-boards.greenhouse.io/x/jobs/1"
    session.add_all(
        [
            Job(source_id=1, external_id="1", title="Same", source_url=url, apply_url=url,
                content_hash="a"),
            Job(source_id=1, external_id="2", title="Different", source_url=url + "2",
                apply_url=url + "2#app", content_hash="b"),
        ]
    )  # fmt: skip
    await session.commit()
    html = (await client.get("/")).text
    assert (
        html.count(f'href="{url}" rel="noopener nofollow" target="_blank">posting · apply</a>') == 1
    )
    assert f'href="{url}2" rel="noopener nofollow" target="_blank">posting</a>' in html
    assert f'href="{url}2#app" rel="noopener nofollow" target="_blank">apply</a>' in html


async def test_search_page_drops_unsafe_links(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    session.add(Source(platform="fake", board_identifier="x", base_url="https://x", id=1))
    await session.flush()
    session.add(
        Job(
            source_id=1,
            external_id="1",
            title="<b>Engineer</b>",
            source_url="javascript:alert(1)",
            content_hash="a",
        )
    )
    await session.commit()
    html = (await client.get("/")).text
    assert "javascript:" not in html
    assert "&lt;b&gt;Engineer&lt;/b&gt;" in html
