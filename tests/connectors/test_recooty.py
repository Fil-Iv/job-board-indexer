"""Recooty connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml              GET https://jobs.recooty.com/sitemap.xml (6 of 2305 urls, all
                             tenants, no <lastmod>)
    discovery_job_di101.html GET https://jobs.recooty.com/discovery-care-group/health-care-assistant-margate-days-nights-di101
"""

import pytest

from jobindex.connectors.recooty import RecootyConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check

H = "jobs.recooty.com"
JOB = "/discovery-care-group/health-care-assistant-margate-days-nights-di101"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.recooty.com/discovery-care-group", "discovery-care-group"),
        (f"https://jobs.recooty.com{JOB}", "discovery-care-group"),
        ("https://jobs.recooty.com/", None),
        ("https://jobs.recooty.com/sitemap.xml", None),
        ("https://recooty.com/pricing", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert RecootyConnector.detect(url) == expected


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "recooty", {f"{H}/sitemap.xml": "sitemap.xml", f"{H}{JOB}": "discovery_job_di101.html"}
    )
    stubs, details = await crawl_fixture(RecootyConnector, "discovery-care-group", routes)
    assert [s.external_id for s in stubs] == ["di101", "di105", "di110"]  # not provanic's
    assert all((s.list_version or "").startswith("refresh:") for s in stubs)
    job = details["di101"].normalized()
    assert job.title == "Health Care Assistant (Margate, Days/Nights) - WI"
    assert (job.company, job.city, job.country) == ("Discovery Care Group", "Broadstairs", "GB")


@pytest.mark.live
@pytest.mark.parametrize("slug", ["discovery-care-group", "gibbons-group"])
async def test_live(slug: str) -> None:
    await live_check(RecootyConnector, slug)
