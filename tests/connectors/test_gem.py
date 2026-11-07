"""Gem connector.

Fixtures (captured 2026-11-07):
    nominal_job_posts.json          2 of 42 posts from
                                    GET https://api.gem.com/job_board/v0/nominal/job_posts/
                                    (in-office, 4 offices)
    function_health_job_posts.json  1 of 33 posts from
                                    GET https://api.gem.com/job_board/v0/function-health/job_posts/
                                    (remote)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.gem import GemConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import FetchError, HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http

POST = "37c9854b-f1ea-4da6-a3f5-51e64923ae08"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.gem.com/nominal", "nominal"),
        ("https://jobs.gem.com/nominal/", "nominal"),
        (f"https://jobs.gem.com/nominal/{POST}", "nominal"),
        ("https://jobs.gem.com/Function-Health?department=eng", "function-health"),
        ("https://api.gem.com/job_board/v0/nominal/job_posts/", "nominal"),
        ("https://jobs.gem.com/", None),
        ("https://www.gem.com/careers", None),
        ("https://api.gem.com/v0/users", None),
        ("https://www.nominal.io/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert GemConnector.detect(url) == expected


async def test_custom_domain_link_resolves() -> None:
    page = f'<a href="https://jobs.gem.com/nominal/{POST}">Mission Operations Engineer</a>'
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("gem", "nominal")


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http(
        {
            "api.gem.com/job_board/v0/nominal/job_posts/": httpx.Response(
                200, text=load_fixture("gem", "nominal_job_posts.json")
            ),
            "api.gem.com/job_board/v0/function-health/job_posts/": httpx.Response(
                200, text=load_fixture("gem", "function_health_job_posts.json")
            ),
        }
    )
    async with http:
        mission, _ = await GemConnector(http).fetch_jobs("nominal")
        (designer,) = await GemConnector(http).fetch_jobs("function-health")
    assert (mission.external_id, mission.title) == (POST, "Mission Operations Engineer")
    assert mission.remote_type is RemoteType.ONSITE  # "in_office"
    assert mission.employment_type == "full_time"
    assert mission.location_raw is not None
    assert mission.location_raw.startswith("Los Angeles, United States; New York, United States")
    assert mission.description_html
    assert mission.description_text
    assert mission.description_text.startswith("About Nominal")
    assert mission.posted_at is not None
    assert mission.posted_at.isoformat() == "2024-03-06T17:24:22+00:00"
    assert mission.source_url == f"https://jobs.gem.com/nominal/{POST}"
    assert designer.remote_type is RemoteType.REMOTE


async def test_unknown_board_and_bad_payload() -> None:
    http, _ = mock_http(
        {"api.gem.com/job_board/v0/x/job_posts/": httpx.Response(200, json={"error": 1})}
    )
    async with http:
        with pytest.raises(FetchError, match="404"):
            await GemConnector(http).fetch_jobs("nosuch")
        with pytest.raises(ConnectorError):
            await GemConnector(http).fetch_jobs("x")


@pytest.mark.live
@pytest.mark.parametrize("slug", ["nominal", "function-health"])
async def test_live(slug: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await GemConnector(http).fetch_jobs(slug)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
