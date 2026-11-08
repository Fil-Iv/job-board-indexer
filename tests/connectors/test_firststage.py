"""FirstStage connector (listing page + JSON-LD job pages).

Fixtures (captured 2026-11-08):
    jobs.html               GET https://agility-gwg.firststage.co/jobs  ("80 jobs open",
                            79 distinct job links; <script>, <style>, <svg>, <link> and
                            class attributes removed)
    job_bD7hZJYQlf.html     GET https://agility-gwg.firststage.co/jobs/bD7hZJYQlf/view
                            (<style>, <svg>, non-JSON-LD <script> and <link> removed)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.firststage import FirstStageConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import mock_http

BOARD = "agility-gwg.firststage.co"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://agility-gwg.firststage.co/jobs", BOARD),
        ("https://Agility-GWG.firststage.co/jobs/bD7hZJYQlf/view", BOARD),
        ("https://www.firststage.co/", None),
        ("https://app.firststage.co/login", None),
        ("https://firststage.org/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert FirstStageConnector.detect(url) == expected


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "firststage",
        {f"{BOARD}/jobs": "jobs.html", f"{BOARD}/jobs/bD7hZJYQlf/view": "job_bD7hZJYQlf.html"},
    )
    stubs, details = await crawl_fixture(FirstStageConnector, BOARD, routes)
    assert len(stubs) == 79  # the heading says 80; one job is counted but not linked
    assert len({s.external_id for s in stubs}) == 79
    assert all(s.source_url and "?" not in s.source_url for s in stubs)
    job = details["bD7hZJYQlf"].normalized()
    assert (job.title, job.company) == ("Accounts & Audit Senior Bradford", "Agility GWG")
    assert job.city == "Bradford"
    assert job.salary_currency == "GBP"
    assert job.description_html
    assert "Bradford" in job.description_html


def _listing(count: int, ids: list[str]) -> str:
    links = "".join(f'<a href="/jobs/{i}/view?layout=list">x</a>' for i in ids)
    return f"<h2>{count} jobs open</h2>{links}"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (_listing(3, ["aaaaaaaaaa"]), "reports 3 jobs, links 1"),  # too many unlinked
        (_listing(1, ["aaaaaaaaaa", "bbbbbbbbbb"]), "reports 1 jobs, links 2"),
        ("<h2>Jobs</h2>", "no job count"),
    ],
)
async def test_count_mismatch_raises(body: str, error: str) -> None:
    http, _ = mock_http({f"{BOARD}/jobs": httpx.Response(200, text=body)})
    async with http:
        with pytest.raises(ConnectorError, match=error):
            await FirstStageConnector(http).fetch_jobs(BOARD)


@pytest.mark.live
async def test_live() -> None:
    await live_check(FirstStageConnector, BOARD)
