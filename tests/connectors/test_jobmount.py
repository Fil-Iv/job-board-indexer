"""JobMount connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    imdiversity_sitemap.xml      GET https://jobs.imdiversity.com/sitemap.xml (6 of 9818 urls)
    imdiversity_job_184346.html  GET https://jobs.imdiversity.com/career/184346/campus-security-...
                                 (open; says "until the position is filled")
    wacities_job_45232.html      GET https://jobnet.wacities.org/career/45232/Parks-Maintenance-...
                                 (baseSalary in cents: 10226400-12463200 "YEAR")
"""

from datetime import UTC, datetime

import pytest

from jobindex.connectors.jobmount import JobMountConnector
from tests.connectors.jsonld_helpers import (
    crawl_fixture,
    fixture_routes,
    live_check,
    sitemap_route,
)
from tests.fakes import load_fixture

IMD = "jobs.imdiversity.com"
IMD_JOB = "/career/184346/campus-security-officer-sc-short-term-california-ca-mission-viejo"


def test_detect_from_page() -> None:
    assert JobMountConnector.detect(f"https://{IMD}/") is None  # custom domains only
    page = load_fixture("jobmount", "imdiversity_job_184346.html")
    assert JobMountConnector.detect_in_html(page) == {IMD: 1}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "jobmount",
        {
            f"{IMD}/sitemap.xml": "imdiversity_sitemap.xml",
            f"{IMD}{IMD_JOB}": "imdiversity_job_184346.html",
        },
    )
    stubs, details = await crawl_fixture(JobMountConnector, IMD, routes)
    assert [s.external_id for s in stubs] == ["184346", "177701", "185152"]  # no /view_all
    job = details["184346"].normalized()
    assert job.title == "Campus Security Officer (SC - Short Term)"
    assert job.company == "South Orange County Community College District"
    assert (job.city, job.country) == ("Mission Viejo", "US")
    # "...accept applications, until the position is filled" is not a closed notice:
    # expires_at is the validThrough
    assert job.expires_at == datetime(2026, 12, 6, 7, 50, 33, tzinfo=UTC)


async def test_salary_in_cents_is_dropped() -> None:
    host, path = "jobnet.wacities.org", "/career/45232/Parks-Maintenance-Supervisor-Region-Renton"
    routes = {
        **fixture_routes("jobmount", {f"{host}{path}": "wacities_job_45232.html"}),
        **sitemap_route(f"https://{host}/sitemap.xml", f"https://{host}{path}"),
    }
    _, details = await crawl_fixture(JobMountConnector, host, routes)
    job = details["45232"].normalized()
    assert (job.title, job.company) == ("Parks Maintenance Supervisor", "City of Renton")
    assert (job.salary_min, job.salary_max) == (None, None)


@pytest.mark.live
@pytest.mark.parametrize("board", ["jobnet.wacities.org", IMD])
async def test_live(board: str) -> None:
    await live_check(JobMountConnector, board)
