"""SmartJobBoard connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml    GET https://jobsboard.edsurge.com/sitemap.xml (6 of 181 urls)
    job_1212.html  GET https://jobsboard.edsurge.com/job/1212/account-executive/
"""

from decimal import Decimal

import pytest

from jobindex.connectors.smartjobboard import SmartJobBoardConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "jobsboard.edsurge.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.mysmartjobboard.com/job/12/engineer/", "acme.mysmartjobboard.com"),
        ("https://www.smartjobboard.com/", None),
        ("https://jobs.alanet.org/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert SmartJobBoardConnector.detect(url) == expected


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("smartjobboard", "job_1212.html")
    assert SmartJobBoardConnector.detect_in_html(page) == {"jobsboard.edsurge.com": 2}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "smartjobboard",
        {
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}/job/1212/account-executive/": "job_1212.html",
        },
    )
    stubs, details = await crawl_fixture(SmartJobBoardConnector, BOARD, routes)
    assert [s.external_id for s in stubs] == ["1212", "1270", "1271"]
    job = details["1212"].normalized()
    assert (job.title, job.company, job.city) == ("Account Executive", "CodeHS", "Chicago")
    assert (job.salary_min, job.salary_max, job.salary_period) == (
        Decimal(85000),
        Decimal(115000),
        "year",
    )


@pytest.mark.live
@pytest.mark.parametrize("board", ["jobsboard.edsurge.com", "jobs.alanet.org"])
async def test_live(board: str) -> None:
    await live_check(SmartJobBoardConnector, board)
