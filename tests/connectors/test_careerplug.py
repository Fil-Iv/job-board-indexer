"""CareerPlug connector (server-rendered, paged job list; no descriptions).

Fixtures (captured 2026-11-11; non-JSON-LD <script>, <style>, <svg>, <link> and the
CSRF meta tags removed):
    otcs_p1.html       GET https://orangetheory-corporate-studios.otrecruit.com/jobs
                       (30 jobs, "next" -> /jobs?page=2; city-first locations)
    otcs_p2.html       GET .../jobs?page=2 (12 jobs, last page: next is a disabled span)
    fastsigns_p1.html  GET https://fastsigns-careers.careerplug.com/jobs (page 1 of 21;
                       region-first locations incl. Canadian postal codes)
    empty_board.html   GET https://stagwoods-skilled-nursing-center.careinhomesjobs.com/jobs
                       ("There are no jobs posted at this time.")
"""

from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.careerplug import CareerPlugConnector, Location, parse_location
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import Route, load_fixture, mock_http

OTCS = "orangetheory-corporate-studios.otrecruit.com"
FASTSIGNS = "fastsigns-careers.careerplug.com"


def _paged(pages: dict[str, str]) -> Route:
    """Route answering ``/jobs?page=N`` from ``{"1": fixture, "2": fixture}``."""

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        return httpx.Response(200, text=load_fixture("careerplug", pages[page]))

    return handle


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{FASTSIGNS}/jobs", FASTSIGNS),
        ("https://FastSigns-Careers.careerplug.com/jobs/3688958", FASTSIGNS),
        (f"https://{OTCS}/jobs?page=2", OTCS),
        ("https://acme.r365hire.com/jobs", "acme.r365hire.com"),
        ("https://app.careerplug.com/jobs/1", None),
        ("https://www.careerplug.com/", None),
        ("https://careerplug.com/jobs", None),
        ("https://acme.careerplug.co/jobs", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert CareerPlugConnector.detect(url) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CA-Tarzana-91356", ("Tarzana, CA 91356", "Tarzana", "CA", "US")),
        ("ON-St. Catharines-L2M 5V6", ("St. Catharines, ON L2M 5V6", "St. Catharines", "ON", "CA")),
        (
            "Ponte Vedra Beach -FL-32081-Orangetheory Fitness Nocatee",
            ("Ponte Vedra Beach, FL 32081", "Ponte Vedra Beach", "FL", "US"),
        ),
        (
            "JACKSONVILLE-FL-32224-Orangetheory Fitness Jacksonville-Hodges Point",
            ("Jacksonville, FL 32224", "Jacksonville", "FL", "US"),
        ),
        ("Remote", ("Remote", None, None, None)),
        (
            "ON-London-N6H 0B7 Fully Remote - CA",
            ("London, ON N6H 0B7 (Fully Remote)", "London", "ON", "CA", RemoteType.REMOTE),
        ),
        (
            "OH-Westerville-43081 Hybrid - US",
            ("Westerville, OH 43081 (Hybrid)", "Westerville", "OH", "US", RemoteType.HYBRID),
        ),
    ],
)
def test_parse_location(raw: str, expected: tuple[object, ...]) -> None:
    assert parse_location(raw) == Location(*expected)  # type: ignore[arg-type]


async def test_two_pages_read_twice() -> None:
    http, seen = mock_http({f"{OTCS}/jobs": _paged({"1": "otcs_p1.html", "2": "otcs_p2.html"})})
    async with http:
        jobs = await CareerPlugConnector(http).fetch_jobs(OTCS)
    assert len(jobs) == 42  # 30 + 12
    assert len({j.external_id for j in jobs}) == 42
    # a multi-page board is read twice (it must not change in between)
    assert [r.url.params.get("page", "1") for r in seen if r.url.path == "/jobs"] == [
        "1",
        "2",
        "1",
        "2",
    ]
    assert all(j.title and j.source_url for j in jobs)
    assert all(j.description_html is None for j in jobs)  # list only
    job = jobs[0]
    assert job.source_url == f"https://{OTCS}/jobs/{job.external_id}"
    assert job.country == "US"
    assert job.employment_type is not None  # this account shows "Full / Part Time"


async def test_region_first_locations_and_dates() -> None:
    page = load_fixture("careerplug", "fastsigns_p1.html")
    connector = CareerPlugConnector(mock_http({})[0])
    jobs, next_url = connector._parse_page("fastsigns-careers.careerplug.com", page, 1)
    assert len(jobs) == 30
    assert next_url == "https://fastsigns-careers.careerplug.com/jobs?page=2"
    job = next(j for j in jobs if j.external_id == "3688958")
    assert job.title == "Signs & Graphics Inside Sales Consultant"
    assert (job.location_raw, job.city, job.region, job.country) == (
        "Tarzana, CA 91356",
        "Tarzana",
        "CA",
        "US",
    )
    assert job.posted_at == datetime(2026, 11, 11, tzinfo=UTC)  # "11-11-26"
    assert all(j.posted_at for j in jobs)


async def test_list_changing_between_passes_is_refused() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        page = request.url.params.get("page", "1")
        html = load_fixture("careerplug", "otcs_p1.html" if page == "1" else "otcs_p2.html")
        if calls["n"] > 2 and page == "2":  # second pass: one job is gone
            html = html.replace('href="/jobs/', 'href="/gone/', 1)
        return httpx.Response(200, text=html)

    http, _ = mock_http({f"{OTCS}/jobs": handle})
    async with http:
        with pytest.raises(ConnectorError, match="changed while"):
            await CareerPlugConnector(http).fetch_jobs(OTCS)


async def test_empty_board() -> None:
    board = "stagwoods-skilled-nursing-center.careinhomesjobs.com"
    page = httpx.Response(200, text=load_fixture("careerplug", "empty_board.html"))
    http, _ = mock_http({f"{board}/jobs": page})
    async with http:
        assert await CareerPlugConnector(http).fetch_jobs(board) == []


async def test_unknown_account_is_an_error() -> None:
    board = "nonexistent-xyz.careerplug.com"
    routes = {
        f"{board}/jobs": httpx.Response(302, headers={"location": "https://app.careerplug.com/"}),
        "app.careerplug.com/": httpx.Response(200, text="<html>CareerPlug</html>"),
    }
    http, _ = mock_http(routes)
    async with http:
        with pytest.raises(ConnectorError, match="not a CareerPlug account"):
            await CareerPlugConnector(http).fetch_jobs(board)


async def test_page_without_list_is_an_error() -> None:
    http, _ = mock_http({f"{OTCS}/jobs": httpx.Response(200, text="<html><body>x</body></html>")})
    async with http:
        with pytest.raises(ConnectorError, match="no job list"):
            await CareerPlugConnector(http).fetch_jobs(OTCS)


@pytest.mark.live
@pytest.mark.parametrize("board", ["fastsigns-careers.careerplug.com", OTCS])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await CareerPlugConnector(http).fetch_jobs(board)
    assert jobs
    assert all(j.title and j.source_url for j in jobs)
    assert sum(1 for j in jobs if j.city) >= len(jobs) // 2
