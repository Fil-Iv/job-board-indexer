"""ejobsitesoftware.com connector (rss/job.php + JSON-LD job pages).

Fixtures (captured 2026-11-11 from connectagri.com; e-mail addresses replaced by
hr@example.com):
    connectagri_feed.xml      GET /rss/job.php (3 of the 10 items kept)
    connectagri_job_129.html  GET /129/Irrigation-Design-Engineer.html
"""

import re

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.ejobsitesoftware import FEED_CAP, EjobsiteSoftwareConnector
from jobindex.http import HttpClient
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes
from tests.fakes import load_fixture, mock_http

BOARD = "connectagri.com"
JOB = "/129/Irrigation-Design-Engineer.html"


def _routes(**extra: httpx.Response) -> dict[str, httpx.Response]:
    routes = fixture_routes(
        "ejobsitesoftware",
        {
            f"{BOARD}/rss/job.php": "connectagri_feed.xml",
            f"{BOARD}{JOB}": "connectagri_job_129.html",
        },
    )
    routes.update({f"{BOARD}{path}": response for path, response in extra.items()})
    return routes


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("ejobsitesoftware", "connectagri_job_129.html")
    found = EjobsiteSoftwareConnector.detect_in_html(page, f"https://{BOARD}{JOB}")
    assert set(found) == {BOARD}


def test_urls_alone_are_not_detected() -> None:
    assert EjobsiteSoftwareConnector.detect(f"https://{BOARD}{JOB}") is None


async def test_feed_and_job_page() -> None:
    stubs, details = await crawl_fixture(EjobsiteSoftwareConnector, BOARD, _routes())
    assert sorted(s.external_id for s in stubs) == ["119", "125", "129"]

    job = details["129"].normalized()
    assert job.title == "Irrigation Design Engineer"
    assert job.company == "Agrinext Consultancy"
    assert job.city == "Pune"
    assert job.country == "IN"
    assert job.employment_type == "full_time"
    assert job.posted_at is not None
    assert job.expires_at is not None  # validThrough 2026-12-07
    assert job.description_text
    assert "Irrigation" in job.description_text


async def test_job_page_5xx_keeps_the_feed_data() -> None:
    # jobqlick.com: a few job pages answer 500 every time; the job is still listed.
    routes = _routes(**{"/125/HR-Executive.html": httpx.Response(500)})
    _, details = await crawl_fixture(EjobsiteSoftwareConnector, BOARD, routes)
    job = details["125"]
    assert job.title == "HR Executive"
    assert job.description_html
    assert job.expires_at is None  # open: it is in the complete list
    assert job.source_url == "https://connectagri.com/125/HR-Executive.html"


async def test_job_page_4xx_is_not_masked() -> None:
    routes = _routes(**{"/125/HR-Executive.html": httpx.Response(404)})
    _, details = await crawl_fixture(EjobsiteSoftwareConnector, BOARD, routes)
    assert details["125"].expires_at is not None  # 404 = closed, as for every board


async def test_full_feed_is_refused() -> None:
    feed = load_fixture("ejobsitesoftware", "connectagri_feed.xml")
    item = re.search(r"<item>.*?</item>", feed, re.S)
    assert item is not None
    items = "".join(item.group(0).replace("/129/", f"/{n}/") for n in range(1000, 1000 + FEED_CAP))
    capped = feed[: feed.index("<item>")] + items + "</channel></rss>"
    http, _ = mock_http({f"{BOARD}/rss/job.php": httpx.Response(200, text=capped)})
    async with http:
        with pytest.raises(ConnectorError, match="cap"):
            await EjobsiteSoftwareConnector(http).fetch_jobs(BOARD)


async def test_not_a_feed_is_an_error() -> None:
    http, _ = mock_http({f"{BOARD}/rss/job.php": httpx.Response(200, text="<html></html>")})
    async with http:
        with pytest.raises(ConnectorError, match="not an RSS feed"):
            await EjobsiteSoftwareConnector(http).fetch_jobs(BOARD)


@pytest.mark.live
@pytest.mark.parametrize("board", ["connectagri.com", "jobqlick.com"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = EjobsiteSoftwareConnector(http)
        stubs = await connector.fetch_jobs(board)
        assert 0 < len(stubs) < FEED_CAP
        sample = [await connector.fetch_job_detail(board, s) for s in stubs[:3]]
    assert all(job is not None and job.title and job.description_html for job in sample)
