"""JobBoard.com connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-08 from jobs.acevo.org.uk):
    robots.txt           GET /robots.txt (verbatim; no Sitemap line)
    sitemap.xml          GET /sitemap.xml (verbatim, starts with a byte-order mark)
    xml-sitemaps-1.xml   GET /xml-sitemaps/1.xml (2 of 15 job pages + 1 content page)
    job.html             GET /jobs/trustees-frank-parkinson-yorkshire-trust-guiseley-leeds/
                         1530-1/ (<style> and <svg> removed)
"""

import pytest

from jobindex.connectors.jobboardcom import JobBoardComConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "jobs.acevo.org.uk"
JOB = "/jobs/trustees-frank-parkinson-yorkshire-trust-guiseley-leeds/1530-1/"


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("jobboardcom", "job.html")
    assert set(JobBoardComConnector.detect_in_html(page)) == {BOARD}


def test_urls_alone_are_not_detected() -> None:
    assert JobBoardComConnector.detect(f"https://{BOARD}{JOB}") is None


async def test_sitemap_and_job_page() -> None:
    routes = fixture_routes(
        "jobboardcom",
        {
            f"{BOARD}/robots.txt": "robots.txt",
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}/xml-sitemaps/1.xml": "xml-sitemaps-1.xml",
            f"{BOARD}{JOB}": "job.html",
        },
    )
    stubs, details = await crawl_fixture(JobBoardComConnector, BOARD, routes)
    assert sorted(s.external_id for s in stubs) == ["1530-1", "1536-1"]  # /resources/ skipped

    job = details["1530-1"].normalized()
    assert job.title == "Trustees - Frank Parkinson Yorkshire Trust"
    assert job.company == "Eastside People"
    assert (job.city, job.country) == ("Guiseley", "GB")
    assert job.posted_at is not None
    assert job.description_text


@pytest.mark.live
@pytest.mark.parametrize("board", [BOARD, "jobs.homeless.org.uk"])
async def test_live(board: str) -> None:
    await live_check(JobBoardComConnector, board)
