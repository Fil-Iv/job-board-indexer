"""Rival (SilkRoad) connector (paged board + JSON-LD job pages).

Fixtures (captured 2026-11-08; <style>, <link>, <svg>, non-JSON-LD <script> removed):
    socco_p1.html       GET https://jobs.silkroad.com/SOCCo/Careers?page=1  ("Page 1 of 4", 10)
    socco_p4.html       GET https://jobs.silkroad.com/SOCCo/Careers?page=4  ("Page 4 of 4", 9)
    socco_job_144.html  GET https://jobs.silkroad.com/SOCCo/Careers/jobs/144
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.rival import RivalConnector
from tests.connectors.jsonld_helpers import crawl_fixture, live_check
from tests.fakes import load_fixture, mock_http

HOST = "jobs.silkroad.com"
BOARD = "SOCCo/Careers"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.silkroad.com/SOCCo/Careers", BOARD),
        ("https://jobs.silkroad.com/SOCCo/Careers/jobs/144", BOARD),
        ("https://jobs.silkroad.com/BU/External?page=2", "BU/External"),
        ("https://jobs.silkroad.com/Content/site.css", None),
        ("https://jobs.silkroad.com/SOCCo", None),
        ("https://rival-hr.com/about-us/careers/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert RivalConnector.detect(url) == expected


def _two_pages() -> object:
    # The saved pages say "of 4"; rewritten to a 2-page board for a complete crawl.
    pages = {
        "1": load_fixture("rival", "socco_p1.html").replace("Page 1 of 4", "Page 1 of 2"),
        "2": load_fixture("rival", "socco_p4.html").replace("Page 4 of 4", "Page 2 of 2"),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages[request.url.params["page"]])

    return handle


async def test_pages_and_job_detail() -> None:
    routes = {
        f"{HOST}/SOCCo/Careers": _two_pages(),
        f"{HOST}/SOCCo/Careers/jobs/144": httpx.Response(
            200, text=load_fixture("rival", "socco_job_144.html")
        ),
    }
    stubs, details = await crawl_fixture(RivalConnector, BOARD, routes)  # type: ignore[arg-type]
    assert len(stubs) == 19
    assert stubs[0].source_url is not None
    assert stubs[0].source_url.startswith("https://jobs.silkroad.com/SOCCo/Careers/jobs/")
    job = details["144"].normalized()
    assert job.title == "Construction Supervisor I/II (HR-0393-0394)"
    assert job.company == "SOCCo"
    assert job.employment_type == "full_time"
    assert job.region == "OH"
    assert job.description_text


async def test_repeated_or_missing_page_raises() -> None:
    p1 = load_fixture("rival", "socco_p1.html")

    def repeat(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=p1)  # page 2 repeats page 1

    http, _ = mock_http({f"{HOST}/SOCCo/Careers": repeat})
    async with http:
        with pytest.raises(ConnectorError, match="page 2 of 4 adds no jobs"):
            await RivalConnector(http).fetch_jobs(BOARD)

    odd = httpx.Response(200, text="<html><body>maintenance</body></html>")
    http, _ = mock_http({f"{HOST}/X/Careers": odd})
    async with http:
        with pytest.raises(ConnectorError, match="no page counter"):
            await RivalConnector(http).fetch_jobs("X/Careers")


@pytest.mark.live
@pytest.mark.parametrize("board", ["SOCCo/Careers"])
async def test_live(board: str) -> None:
    # BU/External (the second example, ~200 jobs) is checked by `make coverage`: at the
    # site's Crawl-delay of 10 s a full list + sample takes several minutes.
    await live_check(RivalConnector, board)
