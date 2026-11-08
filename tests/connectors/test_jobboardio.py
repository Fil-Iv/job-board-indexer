"""JobBoard.io connector (generic sitemap + JSON-LD, custom domains).

Fixtures (captured 2026-11-08):
    sitemap.xml            GET https://jobs.cityandstateny.com/sitemap.xml (index, verbatim;
                           the job chunk URL is XML-escaped: "&amp;start_id=")
    job_sitemap.xml        GET .../sitemaps/jobs/job_sitemap_details.xml?finish_id=640025768
                           &start_id=621269973 (board page + 3 of 16 jobs)
    job_640025768.html     GET .../jobs/640025768-waste-hauler-and-truck-driver-park-
                           maintainer-iii-at-central-park-conservancy (<style>, <svg>,
                           non-JSON-LD <script> removed; <link> kept for the fingerprint)
"""

import pytest

from jobindex.connectors.jobboardio import JobBoardIoConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "jobs.cityandstateny.com"
JOB = (
    "/jobs/640025768-waste-hauler-and-truck-driver-park-maintainer-iii-at-central-park-conservancy"
)


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("jobboardio", "job_640025768.html")
    assert JobBoardIoConnector.detect(f"https://{BOARD}/") is None  # no native host
    assert set(JobBoardIoConnector.detect_in_html(page)) == {BOARD}  # canonical + og:url


async def test_job_sitemap_chunks_and_job_page() -> None:
    routes = fixture_routes(
        "jobboardio",
        {
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}/sitemaps/jobs/job_sitemap_details.xml": "job_sitemap.xml",
            f"{BOARD}{JOB}": "job_640025768.html",
        },
    )
    stubs, details = await crawl_fixture(JobBoardIoConnector, BOARD, routes)
    assert len(stubs) == 3  # board page skipped; page/taxonomy sitemaps not followed
    job = details["640025768"].normalized()
    assert job.title
    assert job.company
    assert job.posted_at is not None
    assert job.description_text


@pytest.mark.live
@pytest.mark.parametrize("board", ["jobs.cityandstateny.com", "jobs.blooloop.com"])
async def test_live(board: str) -> None:
    await live_check(JobBoardIoConnector, board)
