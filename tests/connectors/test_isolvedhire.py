"""iSolved Hire connector (ApplicantPro software, generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    isolved_job_site_map.xml  GET https://isolved.isolvedhire.com/job_site_map.xml (6 of 36)
    isolved_job_1790061.html  GET https://isolved.isolvedhire.com/jobs/1790061.html
"""

import pytest

from jobindex.connectors.isolvedhire import ISolvedHireConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://isolved.isolvedhire.com/jobs/", "isolved"),
        ("https://davidsonoil.isolvedhire.com/jobs/1892162.html", "davidsonoil"),
        ("https://feeds.isolvedhire.com/site_map_index.xml", None),
        ("https://isolved.applicantpro.com/jobs/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert ISolvedHireConnector.detect(url) == expected


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "isolvedhire",
        {
            "isolved.isolvedhire.com/job_site_map.xml": "isolved_job_site_map.xml",
            "isolved.isolvedhire.com/jobs/1790061.html": "isolved_job_1790061.html",
        },
    )
    stubs, details = await crawl_fixture(ISolvedHireConnector, "isolved", routes)
    assert len(stubs) == 6
    job = details["1790061"].normalized()
    assert (job.title, job.company) == ("HCM Field Sales Representative", "isolved")
    assert (job.employment_type, job.country) == ("full_time", "US")
    assert job.salary_min is None  # empty baseSalary value
    assert stubs[3].list_version == "2026-06-08"


@pytest.mark.live
@pytest.mark.parametrize("tenant", ["isolved", "davidsonoil"])
async def test_live(tenant: str) -> None:
    await live_check(ISolvedHireConnector, tenant)
