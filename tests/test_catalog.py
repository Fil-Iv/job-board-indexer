"""Platform catalog (jobindex/catalog.py, jobindex/data/coverage.json) and the
/coverage page (jobindex/api/coverage_page.py)."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.catalog import build_snapshot, catalog, platform_name, platform_names
from jobindex.connectors.registry import default_registry
from jobindex.models import Job, JobStatus, Source

T0 = datetime(2026, 11, 15, tzinfo=UTC)


def test_snapshot_matches_coverage_files() -> None:
    assert catalog() == build_snapshot(), "run `make coverage-snapshot`"


def test_all_78_platforms_with_a_status() -> None:
    platforms = catalog()["platforms"]
    assert len(platforms) == 78
    assert sum(p["type"] == "ats" for p in platforms) == 58
    assert sum(p["type"] == "jobboard" for p in platforms) == 20
    assert {p["status"] for p in platforms} <= {"live", "partial", "blocked", "not built"}
    by_key = {p["key"]: p for p in platforms}
    assert by_key["greenhouse"]["status"] == "live"
    blocked = by_key["paycom"]
    assert blocked["status"] == "blocked"
    evidence = blocked["blocks"][0]["evidence"]
    assert all(e["url"].startswith("https://") and e["checked_at"] for e in evidence)
    assert any(e["robots_rule"] or e["http_status"] for e in evidence)


def test_every_connector_is_in_the_catalog() -> None:
    assert not sorted(set(default_registry().platforms) - set(platform_names()))


def test_platform_names() -> None:
    assert platform_name("wpjobmanager") == "WP Job Manager"
    assert platform_name("successfactors") == "SAP SuccessFactors"
    assert platform_name("jobboardsoftware") == "jobboardsoftware.com"
    assert platform_name("unknown_key") == "unknown_key"
    assert platform_name(None) == ""


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_coverage_page(client: httpx.AsyncClient, session: AsyncSession) -> None:
    session.add_all(
        [
            Source(
                id=1,
                platform="greenhouse",
                board_identifier="acme",
                base_url="https://boards.greenhouse.io/acme",
                last_success_at=T0,
            ),
            Source(
                id=2,
                platform="lever",
                board_identifier="beta",
                base_url="https://jobs.lever.co/beta",
                consecutive_failures=3,
                last_error="FetchError: HTTP 503",
            ),
        ]
    )
    await session.flush()
    for i in range(3):
        session.add(
            Job(
                source_id=1,
                external_id=str(i),
                title="Engineer",
                status=JobStatus.OPEN if i < 2 else JobStatus.CLOSED,
                first_seen_at=T0,
                last_seen_at=T0,
                content_hash=str(i),
            )
        )
    await session.commit()

    response = await client.get("/coverage")
    assert response.status_code == 200
    assert "s-maxage=600" in response.headers["cache-control"]
    page = response.text
    assert 'aria-current="page">Coverage' in page
    assert len(re.findall(r'<li id="p-[a-z_]+" class="cov-row">', page)) == 78
    greenhouse = page[page.index('id="p-greenhouse"') :][:3000]
    assert "<b>2</b> open jobs · 1 board · last crawl" in greenhouse  # closed job not counted
    assert "2026-11-15" in greenhouse
    lever = page[page.index('id="p-lever"') :][:3000]
    assert "failing \u00d73" in lever
    paycom = page[page.index('id="p-paycom"') :][:4000]
    assert "st-blocked" in paycom
    assert "<details open>" in paycom
    assert "robots.txt <code>Disallow: /</code>" in paycom
    counts = catalog()["platforms"]
    for status in ("live", "partial", "blocked", "not built"):
        n = sum(p["status"] == status for p in counts)
        assert f'<b>{n}</b><span><i class="badge st-{status.replace(" ", "-")}">' in page
