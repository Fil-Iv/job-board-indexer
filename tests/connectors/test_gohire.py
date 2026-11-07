"""GoHire connector (paged board + JSON-LD job pages).

Fixtures (captured 2026-11-07; <style>, <link>, <svg> and non-JSON-LD <script> removed):
    dexerto_p1.html          GET https://jobs.gohire.io/dexerto-de5jlhjo/  ("Page 1 of 2,
    dexerto_p2.html          GET .../dexerto-de5jlhjo/?page=2               Total 16 jobs")
    su9lyitg_p1.html         GET https://jobs.gohire.io/su9lyitg  (served at the full slug
                             axented-su9lyitg; page 1 of 2, 18 jobs)
    axented_p2.html          GET https://jobs.gohire.io/axented-su9lyitg/?page=2
    dexerto_job_287601.html  GET .../dexerto-de5jlhjo/brand-and-communications-manager-uk-
                             remote-287601/  (remote, GBP salary, validThrough in the past)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.gohire import GoHireConnector
from jobindex.models.enums import RemoteType
from tests.connectors.jsonld_helpers import crawl_fixture, live_check
from tests.fakes import load_fixture, mock_http

HOST = "jobs.gohire.io"
BOARD = "dexerto-de5jlhjo"
JOB = f"{HOST}/{BOARD}/brand-and-communications-manager-uk-remote-287601/"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.gohire.io/dexerto-de5jlhjo/", BOARD),
        ("https://jobs.gohire.io/Dexerto-de5JlhJo/x-287601/", BOARD),
        ("https://jobs.gohire.io/su9lyitg", "su9lyitg"),
        ("https://jobs.gohire.io/assets/css/jobs.css", None),
        ("https://jobs.gohire.io/", None),
        ("https://www.gohire.io/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert GoHireConnector.detect(url) == expected


def _board(*pages: str) -> object:
    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, text=load_fixture("gohire", pages[page - 1]))

    return handle


async def test_fetch_all_pages_and_job_detail() -> None:
    routes = {
        f"{HOST}/{BOARD}/": _board("dexerto_p1.html", "dexerto_p2.html"),
        JOB: httpx.Response(200, text=load_fixture("gohire", "dexerto_job_287601.html")),
    }
    stubs, details = await crawl_fixture(GoHireConnector, BOARD, routes)  # type: ignore[arg-type]
    assert len(stubs) == 16
    assert len({s.external_id for s in stubs}) == 16
    job = details["287601"].normalized()
    assert job.title == "Brand & Communications Manager (UK - Remote)"
    assert job.company == "Dexerto"
    assert job.remote_type is RemoteType.REMOTE
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_period) == (
        Decimal(65000),
        Decimal(80000),
        "GBP",
        "year",
    )
    assert job.expires_at is None  # validThrough 2026-09-12 is ignored: the job is listed
    assert job.description_text


async def test_board_addressed_by_id_pages_on_the_full_slug() -> None:
    # /su9lyitg/?page=N redirects to /axented-su9lyitg/ and loses the page number.
    def short(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": f"https://{HOST}/axented-su9lyitg/"})

    routes = {
        f"{HOST}/su9lyitg/": short,
        f"{HOST}/axented-su9lyitg/": _board("su9lyitg_p1.html", "axented_p2.html"),
    }
    http, seen = mock_http(routes)  # type: ignore[arg-type]
    async with http:
        stubs = await GoHireConnector(http).fetch_jobs("su9lyitg")
    assert len(stubs) == 18
    assert f"https://{HOST}/axented-su9lyitg/?page=2" in {str(r.url) for r in seen}
    assert all("/axented-su9lyitg/" in (s.source_url or "") for s in stubs)


async def test_missing_page_raises() -> None:
    # page 2 repeats page 1: 10 unique of 18 -> error, never a partial list
    routes = {f"{HOST}/axented-su9lyitg/": _board("su9lyitg_p1.html", "su9lyitg_p1.html")}
    http, _ = mock_http(routes)  # type: ignore[arg-type]
    async with http:
        with pytest.raises(ConnectorError, match="reports 18, got 10"):
            await GoHireConnector(http).fetch_jobs("axented-su9lyitg")
    connector = GoHireConnector(http)
    other = f"https://{HOST}/other-zz11/lead-crm-engineer-303514/"
    assert connector.job_id(f"https://{HOST}/axented-su9lyitg/lead-crm-303514/", "su9lyitg")
    assert connector.job_id(other, "su9lyitg") is None


async def test_unknown_board_and_changed_template_raise() -> None:
    http, _ = mock_http({f"{HOST}/odd-board1/": httpx.Response(200, text="<html>jobs</html>")})
    async with http:
        with pytest.raises(Exception, match="404"):
            await GoHireConnector(http).fetch_jobs("nosuch-zz99")
        with pytest.raises(ConnectorError, match="no job count"):
            await GoHireConnector(http).fetch_jobs("odd-board1")


@pytest.mark.live
@pytest.mark.parametrize("board", ["dexerto-de5jlhjo", "su9lyitg"])
async def test_live(board: str) -> None:
    await live_check(GoHireConnector, board)
