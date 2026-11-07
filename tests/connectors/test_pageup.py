"""PageUp connector (RSS feed).

Fixtures (captured 2026-11-07):
    universal_rss.xml     GET https://careers.pageuppeople.com/851/ci/en-us/rss (2 of 144 items)
    flightcentre_rss.xml  GET https://careers.pageuppeople.com/889/usw/en/rss (1 of 53 items;
                          location "USA|Massachusetts", empty closingDate)
"""

from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.pageup import PageUpConnector
from jobindex.http import FetchError, HttpClient
from tests.fakes import load_fixture, mock_http

HOST = "careers.pageuppeople.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://careers.pageuppeople.com/851/ci/en-us/listing", "851/ci/en-us"),
        ("https://careers.pageuppeople.com/889/usw/en/listing/", "889/usw/en"),
        ("https://careers.pageuppeople.com/851/ci/en-us/job/669032", "851/ci/en-us"),
        ("https://careers.pageuppeople.com/902", None),  # landing page: portal unknown
        ("https://www.pageuppeople.com/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert PageUpConnector.detect(url) == expected


def _xml(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("pageup", name))


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http(
        {
            f"{HOST}/851/ci/en-us/rss": _xml("universal_rss.xml"),
            f"{HOST}/889/usw/en/rss": _xml("flightcentre_rss.xml"),
        }
    )
    async with http:
        coordinator, stitcher = await PageUpConnector(http).fetch_jobs("851/ci/en-us")
        (manager,) = await PageUpConnector(http).fetch_jobs("889/usw/en")
    assert (coordinator.external_id, coordinator.title) == (
        "669032",
        "Coordinator – Entertainment Events",
    )
    assert coordinator.location_raw == "Orlando"
    assert coordinator.employment_type == "full_time"  # "Regular (Full-Time)"
    assert coordinator.posted_at == datetime(2026, 11, 6, 22, 10, tzinfo=UTC)
    assert coordinator.expires_at == datetime(2026, 11, 12, 4, 55, tzinfo=UTC)
    assert coordinator.source_url == f"https://{HOST}/851/ci/en-us/job/669032"
    assert coordinator.description_html
    assert coordinator.normalized().description_text
    assert stitcher.external_id == "669028"
    assert manager.location_raw == "Massachusetts, USA"
    assert manager.expires_at is None  # empty closingDate
    assert manager.normalized().country == "US"


async def test_unknown_site_and_bad_payload() -> None:
    http, _ = mock_http(
        {
            f"{HOST}/1/x/en/rss": httpx.Response(200, text="<html>not xml"),
            f"{HOST}/2/x/en/rss": httpx.Response(200, text="<feed></feed>"),
        }
    )
    async with http:
        with pytest.raises(FetchError, match="404"):
            await PageUpConnector(http).fetch_jobs("999999/xx/en")
        with pytest.raises(ConnectorError, match="not valid XML"):
            await PageUpConnector(http).fetch_jobs("1/x/en")
        with pytest.raises(ConnectorError, match="not an RSS feed"):
            await PageUpConnector(http).fetch_jobs("2/x/en")


@pytest.mark.live
@pytest.mark.parametrize("board", ["851/ci/en-us", "889/usw/en"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await PageUpConnector(http).fetch_jobs(board)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
