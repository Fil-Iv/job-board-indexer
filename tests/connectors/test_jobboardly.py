"""Jobboardly connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml        GET https://www.motorsport.jobs/sitemap.xml (6 of 108 urls)
    job_8239cd39.html  GET https://www.motorsport.jobs/jobs/senior-race-engineer-gt3-m-f-d-8239cd39
"""

import pytest

from jobindex.connectors.jobboardly import JobboardlyConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "www.motorsport.jobs"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.jobboardly.com/jobs/engineer-8239cd39", "acme.jobboardly.com"),
        ("https://cdn.jobboardly.com/app.js", None),
        ("https://assets.jobboardly.com/x.css", None),
        ("https://www.motorsport.jobs/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JobboardlyConnector.detect(url) == expected


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("jobboardly", "job_8239cd39.html")
    assert JobboardlyConnector.detect_in_html(page) == {"www.motorsport.jobs": 2}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "jobboardly",
        {
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}/jobs/senior-race-engineer-gt3-m-f-d-8239cd39": "job_8239cd39.html",
        },
    )
    stubs, details = await crawl_fixture(JobboardlyConnector, BOARD, routes)
    assert [s.external_id for s in stubs] == ["8239cd39", "930f025d", "12fd8df0"]  # not /about
    job = details["8239cd39"].normalized()
    assert (job.title, job.company) == ("(Senior) Race Engineer GT3 m/f/d", "Manthey Racing")
    assert (job.city, job.country, job.employment_type) == ("Meuspath", "DE", "full_time")
    assert stubs[0].list_version == "2026-10-20T02:36:43-05:00"


@pytest.mark.live
@pytest.mark.parametrize("board", ["www.motorsport.jobs", "tradiejobs.com.au"])
async def test_live(board: str) -> None:
    await live_check(JobboardlyConnector, board)
