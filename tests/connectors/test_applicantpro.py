"""ApplicantPro connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    mwra_job_site_map.xml  GET https://mwra.applicantpro.com/job_site_map.xml (6 of 19 jobs)
    mwra_job_4023073.html  GET https://mwra.applicantpro.com/jobs/4023073.html (malformed
                           validThrough "2026-12-03 00:00:00T23:59"; annual pay as "HOUR")
"""

from decimal import Decimal

import pytest

from jobindex.connectors.applicantpro import ApplicantProConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mwra.applicantpro.com/jobs/", "mwra"),
        ("https://mwra.applicantpro.com/jobs/4023073.html", "mwra"),
        ("https://www.applicantpro.com/openings/mwra/jobs/4023073", "mwra"),
        ("https://ForeignAutoTruckSupply.applicantpro.com/jobs/", "foreignautotrucksupply"),
        ("https://www.applicantpro.com/", None),
        ("https://feeds.applicantpro.com/site_map_index.xml", None),
        ("https://mwra.isolvedhire.com/jobs/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert ApplicantProConnector.detect(url) == expected


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "applicantpro",
        {
            "mwra.applicantpro.com/job_site_map.xml": "mwra_job_site_map.xml",
            "mwra.applicantpro.com/jobs/4023073.html": "mwra_job_4023073.html",
        },
    )
    stubs, details = await crawl_fixture(ApplicantProConnector, "mwra", routes)
    assert len(stubs) == 6
    # listed as www.applicantpro.com/openings/...; fetched on the tenant host (no redirect)
    assert stubs[-1].source_url == "https://mwra.applicantpro.com/jobs/4023073.html"
    job = details["4023073"].normalized()
    assert job.title == "(3rd) Third Class Engineer"
    assert (job.city, job.region, job.country) == ("Winthrop", "MA", "US")
    assert (job.salary_min, job.salary_max) == (Decimal("79229.54"), Decimal("113390.04"))
    assert job.salary_period == "year"  # labelled HOUR
    assert job.expires_at is not None
    assert job.expires_at.isoformat() == "2026-12-04T00:00:00+00:00"


@pytest.mark.live
@pytest.mark.parametrize("tenant", ["mwra", "commquest"])
async def test_live(tenant: str) -> None:
    await live_check(ApplicantProConnector, tenant)
