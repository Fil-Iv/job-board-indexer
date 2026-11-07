"""Jobiqo connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml          GET https://www.lovelocaljobs.com/sitemap.xml (index, verbatim)
    sitemap_page1.xml    GET .../sitemap.xml?page=1 (6 of 2000 urls: home, blog, 3 jobs)
    sitemap_page2.xml    GET .../sitemap.xml?page=2 (2 organisation urls)
    sitemap_page3.xml    GET .../sitemap.xml?page=3 (2 organisation urls)
    job_59325.html       GET https://www.lovelocaljobs.com/job/level-3-nursery-nurse-59325
                         (keeps the inline config excerpt with POWERED_BY_LINK)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.jobiqo import JobiqoConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import Route, load_fixture

H = "www.lovelocaljobs.com"


def test_detect_from_page() -> None:
    assert JobiqoConnector.detect("https://www.lovelocaljobs.com/") is None  # needs the page
    page = load_fixture("jobiqo", "job_59325.html")
    assert JobiqoConnector.detect_in_html(page) == {H: 1}


def test_job_ids() -> None:
    connector = JobiqoConnector.__new__(JobiqoConnector)
    connector.board_hosts = set()
    uuid = "86875c44-77e1-46e3-b49c-fe8112bdbb45"
    assert connector.job_id(f"https://{H}/job/steel-fixer-59333", H) == "59333"
    assert connector.job_id(f"https://{H}/job/{uuid}/procurement-clerk", H) == uuid
    assert connector.job_id(f"https://{H}/jobs/manufacturing-0/england", H) is None
    assert connector.job_id(f"https://{H}/blog/x/university-fair-2025", H) is None


async def test_fetch_from_fixture() -> None:
    pages = {"1": "sitemap_page1.xml", "2": "sitemap_page2.xml", "3": "sitemap_page3.xml"}

    def sitemap(request: httpx.Request) -> httpx.Response:  # mock routes ignore the query
        name = pages.get(request.url.params.get("page", ""), "sitemap.xml")
        return httpx.Response(200, text=load_fixture("jobiqo", name))

    routes: dict[str, Route] = {
        **fixture_routes("jobiqo", {f"{H}/job/level-3-nursery-nurse-59325": "job_59325.html"}),
        f"{H}/sitemap.xml": sitemap,
    }
    stubs, details = await crawl_fixture(JobiqoConnector, H, routes)
    assert [s.external_id for s in stubs] == ["59325", "59333", "60003"]
    job = details["59325"].normalized()
    assert (job.title, job.company) == ("Level 3 Nursery Nurse", "Academics")
    assert (job.city, job.country, job.salary_currency) == ("Worthing", "GB", "GBP")
    assert job.salary_min is not None
    assert job.salary_min > Decimal(0)


@pytest.mark.live
@pytest.mark.parametrize("board", ["www.lovelocaljobs.com", "www.thejobnetwork.com"])
async def test_live(board: str) -> None:
    await live_check(JobiqoConnector, board)
