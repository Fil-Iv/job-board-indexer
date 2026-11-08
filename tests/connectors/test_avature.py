"""Avature connector (portal sitemap, checked against the search total, + HTML job pages).

Fixtures (captured 2026-11-08; <script>, <style> and <svg> removed):
    sitemap_index.xml          GET bloomberg.avature.net/careers/sitemap_index.xml (verbatim)
    sitemap.xml                GET .../careers/sitemap.xml (1 content page + 3 of 323 jobs)
    searchjobs.html            GET .../careers/SearchJobs/?jobOffset=0 ("323 results")
    bloomberg_job_46004.html   GET .../careers/JobDetail/Bloomberg-Intelligence-Banks-Equity-
                               Research-Associate/46004 (labelled fields)
    uclahealth_job_33432.html  GET uclahealth.avature.net/careers/JobDetail/Behavioral-Health-
                               Psychiatric-Professional-100-Limited/33432 (one-line
                               "Work Location: ..." field, Posted Date, Employment Type)
"""

from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.avature import AvatureConnector
from jobindex.connectors.base import ConnectorError, RawJob
from tests.connectors.jsonld_helpers import crawl_fixture, live_check
from tests.fakes import load_fixture, mock_http

BOARD = "bloomberg.avature.net/careers"
B = "bloomberg.avature.net/careers"
JOB = "/JobDetail/Bloomberg-Intelligence-Banks-Equity-Research-Associate/46004"


def _routes(total: int) -> dict[str, httpx.Response]:
    search = load_fixture("avature", "searchjobs.html").replace(
        'aria-label="323 results"', f'aria-label="{total} results"'
    )
    return {
        f"{B}/sitemap_index.xml": httpx.Response(
            200, text=load_fixture("avature", "sitemap_index.xml")
        ),
        f"{B}/sitemap.xml": httpx.Response(200, text=load_fixture("avature", "sitemap.xml")),
        f"{B}/SearchJobs/": httpx.Response(200, text=search),
        f"{B}{JOB}": httpx.Response(200, text=load_fixture("avature", "bloomberg_job_46004.html")),
    }


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://bloomberg.avature.net/careers/SearchJobs", BOARD),
        (f"https://bloomberg.avature.net/careers{JOB}", BOARD),
        ("https://uclahealth.avature.net/careers", "uclahealth.avature.net/careers"),
        ("https://bloomberg.avature.net/", None),
        ("https://bloomberg.avature.net/portal/15", None),
        ("https://www.avature.net/careers", None),
        ("https://example.com/careers", None),
    ],
)
def test_detect(url: str, board: str | None) -> None:
    assert AvatureConnector.detect(url) == board


async def test_sitemap_and_job_page() -> None:
    stubs, details = await crawl_fixture(AvatureConnector, BOARD, _routes(total=3))
    # the content page (AgentCreate) is skipped
    assert sorted(s.external_id for s in stubs) == ["46004", "46022", "46044"]
    assert all(s.list_version and s.list_version.startswith("2026-") for s in stubs)

    job = details["46004"].normalized()
    assert job.title == "Bloomberg Intelligence - Banks Equity Research Associate"
    assert job.company == "Bloomberg"  # <title> ... - 46004 - Bloomberg
    assert job.location_raw == "New York"
    assert "Bloomberg Intelligence (BI) delivers" in (job.description_text or "")
    assert job.salary_min is not None  # "Salary Range = 100,000 - 120,000 USD Annual"
    assert job.expires_at is None


async def test_sitemap_with_fewer_jobs_than_the_board_fails() -> None:
    http, _ = mock_http(_routes(total=323))  # the real total; the fixture sitemap has 3
    async with http:
        with pytest.raises(ConnectorError, match="sitemap lists 3 jobs but the board says 323"):
            await AvatureConnector(http).fetch_jobs(BOARD)


async def test_inline_label_fields() -> None:
    host = "uclahealth.avature.net"
    path = "/careers/JobDetail/Behavioral-Health-Psychiatric-Professional-100-Limited/33432"
    routes = {
        f"{host}{path}": httpx.Response(
            200, text=load_fixture("avature", "uclahealth_job_33432.html")
        )
    }
    http, _ = mock_http(routes)
    stub = RawJob(external_id="33432", title="", source_url=f"https://{host}{path}")
    async with http:
        raw = await AvatureConnector(http).fetch_job_detail(f"{host}/careers", stub)
    assert raw is not None
    job = raw.normalized()
    assert job.title == "Behavioral Health Psychiatric Professional (100% Limited)"
    assert job.company == "UCLA Health"
    assert job.location_raw == "Los Angeles, CA, USA"  # "Work Location: Los Angeles, CA, USA"
    assert (job.city, job.region, job.country) == ("Los Angeles", "CA", "US")
    assert job.posted_at == datetime(2026, 11, 6, tzinfo=UTC)
    assert "Department of Medicine" in (job.description_text or "")
    assert "Work Location" not in (job.description_text or "")
    assert job.apply_url == "https://uclahealth.avature.net/careers/ApplicationMethods?jobId=33432"


@pytest.mark.live
@pytest.mark.parametrize("board", [BOARD, "uclahealth.avature.net/careers"])
async def test_live(board: str) -> None:
    await live_check(AvatureConnector, board)
