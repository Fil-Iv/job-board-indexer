"""HireHive connector.

Fixtures (captured 2026-11-07):
    focus_ireland_jobs.json   GET https://focus-ireland.hirehive.com/api/v1/jobs, 2 of 23
                              jobs (full time, part time); publishedJobsCount set to 2
    catalyx_jobs_take5.json   GET https://catalyx.hirehive.com/api/v1/jobs?take=5, 2 of the
                              5 jobs kept (publishedJobsCount 24, nextPage take=5&skip=5)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.hirehive import HireHiveConnector
from jobindex.http import FetchError, HttpClient
from tests.fakes import load_fixture, load_json_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://focus-ireland.hirehive.com/", "focus-ireland"),
        ("https://Catalyx.hirehive.com/senior-engineer-cork-AbC123", "catalyx"),
        ("https://www.hirehive.com/", None),
        ("https://www2.hirehive.com/sample-job-dublin-3y1UKk", None),
        ("https://hirehive.com/", None),
        ("https://www.focusireland.ie/careers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert HireHiveConnector.detect(url) == expected


async def test_fetch_jobs_from_fixture() -> None:
    body = load_fixture("hirehive", "focus_ireland_jobs.json")
    http, _ = mock_http({"focus-ireland.hirehive.com/api/v1/jobs": httpx.Response(200, text=body)})
    async with http:
        full, part = await HireHiveConnector(http).fetch_jobs("focus-ireland")
    assert (full.external_id, full.title) == ("214155", "Project Worker, Housing First, South East")
    assert (full.location_raw, full.country) == ("Tipperary", "IE")
    assert full.employment_type == "full_time"
    assert part.employment_type == "part_time"
    assert full.description_html
    assert full.description_text
    assert full.posted_at is not None
    assert full.posted_at.year == 2026
    assert full.source_url == (
        "https://focus-ireland.hirehive.com/project-worker-housing-first-south-east-tipperary-eLqcma"
    )
    assert full.normalized().city == "Tipperary"


async def test_follows_next_page_and_checks_count() -> None:
    first = load_json_fixture("hirehive", "catalyx_jobs_take5.json")
    rest = load_json_fixture("hirehive", "focus_ireland_jobs.json")["jobs"]

    def pages(count: int) -> object:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("skip") == "5":
                return httpx.Response(200, json={"jobs": rest, "publishedJobsCount": count})
            return httpx.Response(200, json={**first, "publishedJobsCount": count})

        return handle

    http, seen = mock_http({"catalyx.hirehive.com/api/v1/jobs": pages(4)})  # type: ignore[dict-item]
    async with http:
        jobs = await HireHiveConnector(http).fetch_jobs("catalyx")
    assert len(jobs) == 4
    assert [r.url.params.get("skip") for r in seen if "api" in r.url.path] == [None, "5"]

    http, _ = mock_http({"catalyx.hirehive.com/api/v1/jobs": pages(24)})  # type: ignore[dict-item]
    async with http:
        with pytest.raises(ConnectorError, match="reports 24 jobs, got 4"):
            await HireHiveConnector(http).fetch_jobs("catalyx")


async def test_unknown_company_and_bad_payload() -> None:
    elsewhere = {"jobs": [], "publishedJobsCount": 1, "nextPage": "https://evil.example/x"}
    http, _ = mock_http(
        {
            "odd.hirehive.com/api/v1/jobs": httpx.Response(200, json=[]),
            "away.hirehive.com/api/v1/jobs": httpx.Response(200, json=elsewhere),
        }
    )
    async with http:
        with pytest.raises(FetchError, match="404"):
            await HireHiveConnector(http).fetch_jobs("nosuch")
        with pytest.raises(ConnectorError, match="unexpected response"):
            await HireHiveConnector(http).fetch_jobs("odd")
        with pytest.raises(ConnectorError, match="leaves the board"):
            await HireHiveConnector(http).fetch_jobs("away")


@pytest.mark.live
@pytest.mark.parametrize("company", ["focus-ireland", "catalyx"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await HireHiveConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
