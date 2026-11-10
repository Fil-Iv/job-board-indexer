"""BrassRing connector (server-rendered preload JSON; sites with up to 50 jobs).

Fixtures (captured 2026-11-08 from sjobs.brassring.com; each page is ~1 MB of app code,
so only <title> and the <input id="preLoadJSON"> are kept, and its JSON is reduced to the
keys the connector reads):
    halifax_search.html       HomeWithPreLoad?partnerid=25749&siteid=5764&PageType=searchResults
                              (3 of the 36 jobs kept; JobsCount set to 3 to match)
    halifax_job_772312.html   ...&PageType=JobDetails&jobid=772312 (Jobdetails,
                              JobDetailFieldsToDisplay)
    halifax_job_gone.html     ...&PageType=JobDetails&jobid=1 (Jobdetails: null)
    fcps_search_56.html       partnerid=25103&siteid=5042&PageType=searchResults
                              (JobsCount 56; 3 of the 50 jobs kept)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, company_name_from_page
from jobindex.connectors.brassring import BrassRingConnector
from jobindex.http import HttpClient
from tests.fakes import assert_detail_fields_cover, load_fixture, mock_http

PAGE = "sjobs.brassring.com/TGnewUI/Search/Home/HomeWithPreLoad"
HALIFAX = "sjobs.brassring.com/25749/5764"


def _route(pages: dict[str, str]) -> dict[str, object]:
    """One path serves every page type; pick the fixture by PageType/jobid."""

    def handle(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        key = params.get("PageType", "") + ":" + params.get("jobid", "")
        return httpx.Response(200, text=load_fixture("brassring", pages[key]))

    return {PAGE: handle}


@pytest.mark.parametrize(
    ("url", "board"),
    [
        (
            "https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25749&siteid=5764",
            HALIFAX,
        ),
        (
            "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?PartnerId=25103"
            "&SiteId=5041&PageType=JobDetails&jobid=1485646",
            "sjobs.brassring.com/25103/5041",
        ),
        ("https://sjobs.brassring.com/TGWebHost/jobdetails.aspx?partnerid=1&siteid=2", None),
        ("https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25749", None),
        ("https://example.com/TGnewUI/Search/Home/Home?partnerid=1&siteid=2", None),
    ],
)
def test_detect(url: str, board: str | None) -> None:
    assert BrassRingConnector.detect(url) == board


async def test_list_and_detail() -> None:
    routes = _route(
        {
            "searchResults:": "halifax_search.html",
            "JobDetails:772312": "halifax_job_772312.html",
            "JobDetails:772333": "halifax_job_gone.html",
        }
    )
    http, _ = mock_http(routes)  # type: ignore[arg-type]
    async with http:
        connector = BrassRingConnector(http)
        jobs = await connector.fetch_jobs(HALIFAX)
        by_id = {j.external_id: j for j in jobs}
        detail = await connector.fetch_job_detail(HALIFAX, by_id["772312"])
        gone = await connector.fetch_job_detail(HALIFAX, by_id["772333"])
    assert sorted(by_id) == ["772173", "772312", "772333"]
    assert by_id["772312"].title == "Hostler Repairperson"
    assert by_id["772312"].posted_at is not None
    assert by_id["772312"].list_version == "v1"
    assert detail is not None
    assert detail.title == "Hostler Repairperson"
    text = detail.normalized().description_text or ""
    assert "Job Posting" in text
    assert "Halifax Regional Municipality is inviting applications" in text
    assert "12032BR" not in text  # bookkeeping fields are not description
    assert gone is None  # Jobdetails: null -> closed
    assert detail.apply_url == by_id["772312"].source_url
    assert_detail_fields_cover(BrassRingConnector, by_id["772312"], detail)


async def test_site_with_more_than_50_jobs_fails() -> None:
    http, _ = mock_http(_route({"searchResults:": "fcps_search_56.html"}))  # type: ignore[arg-type]
    async with http:
        with pytest.raises(ConnectorError, match="has 56 jobs; only the first 50"):
            await BrassRingConnector(http).fetch_jobs("sjobs.brassring.com/25103/5042")


async def test_count_mismatch_fails() -> None:
    page = load_fixture("brassring", "halifax_search.html").replace(
        "&quot;JobsCount&quot;: 3", "&quot;JobsCount&quot;: 4"
    )
    assert "JobsCount&quot;: 4" in page
    http, _ = mock_http({PAGE: httpx.Response(200, text=page)})
    async with http:
        with pytest.raises(ConnectorError, match="3 distinct jobs received but JobsCount is 4"):
            await BrassRingConnector(http).fetch_jobs(HALIFAX)


def test_company_from_page_title() -> None:
    page = load_fixture("brassring", "halifax_search.html")
    sources = BrassRingConnector.company_name_sources
    assert company_name_from_page(page, sources) == "Halifax Regional Municipality"


@pytest.mark.live
@pytest.mark.parametrize("board", [HALIFAX, "sjobs.brassring.com/25103/5041"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = BrassRingConnector(http)
        jobs = await connector.fetch_jobs(board)
        assert jobs
        detail = await connector.fetch_job_detail(board, jobs[0])
    assert detail is None or (detail.title and detail.description_html)


def test_detail_version_forces_one_refetch_of_stored_jobs() -> None:
    """Jobs stored before ``list_version`` existed are re-read once (2026-11-10 fix)."""
    assert "list_version" in BrassRingConnector.detail_cache_key
    assert {"location_raw", "apply_url"} <= set(BrassRingConnector.detail_fields)
