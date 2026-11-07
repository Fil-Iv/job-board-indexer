"""Paylocity connector (embedded job list + HTML job pages).

Fixtures (captured 2026-11-07):
    adapthealth_board.html  GET https://recruiting.paylocity.com/recruiting/jobs/All/
                            4bcae427-e9e7-4d6d-9772-73ef70c3a278/Adapt-Health-LLC, reduced to
                            the window.pageData script with 3 of 481 jobs
    job_4649628.html        GET .../Recruiting/Jobs/Details/4649628 (AdaptHealth: "Hybrid
                            Remote", Job Type section, also has JSON-LD)
    job_4591787.html        GET .../Recruiting/Jobs/Details/4591787 (Peak Support: "Fully
                            Remote", no JSON-LD)
    job_not_found.html      GET .../Recruiting/Jobs/Details/1000001 -> /Recruiting/Jobs/JobNotFound
    (job pages: <style>, <link>, <svg> and non-JSON-LD <script> removed)
"""

from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.paylocity import PaylocityConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http

HOST = "recruiting.paylocity.com"
BOARD = "4bcae427-e9e7-4d6d-9772-73ef70c3a278"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}/recruiting/jobs/All/{BOARD}/Adapt-Health-LLC", BOARD),
        (f"https://{HOST}/Recruiting/Jobs/All/{BOARD.upper()}", BOARD),
        (f"https://{HOST}/Recruiting/Jobs/Details/4649628", None),  # job page: no board id
        (f"https://{HOST}/recruiting/jobs/All/not-a-guid", None),
        ("https://www.paylocity.com/company/careers/all-listings/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert PaylocityConnector.detect(url) == expected


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("paylocity", name))


def _stub(job_id: str) -> RawJob:
    return RawJob(
        external_id=job_id, title="x", source_url=f"https://{HOST}/Recruiting/Jobs/Details/{job_id}"
    )


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http({f"{HOST}/recruiting/jobs/All/{BOARD}": _page("adapthealth_board.html")})
    async with http:
        jobs = await PaylocityConnector(http).fetch_jobs(BOARD)
        company = await PaylocityConnector(http).fetch_company_name(BOARD)
    assert company == "AdaptHealth LLC"
    assert [j.external_id for j in jobs] == ["4649628", "4267085", "4295707"]
    first = jobs[0]
    assert (first.title, first.company) == ("Senior Financial Analyst, FP&A", "AdaptHealth LLC")
    assert first.location_raw == "Conshohocken, PA"
    assert (first.city, first.region) == ("Conshohocken", "PA")
    assert first.normalized().country == "US"  # "USA"
    assert first.posted_at is not None
    assert first.posted_at == datetime(2026, 11, 6, 22, 13, 6, tzinfo=UTC)
    assert first.source_url == f"https://{HOST}/Recruiting/Jobs/Details/4649628"
    assert first.apply_url == f"https://{HOST}/Recruiting/Jobs/Apply/4649628"
    assert first.description_html is None  # from the detail page


async def test_detail_from_html() -> None:
    http, _ = mock_http(
        {
            f"{HOST}/Recruiting/Jobs/Details/4649628": _page("job_4649628.html"),
            f"{HOST}/Recruiting/Jobs/Details/4591787": _page("job_4591787.html"),
        }
    )
    async with http:
        connector = PaylocityConnector(http)
        hybrid = await connector.fetch_job_detail(BOARD, _stub("4649628"))
        remote = await connector.fetch_job_detail(BOARD, _stub("4591787"))
    assert hybrid is not None
    assert hybrid.remote_type is RemoteType.HYBRID  # "Hybrid Remote" (IsRemote is true)
    assert hybrid.employment_type == "full_time"  # Job Type section, not in the description
    assert hybrid.description_html is not None
    assert hybrid.description_html.startswith("<h3>Description</h3>")
    assert "<h3>Job Type</h3>" not in hybrid.description_html
    assert "<h3>Requirements</h3>" in hybrid.description_html
    assert remote is not None
    assert remote.remote_type is RemoteType.REMOTE  # "Fully Remote"
    assert remote.description_html is not None
    assert "English and German-speaking markets" in remote.description_html


async def test_closed_job_and_unknown_board() -> None:
    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": f"https://{HOST}/Recruiting/Jobs/JobNotFound"}
        )

    unknown = "00000000-0000-0000-0000-000000000000"
    http, _ = mock_http(
        {
            f"{HOST}/Recruiting/Jobs/Details/1000001": not_found,
            f"{HOST}/Recruiting/Jobs/JobNotFound": _page("job_not_found.html"),
            f"{HOST}/recruiting/jobs/All/{unknown}": _page("job_not_found.html"),
            f"{HOST}/Recruiting/Jobs/Details/2": httpx.Response(200, text="<html></html>"),
        }
    )
    async with http:
        connector = PaylocityConnector(http)
        assert await connector.fetch_job_detail(BOARD, _stub("1000001")) is None
        with pytest.raises(ConnectorError, match=r"no window\.pageData"):
            await connector.fetch_jobs(unknown)
        with pytest.raises(ConnectorError, match="template changed"):
            await connector.fetch_job_detail(BOARD, _stub("2"))


@pytest.mark.live
@pytest.mark.parametrize(
    "board", ["4bcae427-e9e7-4d6d-9772-73ef70c3a278", "67c76e24-da8b-4733-a8b3-9c1fe859c5b9"]
)
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = PaylocityConnector(http)
        jobs = await connector.fetch_jobs(board)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(board, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html
