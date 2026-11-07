"""HiringThing connector (generic JSON-LD pages, list from the RSS feed).

Fixtures (captured 2026-11-07):
    franklin_rss.xml         GET https://franklin-companies.hiringthing.com/api/rss.xml
                             (3 of 41 items)
    foodcorps_rss_empty.xml  GET https://foodcorps.hiringthing.com/api/rss.xml (no open jobs,
                             verbatim)
    job_1001403.html         GET https://franklin-companies.hiringthing.com/job/1001403/
                             certified-nursing-assistant-all-shifts (<style>, <link>, <svg>,
                             non-JSON-LD <script> removed)
"""

import httpx
import pytest

from jobindex.connectors.hiringthing import HiringThingConnector
from jobindex.http import FetchError
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture, mock_http

BOARD = "franklin-companies.hiringthing.com"
JOB = f"{BOARD}/job/1001403/certified-nursing-assistant-all-shifts"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://franklin-companies.hiringthing.com/", BOARD),
        (f"https://{JOB}", BOARD),
        ("https://www.hiringthing.com/", None),
        ("https://app.hiringthing.com/login", None),
        ("https://www.franklincompanies.com/careers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert HiringThingConnector.detect(url) == expected


async def test_feed_and_job_page_from_fixture() -> None:
    routes = fixture_routes(
        "hiringthing", {f"{BOARD}/api/rss.xml": "franklin_rss.xml", JOB: "job_1001403.html"}
    )
    stubs, details = await crawl_fixture(HiringThingConnector, BOARD, routes)
    assert [s.external_id for s in stubs] == ["1001403", "880359", "902694"]
    job = details["1001403"].normalized()
    assert job.title == "Certified Nursing Assistant (All Shifts)"
    assert job.company == "Franklin Companies"
    assert job.posted_at is not None
    assert job.posted_at.date().isoformat() == "2026-02-26"
    assert job.city
    assert job.description_text
    assert job.expires_at is None


async def test_empty_board_and_unknown_company() -> None:
    empty = httpx.Response(200, text=load_fixture("hiringthing", "foodcorps_rss_empty.xml"))
    http, _ = mock_http(
        {
            "foodcorps.hiringthing.com/api/rss.xml": empty,
            "nosuch.hiringthing.com/api/rss.xml": httpx.Response(403),
        }
    )
    async with http:
        assert await HiringThingConnector(http).fetch_jobs("foodcorps.hiringthing.com") == []
        with pytest.raises(FetchError, match="403"):
            await HiringThingConnector(http).fetch_jobs("nosuch.hiringthing.com")


@pytest.mark.live
@pytest.mark.parametrize(
    "board", ["franklin-companies.hiringthing.com", "friendsofthechildren-national.hiringthing.com"]
)
async def test_live(board: str) -> None:
    await live_check(HiringThingConnector, board)
