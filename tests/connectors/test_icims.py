"""iCIMS connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml     GET https://careers-eastwestbank.icims.com/sitemap.xml (6 of 229 urls)
    job_13643.html  GET https://careers-eastwestbank.icims.com/jobs/13643/senior-security-engineer/job
                    (hiringOrganization "UNAVAILABLE", two jobLocations)
"""

from decimal import Decimal

import pytest

from jobindex.connectors.icims import ICIMSConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

H = "careers-eastwestbank.icims.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://careers-eastwestbank.icims.com/jobs/search?ss=1", H),
        ("https://careers-eastwestbank.icims.com/jobs/13643/senior-security-engineer/job", H),
        ("https://uscareers-acme.icims.com/jobs/intro", "uscareers-acme.icims.com"),
        ("https://cdn02.icims.com/x.js", None),
        ("https://www.icims.com/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert ICIMSConnector.detect(url) == expected


def test_detect_in_page() -> None:
    page = load_fixture("icims", "job_13643.html")
    assert ICIMSConnector.detect_in_html(page).get(H, 0) > 0


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "icims",
        {
            f"{H}/sitemap.xml": "sitemap.xml",
            f"{H}/jobs/13643/senior-security-engineer/job": "job_13643.html",
        },
    )
    stubs, details = await crawl_fixture(ICIMSConnector, H, routes)
    assert {s.external_id for s in stubs} >= {"13643", "13595", "13210"}
    assert all(s.source_url and s.source_url.endswith("/job") for s in stubs)  # not /jobs/search
    job = details["13643"].normalized(company_fallback="East West Bank")
    assert job.company == "East West Bank"  # "UNAVAILABLE" dropped
    assert job.location_raw == "Dallas, TX, US; San Marino, CA, US"
    assert (job.salary_min, job.salary_period) == (Decimal(115000), "year")


@pytest.mark.live
@pytest.mark.parametrize("board", [H, "careers-emcorgroup.icims.com"])
async def test_live(board: str) -> None:
    await live_check(ICIMSConnector, board)
