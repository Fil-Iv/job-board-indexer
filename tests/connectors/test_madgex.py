"""Wiley Madgex connector (paged HTML listing + JSON-LD job pages, custom domains).

Fixtures (captured 2026-11-08; <style>, <svg> and non-JSON-LD <script> removed):
    sciencecareers_page1.html  GET https://jobs.sciencecareers.org/jobs/     (20 jobs)
    sciencecareers_page2.html  GET https://jobs.sciencecareers.org/jobs/2/   (20 jobs)
                               In both, "Found 541 jobs" was changed to "Found 40 jobs" so
                               the two saved pages form a complete board.
    sciencecareers_job_681144.html  GET https://jobs.sciencecareers.org/job/681144/tenure-
                               track-biological-chemical-basis-of-urban-environmental-health/
    the_job_417454.html        GET https://www.timeshighereducation.com/unijobs/listing/
                               417454/phd-position-on-arctic-energy-budget-diagnostics-at-vinar/
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.madgex import MadgexConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture, mock_http

BOARD = "jobs.sciencecareers.org/jobs"
THE = "www.timeshighereducation.com/unijobs/listings"
JOB = (
    "jobs.sciencecareers.org/job/681144/"
    "tenure-track-biological-chemical-basis-of-urban-environmental-health/"
)


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://jobs.sciencecareers.org/jobs/", BOARD),
        ("https://jobs.sciencecareers.org/jobs/3/", BOARD),
        ("https://jobs.sciencecareers.org/job/680988/x/", BOARD),
        ("https://jobs.sciencecareers.org/", BOARD),
        ("https://www.timeshighereducation.com/unijobs/listings/", THE),
        ("https://www.timeshighereducation.com/unijobs/listing/417454/phd/", THE),
    ],
)
def test_board_from_page_url(url: str, board: str) -> None:
    assert MadgexConnector.board_from_page_url(url) == board


def test_detected_from_page_html() -> None:
    assert MadgexConnector.detect("https://jobs.sciencecareers.org/jobs/") is None
    page = load_fixture("madgex", "sciencecareers_page1.html")
    assert set(MadgexConnector.detect_in_html(page)) == {BOARD}  # canonical URL
    job = load_fixture("madgex", "the_job_417454.html")
    assert set(MadgexConnector.detect_in_html(job)) == {THE}


def test_job_id_belongs_to_board() -> None:
    c = MadgexConnector(None)
    assert c.job_id(f"https://{JOB}", BOARD) == "681144"
    assert c.job_id("https://jobs.sciencecareers.org/jobs/2/", BOARD) is None
    assert c.job_id("https://other.example/job/680988/x/", BOARD) is None
    the_job = "https://www.timeshighereducation.com/unijobs/listing/417454/phd/"
    assert c.job_id(the_job, THE) == "417454"
    assert c.job_id("https://www.timeshighereducation.com/listing/417454/phd/", THE) is None


def _listing_routes() -> dict[str, httpx.Response]:
    return fixture_routes(
        "madgex",
        {
            "jobs.sciencecareers.org/jobs/": "sciencecareers_page1.html",
            "jobs.sciencecareers.org/jobs/2/": "sciencecareers_page2.html",
            JOB: "sciencecareers_job_681144.html",
        },
    )


async def test_listing_pages_and_job_page() -> None:
    stubs, details = await crawl_fixture(MadgexConnector, BOARD, _listing_routes())
    assert len(stubs) == 40
    assert all(s.source_url and "LinkSource" not in s.source_url for s in stubs)
    job = details["681144"].normalized()
    assert job.title
    assert job.company
    assert job.posted_at is not None
    assert job.description_text
    assert job.source_url == f"https://{JOB}"
    assert job.apply_url


async def test_job_page_with_salary_and_location() -> None:
    routes = fixture_routes(
        "madgex",
        {
            "www.timeshighereducation.com/unijobs/listing/417454/phd/": "the_job_417454.html",
        },
    )
    http, _ = mock_http(routes)
    async with http:
        stub = RawJob(
            external_id="417454",
            title="",
            source_url="https://www.timeshighereducation.com/unijobs/listing/417454/phd/",
            list_version="v1",
        )
        detail = await MadgexConnector(http).fetch_job_detail(THE, stub)
    assert detail is not None
    job = detail.normalized()
    assert job.title == "PhD Position on Arctic Energy Budget Diagnostics at VINAR"
    assert job.company == "UNIVERSITY OF VIENNA"
    assert job.country == "AT"
    assert job.posted_at is not None


async def test_missing_page_fails_the_crawl() -> None:
    routes = _listing_routes()
    del routes["jobs.sciencecareers.org/jobs/2/"]  # page 2 answers 404
    with pytest.raises(Exception, match="404"):
        await crawl_fixture(MadgexConnector, BOARD, routes)


async def test_fewer_jobs_than_the_total_fails() -> None:
    page1 = load_fixture("madgex", "sciencecareers_page1.html").replace(
        "Found 40 jobs", "Found 41 jobs"
    )
    routes = _listing_routes()
    routes["jobs.sciencecareers.org/jobs/"] = httpx.Response(200, text=page1)
    with pytest.raises(ConnectorError, match="lists 41 jobs but 40"):
        await crawl_fixture(MadgexConnector, BOARD, routes)


async def test_page_without_total_fails() -> None:
    routes = {"jobs.sciencecareers.org/jobs/": httpx.Response(200, text="<html>hi</html>")}
    with pytest.raises(ConnectorError, match="no job total"):
        await crawl_fixture(MadgexConnector, BOARD, routes)


@pytest.mark.live
@pytest.mark.parametrize("board", [BOARD, THE])
async def test_live(board: str) -> None:
    await live_check(MadgexConnector, board)
