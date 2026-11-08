"""SAP SuccessFactors RMK connector (generic sitemap + microdata JobPosting).

Fixtures (captured 2026-11-07, trimmed):
    airindia_sitemap.xml          GET https://careers.airindia.com/sitemap.xml (6 of 13)
    airindia_job_52901044.html    GET https://careers.airindia.com/job/Gurugram-A320-Type-Rated-...-Guru/52901044/
                                  (addressCountry "No")
    yash_job_1368697366.html      GET https://careers.yash.com/job/UI-Specialist-Frontend-Job/1368697366/
    ntt_job_10891.html            GET https://careers.nttdata-solutions.com/job/Budapest-Sales-
                                  Manager-SAP-Solution-BU-1117/10891-en_US/ (2026-11-08; the
                                  sitemap URL .../1234969701/ redirects here; microdata has
                                  only the description)
    ntt_job_1268057101_split_description.html
                                  GET https://careers.nttdata-solutions.com/job/Bangalore-BASIS-
                                  Senior-Consultant-560066/1268057101/ (2026-11-08; three
                                  itemprop="description" blocks: banner image, empty, text)
"""

from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import RawJob
from jobindex.connectors.successfactors import SuccessFactorsConnector, location_from_slug
from tests.connectors.jsonld_helpers import (
    crawl_fixture,
    fixture_routes,
    live_check,
    sitemap_route,
)
from tests.fakes import load_fixture, mock_http

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


@pytest.mark.parametrize(
    ("slug", "title", "location"),
    [
        (
            "Budapest-Sales-Manager-SAP-Solution-BU-1117",
            "Sales Manager SAP Solution",
            "Budapest, BU",
        ),
        (
            "Bielefeld-Executive-Assistant-&-Project-Management-Support-%28fmd%29-NW-33605",
            "Executive Assistant & Project Management Support (f/m/d)",
            "Bielefeld, NW",
        ),
        ("UI-Specialist-Frontend-Job", "UI Specialist Frontend", None),  # "-Job" suffix
        ("Engineer", "Engineer", None),
        ("Berlin-Data-Engineer", "Platform Engineer", None),  # title not in the slug
    ],
)
def test_location_from_slug(slug: str, title: str, location: str | None) -> None:
    got = location_from_slug(f"https://careers.example/job/{slug}/1/", title)
    assert got == location


async def test_template_without_title_microdata() -> None:
    url = "https://careers.nttdata-solutions.com/job/Budapest-Sales-Manager-SAP-Solution-BU-1117/1234969701/"
    page = load_fixture("successfactors", "ntt_job_10891.html")
    http, _ = mock_http({url.split("://", 1)[1]: httpx.Response(200, text=page)})
    stub = RawJob(external_id="1234969701", title="", source_url=url, list_version="v")
    async with http:
        job = await SuccessFactorsConnector(http).fetch_job_detail(
            "careers.nttdata-solutions.com", stub
        )
    assert job is not None
    assert job.expires_at is None
    job = job.normalized()
    assert job.title == "Sales Manager SAP Solution"
    assert job.location_raw == "Budapest, BU"
    assert job.description_text


async def test_description_outside_the_jobposting_scope() -> None:
    url = "https://careers.nttdata-solutions.com/job/Bangalore-BASIS-Senior-Consultant-560066/1268057101/"
    page = load_fixture("successfactors", "ntt_job_1268057101_split_description.html")
    http, _ = mock_http({url.split("://", 1)[1]: httpx.Response(200, text=page)})
    stub = RawJob(external_id="1268057101", title="", source_url=url, list_version="v")
    async with http:
        job = await SuccessFactorsConnector(http).fetch_job_detail(
            "careers.nttdata-solutions.com", stub
        )
    assert job is not None
    job = job.normalized()
    assert job.title == "BASIS - Senior Consultant"
    assert job.description_text
    assert "Walk-In Drive" in job.description_text
