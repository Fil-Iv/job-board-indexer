"""CareerSiteCloud connector (paged, counted job list + JSON-LD job pages).

Fixtures (captured 2026-11-11 from wpp.careersitecloud.com; non-JSON-LD <script>,
<style>, <svg> and <link> removed):
    pep.html       GET /pep      (view with 8 jobs, one page)
    akqa_0.html    GET /akqa     (view with 26 jobs: 20 here, pager link "2" -> /akqa/1)
    akqa_1.html    GET /akqa/1   (the other 6)
    search_0.html  GET /search   (all 2,180 jobs; first page of 20)
    job.html       GET /job-6ae5d87ffa2f6b3f6ba2e7f3-account-coordinator-new-york-new-york
"""

import re
from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.careersitecloud import CareerSiteCloudConnector
from jobindex.http import FetchError
from tests.connectors.jsonld_helpers import crawl_fixture, live_check
from tests.fakes import load_fixture, mock_http

HOST = "wpp.careersitecloud.com"
JOB = "/job-6ae5d87ffa2f6b3f6ba2e7f3-account-coordinator-new-york-new-york"


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("careersitecloud", name))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}/", HOST),
        (f"https://{HOST}/search?qs_search_job=page%3d1", HOST),
        (f"https://{HOST}{JOB}", HOST),
        (f"https://{HOST}/pep", f"{HOST}/pep"),
        (f"https://{HOST}/AKQA/1", f"{HOST}/akqa"),
        (f"https://{HOST}/new%20york", f"{HOST}/new york"),
        (f"https://{HOST}/about-us", HOST),
        ("https://www.careersitecloud.com/", None),
        ("https://cloudfront.careersitecloud.com/data/x.css", None),
        ("https://wpp.example.com/pep", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert CareerSiteCloudConnector.detect(url) == expected


def test_board_urls() -> None:
    assert CareerSiteCloudConnector.board_url(HOST) == f"https://{HOST}/search"
    assert CareerSiteCloudConnector.board_url(f"{HOST}/new york") == f"https://{HOST}/new%20york"


def test_custom_domain_detected_by_asset_hosts() -> None:
    page = load_fixture("careersitecloud", "pep.html").replace(HOST, "careers.example.org")
    assert "careers.example.org" in CareerSiteCloudConnector.detect_in_html(
        page, "https://careers.example.org/pep"
    )


async def test_view_one_page_and_job_page() -> None:
    routes = {f"{HOST}/pep": _page("pep.html"), f"{HOST}{JOB}": _page("job.html")}
    stubs, details = await crawl_fixture(CareerSiteCloudConnector, f"{HOST}/pep", routes)
    assert len(stubs) == 8  # = <h1 class="total-jobs">8
    job = details["6ae5d87ffa2f6b3f6ba2e7f3"].normalized()
    assert job.title == "Account Coordinator"
    assert job.company == "Pep"
    assert (job.city, job.region, job.country) == ("New York", "NY", "US")
    assert job.employment_type == "full_time"
    assert job.posted_at == datetime(2026, 10, 30, tzinfo=UTC)
    assert job.expires_at == datetime(2027, 4, 30, tzinfo=UTC)
    assert (job.description_text or "").startswith("About WPP")


async def test_view_two_pages() -> None:
    routes = {f"{HOST}/akqa": _page("akqa_0.html"), f"{HOST}/akqa/1": _page("akqa_1.html")}
    http, seen = mock_http(routes)
    async with http:
        stubs = await CareerSiteCloudConnector(http).fetch_jobs(f"{HOST}/akqa")
    assert len(stubs) == 26
    assert [r.url.path for r in seen if "/akqa" in r.url.path] == ["/akqa", "/akqa/1"]
    assert all(s.source_url and s.source_url.startswith(f"https://{HOST}/job-") for s in stubs)


def test_list_urls() -> None:
    url = CareerSiteCloudConnector.list_url
    assert url(f"{HOST}/akqa", 0) == f"https://{HOST}/akqa"
    assert url(f"{HOST}/new york", 2) == f"https://{HOST}/new%20york/2"
    # the bare /search states one job fewer than its later pages: page 0 is explicit
    assert url(HOST, 0) == f"https://{HOST}/search?qs_search_job=page%3d0"
    assert url(HOST, 108) == f"https://{HOST}/search?qs_search_job=page%3d108"


async def test_full_site_pages_are_requested_directly() -> None:
    """2,180 jobs at 20 per page = 109 pages, requested by number (the site's own pager
    stops linking after page 50). Every page here repeats page 0's jobs, so the distinct
    jobs do not add up to the total and the list is refused."""
    page0 = load_fixture("careersitecloud", "search_0.html")
    http, seen = mock_http({f"{HOST}/search": httpx.Response(200, text=page0)})
    async with http:
        with pytest.raises(ConnectorError, match="listed on two pages"):
            await CareerSiteCloudConnector(http).fetch_jobs(HOST)
    pages = [r.url.params.get("qs_search_job") for r in seen if r.url.path == "/search"]
    assert pages == ["page=0", "page=1"]


async def test_full_site_page_count() -> None:
    """2,180 jobs at 20 per page: pages 0..108 are requested by number (the site's own
    pager stops linking after page 50)."""
    page0 = load_fixture("careersitecloud", "search_0.html")
    ids = iter(range(10**6))

    def fresh_ids(request: httpx.Request) -> httpx.Response:
        # every page lists 20 jobs not seen before, as a stable list would
        html = re.sub(r"/job-[0-9a-f]{24}", lambda _: f"/job-{next(ids):024x}", page0)
        return httpx.Response(200, text=html)

    http, seen = mock_http({f"{HOST}/search": fresh_ids})
    async with http:
        stubs = await CareerSiteCloudConnector(http).fetch_jobs(HOST)
    assert len(stubs) == 2180
    pages = [r.url.params.get("qs_search_job") for r in seen if r.url.path == "/search"]
    assert pages == [f"page={n}" for n in range(109)]


async def test_missing_page_is_refused() -> None:
    http, _ = mock_http({f"{HOST}/akqa": _page("akqa_0.html")})  # /akqa/1 -> 404
    async with http:
        with pytest.raises(FetchError, match="404"):
            await CareerSiteCloudConnector(http).fetch_jobs(f"{HOST}/akqa")


async def test_count_mismatch_is_refused() -> None:
    html = load_fixture("careersitecloud", "pep.html").replace(
        'class="total-jobs">8', 'class="total-jobs">7'
    )
    http, _ = mock_http({f"{HOST}/pep": httpx.Response(200, text=html)})
    async with http:
        with pytest.raises(ConnectorError, match="refusing a partial list"):
            await CareerSiteCloudConnector(http).fetch_jobs(f"{HOST}/pep")


async def test_page_without_count_is_refused() -> None:
    http, _ = mock_http({f"{HOST}/about": _page("job.html")})
    async with http:
        with pytest.raises(ConnectorError, match="no job count"):
            await CareerSiteCloudConnector(http).fetch_jobs(f"{HOST}/about")


@pytest.mark.live
@pytest.mark.parametrize("board", [f"{HOST}/pep", f"{HOST}/akqa"])
async def test_live(board: str) -> None:
    await live_check(CareerSiteCloudConnector, board)
