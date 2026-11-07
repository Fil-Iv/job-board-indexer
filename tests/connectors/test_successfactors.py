"""SAP SuccessFactors RMK connector (generic sitemap + microdata JobPosting).

Fixtures (captured 2026-11-07, trimmed):
    airindia_sitemap.xml          GET https://careers.airindia.com/sitemap.xml (6 of 13)
    airindia_job_52901044.html    GET https://careers.airindia.com/job/Gurugram-A320-Type-Rated-...-Guru/52901044/
                                  (addressCountry "No")
    yash_job_1368697366.html      GET https://careers.yash.com/job/UI-Specialist-Frontend-Job/1368697366/
"""

from datetime import UTC, datetime

import pytest

from jobindex.connectors.successfactors import SuccessFactorsConnector
from tests.connectors.jsonld_helpers import (
    crawl_fixture,
    fixture_routes,
    live_check,
    sitemap_route,
)
from tests.fakes import load_fixture

AI = "careers.airindia.com"
AI_JOB = (
    "/job/Gurugram-A320-Type-Rated-Co-Pilots-(First-Officer-Senior-First-Officer)-Guru/52901044/"
)


def test_detect_from_page() -> None:
    assert SuccessFactorsConnector.detect(f"https://{AI}/") is None  # custom domains only
    page = load_fixture("successfactors", "yash_job_1368697366.html")
    assert SuccessFactorsConnector.detect_in_html(page) == {"careers.yash.com": 1}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "successfactors",
        {
            f"{AI}/sitemap.xml": "airindia_sitemap.xml",
            f"{AI}{AI_JOB}": "airindia_job_52901044.html",
        },
    )
    stubs, details = await crawl_fixture(SuccessFactorsConnector, AI, routes)
    assert {"52901044", "52871144", "59764444"} <= {s.external_id for s in stubs}
    job = details["52901044"].normalized()
    assert job.company == "Air India Careers"
    assert (job.location_raw, job.country) == ("Gurugram, Guru", None)  # "No" dropped


async def test_java_dates() -> None:
    host, path = "careers.yash.com", "/job/UI-Specialist-Frontend-Job/1368697366/"
    routes = {
        **fixture_routes("successfactors", {f"{host}{path}": "yash_job_1368697366.html"}),
        **sitemap_route(f"https://{host}/sitemap.xml", f"https://{host}{path}"),
    }
    _, details = await crawl_fixture(SuccessFactorsConnector, host, routes)
    job = details["1368697366"].normalized()
    assert job.title == "UI Specialist - Frontend Job"
    assert job.posted_at == datetime(2026, 10, 12, tzinfo=UTC)
    assert job.expires_at == datetime(2026, 11, 30, 18, 30, tzinfo=UTC)
    assert (job.city, job.region, job.country) == ("Bangalore", "KA", "IN")


@pytest.mark.live
@pytest.mark.parametrize("board", ["careers.yash.com", AI, "careers.nttdata-solutions.com"])
async def test_live(board: str) -> None:
    await live_check(SuccessFactorsConnector, board)
