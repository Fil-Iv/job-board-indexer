"""Rippling connector.

Fixtures (captured 2026-11-07):
    vulcanforms_jobs.json         GET https://api.rippling.com/platform/api/ats/v1/board/
                                  vulcanforms/jobs — 3 of 116 rows; "Senior Applications
                                  Engineer" is listed twice (two locations, one uuid)
    vulcanforms_job_cde23e66.json GET .../board/vulcanforms/jobs/cde23e66-c732-4a5f-838e-
                                  9ac2d0d80e4e (application-form questions trimmed)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.connectors.rippling import RipplingConnector
from jobindex.http import HttpClient
from tests.fakes import load_fixture, mock_http

API = "api.rippling.com/platform/api/ats/v1/board/vulcanforms/jobs"
UUID = "cde23e66-c732-4a5f-838e-9ac2d0d80e4e"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ats.rippling.com/vulcanforms/jobs", "vulcanforms"),
        (f"https://ats.rippling.com/vulcanforms/jobs/{UUID}", "vulcanforms"),
        (f"https://ats.rippling.com/vulcanforms/jobs/{UUID}/apply", "vulcanforms"),
        ("https://ats.rippling.com/en-GB/carbon-health/jobs", "carbon-health"),
        ("https://ats.rippling.com/Carbon-Health/jobs?page=2", "carbon-health"),
        (f"https://{API}", "vulcanforms"),
        (f"https://{API}/{UUID}", "vulcanforms"),
        ("https://ats.rippling.com/", None),
        ("https://www.rippling.com/careers", None),  # Rippling's own marketing site
        ("https://careers.vulcanforms.com/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert RipplingConnector.detect(url) == expected


async def test_custom_domain_link_resolves() -> None:
    page = '<a href="https://ats.rippling.com/vulcanforms/jobs">See open roles</a>'
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("rippling", "vulcanforms")


def _routes() -> dict[str, httpx.Response]:
    return {
        API: httpx.Response(200, text=load_fixture("rippling", "vulcanforms_jobs.json")),
        f"{API}/{UUID}": httpx.Response(
            200, text=load_fixture("rippling", "vulcanforms_job_cde23e66.json")
        ),
    }


async def test_fetch_jobs_groups_locations_per_uuid() -> None:
    http, _ = mock_http(_routes())
    async with http:
        jobs = await RipplingConnector(http).fetch_jobs("vulcanforms")
    assert len(jobs) == 2  # 3 rows, 2 uuids
    manager, engineer = jobs
    assert manager.external_id == UUID
    assert manager.title == "Applications Manager - ERP System"
    assert manager.location_raw == "Devens, MA"
    assert manager.source_url == f"https://ats.rippling.com/vulcanforms/jobs/{UUID}"
    assert engineer.location_raw == "Devens, MA; Remote (United States)"


async def test_fetch_job_detail() -> None:
    http, _ = mock_http(_routes())
    async with http:
        connector = RipplingConnector(http)
        manager, _ = await connector.fetch_jobs("vulcanforms")
        detailed = await connector.fetch_job_detail("vulcanforms", manager)
    assert detailed is not None
    assert detailed.company == "VulcanForms"
    assert detailed.description_html is not None
    assert detailed.employment_type == "full_time"  # "Salaried, full-time"
    assert detailed.posted_at is not None
    assert detailed.posted_at.isoformat().startswith("2026-04-10T15:51:13")
    normalized = detailed.normalized()
    assert (normalized.city, normalized.region, normalized.country) == ("Devens", "MA", "US")


async def test_closed_job_and_unknown_board() -> None:
    http, _ = mock_http({})
    async with http:
        connector = RipplingConnector(http)
        job = RipplingConnector._map_list(UUID, [{"uuid": UUID, "name": "X"}], "vulcanforms")
        assert await connector.fetch_job_detail("vulcanforms", job) is None
        with pytest.raises(Exception, match="404"):
            await connector.fetch_jobs("nosuch")  # unknown slug: 404, never "0 jobs"


async def test_unexpected_payload_is_refused() -> None:
    http, _ = mock_http({API: httpx.Response(200, json={"jobs": []})})
    async with http:
        with pytest.raises(ConnectorError, match="not a JSON array"):
            await RipplingConnector(http).fetch_jobs("vulcanforms")


@pytest.mark.live
@pytest.mark.parametrize("slug", ["vulcanforms", "carbon-health"])
async def test_live(slug: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = RipplingConnector(http)
        jobs = await connector.fetch_jobs(slug)
        assert len(jobs) > 0
        detailed = await connector.fetch_job_detail(slug, jobs[0])
    assert len({j.external_id for j in jobs}) == len(jobs)
    assert detailed is not None
    assert detailed.description_html
