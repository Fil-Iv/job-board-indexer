"""jobBoardHQ connector (browser-driven /search pager + JSON-LD job pages).

Fixtures (captured 2026-11-11 with jobindex.browser; the search pages are trimmed to the
parts the connector reads: the vendor stylesheet link, #jobList with its job links,
#lblLimitExceeded and the pager; logos, share buttons, abstracts, skills and pay details
removed):
    macslist_search_rows20.html          jobs.macslist.org/search as first shown (20 rows)
    macslist_search_rows100_p{1..4}.html after choosing "100 Rows", pages 1-4 (377 jobs)
    roaddogjobs_search_rows20.html       www.roaddogjobs.com/search ("Only the top 1000")
    macslist_job_ykbcny.html             a job page (JSON-LD; e-mail addresses replaced)
"""

import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.jobboardhq import JobBoardHQConnector, SearchState, read_search
from jobindex.http import HttpClient
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes
from tests.fakes import load_fixture

BOARD = "jobs.macslist.org"
URL = f"https://{BOARD}/search"
JOB = (
    "/job/ykbcny/administrative-assistant-ei-ecse-site-based-beaverton-early-childhood-center"
    "/hillsboro/or"
)


def _page(name: str) -> str:
    return load_fixture("jobboardhq", name)


class FakeSearchPage:
    """Serves saved pages the way the real page changes when a visitor clicks."""

    def __init__(self, pages: dict[int, str], first: str | None = None) -> None:
        self.pages = pages
        self.current = first if first is not None else pages[1]
        self.actions: list[str] = []

    async def html(self) -> str:
        return self.current

    async def choose_rows(self, rows: int) -> None:
        self.actions.append(f"rows {rows}")
        self.current = self.pages[1]

    async def click_page(self, number: int) -> None:
        self.actions.append(f"page {number}")
        self.current = self.pages[number]


def _macslist(**changes: str) -> FakeSearchPage:
    pages = {n: _page(f"macslist_search_rows100_p{n}.html") for n in range(1, 5)}
    pages.update({int(k.removeprefix("p")): v for k, v in changes.items()})
    return FakeSearchPage(pages, first=_page("macslist_search_rows20.html"))


def test_search_state_reads_the_rendered_page() -> None:
    first = SearchState(_page("macslist_search_rows20.html"))
    assert (first.current, first.last, first.rows, first.limit_exceeded) == (1, 19, 20, False)
    assert len(first.job_paths) == 20  # each job linked twice (title + row), kept once
    assert first.job_paths[0] == JOB
    last = SearchState(_page("macslist_search_rows100_p4.html"))
    assert (last.current, last.last, last.rows, len(last.job_paths)) == (4, 4, 100, 77)


async def test_reads_every_page_after_choosing_100_rows() -> None:
    page = _macslist()
    paths = await read_search(page, URL)
    assert page.actions == ["rows 100", "page 2", "page 3", "page 4"]
    assert len(paths) == len(set(paths)) == 377  # 3 full pages + 77


async def test_limit_exceeded_is_refused() -> None:
    # "Only the top 1000 jobs found matching your selection are shown": incomplete list
    rdj = _page("roaddogjobs_search_rows20.html")
    assert SearchState(rdj).limit_exceeded
    page = FakeSearchPage({1: rdj.replace('value="50"', 'value="100"')}, first=rdj)
    with pytest.raises(ConnectorError, match="only part of its jobs"):
        await read_search(page, "https://www.roaddogjobs.com/search")


async def test_short_middle_page_fails() -> None:
    page = _macslist(
        p2=_page("macslist_search_rows100_p4.html").replace(
            '<span class="pagerCurrentPage">4</span>', '<span class="pagerCurrentPage">2</span>'
        )
    )
    with pytest.raises(ConnectorError, match="page 2 of 4 lists 77 jobs"):
        await read_search(page, URL)


async def test_job_on_two_pages_fails() -> None:
    p2 = _page("macslist_search_rows100_p2.html")
    p3 = _page("macslist_search_rows100_p3.html")
    first_p2 = SearchState(p2).job_paths[0]
    first_p3 = SearchState(p3).job_paths[0]
    page = _macslist(p3=p3.replace(first_p3, first_p2))  # the order shifted
    with pytest.raises(ConnectorError, match="listed twice"):
        await read_search(page, URL)


async def test_wrong_page_after_click_fails() -> None:
    page = _macslist(p3=_page("macslist_search_rows100_p2.html"))
    with pytest.raises(ConnectorError, match="asked for page 3, got 2"):
        await read_search(page, URL)


async def test_not_a_search_page_fails() -> None:
    page = FakeSearchPage({1: "<html><body>Page not found</body></html>"})
    with pytest.raises(ConnectorError, match="not a jobBoardHQ search page"):
        await read_search(page, URL)


class _FixtureConnector(JobBoardHQConnector):
    async def read_list(self, http: HttpClient, url: str) -> list[str]:
        return await read_search(_macslist(), url)


async def test_list_and_job_page() -> None:
    routes = fixture_routes("jobboardhq", {f"{BOARD}{JOB}": "macslist_job_ykbcny.html"})
    stubs, details = await crawl_fixture(_FixtureConnector, BOARD, routes)
    assert len(stubs) == 377
    job = details["ykbcny"].normalized()
    assert job.title.startswith("Administrative Assistant - EI/ECSE")
    assert job.company == "Northwest Regional ESD"
    assert job.city == "Hillsboro"
    assert job.region == "OR"
    assert job.posted_at is not None
    assert job.expires_at is not None  # validThrough 2026-11-22
    assert job.description_text


def test_custom_domain_detected_from_page() -> None:
    page = _page("macslist_search_rows20.html")
    assert set(JobBoardHQConnector.detect_in_html(page, URL)) == {BOARD}
    assert JobBoardHQConnector.detect(URL) is None


@pytest.mark.live
@pytest.mark.parametrize("board", ["jobs.macslist.org", "www.jobsindoorcounty.com"])
async def test_live(board: str) -> None:
    pytest.importorskip("playwright")
    async with HttpClient.from_settings() as http:
        connector = JobBoardHQConnector(http)
        stubs = await connector.fetch_jobs(board)
        assert len(stubs) > 100
        sample = [await connector.fetch_job_detail(board, s) for s in stubs[:2]]
    assert all(job is not None and job.title and job.description_html for job in sample)
