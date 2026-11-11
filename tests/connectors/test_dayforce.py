"""Dayforce connector (job board rendered in the polite browser; search responses read).

Fixtures (captured 2026-11-11 with jobindex.browser: the bodies of the
``/api/geo/{ns}/jobposting/search`` responses the page received; e-mail addresses
replaced by hr@example.com):
    dayforce_loco_p1.json    El Pollo Loco, the only page (17 of 17)
    dayforce_costa_p1.json   Costa Coffee, page 1 (offset 0, 25 of 446)
    dayforce_costa_p2.json   Costa Coffee, page 2 (offset 25)
"""

import json
from typing import Any

import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.dayforce import DayforceConnector, TotalChangedError, read_results
from jobindex.http import HttpClient
from tests.fakes import load_fixture, mock_http

URL = "https://jobs.dayforcehcm.com/en-US/costa/CANDIDATEPORTAL"


def _data(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(load_fixture("dayforce", name))
    return data


class FakeResults:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.clicks = 0

    async def first(self) -> dict[str, Any]:
        return self.pages[0]

    async def next(self) -> dict[str, Any]:
        self.clicks += 1
        return self.pages[self.clicks]


def _costa(total: int = 50) -> list[dict[str, Any]]:
    """The two saved Costa pages, as if the board had ``total`` jobs."""
    return [{**_data(f"dayforce_costa_p{n}.json"), "maxCount": total} for n in (1, 2)]


def test_detect() -> None:
    detect = DayforceConnector.detect
    assert detect("https://jobs.dayforcehcm.com/en-GB/costa/CANDIDATEPORTAL") == (
        "costa/CANDIDATEPORTAL"
    )
    assert detect("https://jobs.dayforcehcm.com/loco/CANDIDATEPORTAL/jobs/33501") == (
        "loco/CANDIDATEPORTAL"
    )
    assert detect("https://jobs.dayforcehcm.com/api/geo/loco/jobposting/search") is None
    assert detect("https://www.example.com/en-US/costa/CANDIDATEPORTAL") is None
    assert DayforceConnector.board_url("costa/CANDIDATEPORTAL") == URL


async def test_reads_pages_until_the_sites_total() -> None:
    results = FakeResults(_costa(50))
    postings = await read_results(results, URL)
    assert results.clicks == 1
    assert len(postings) == 50


async def test_one_page_board() -> None:
    results = FakeResults([_data("dayforce_loco_p1.json")])
    assert len(await read_results(results, URL)) == 17
    assert results.clicks == 0


async def test_short_of_the_total_fails() -> None:
    pages = _costa(51)
    pages.append({**pages[1], "offset": 50, "count": 0, "jobPostings": []})
    with pytest.raises(ConnectorError, match="read 50 jobs, the site says 51"):
        await read_results(FakeResults(pages), URL)


async def test_changed_total_fails() -> None:
    pages = _costa(50)
    pages[1]["maxCount"] = 49
    with pytest.raises(TotalChangedError, match="total changed"):
        await read_results(FakeResults(pages), URL)


async def test_wrong_offset_fails() -> None:
    pages = _costa(50)
    pages[1] = pages[0]  # the page did not move on
    with pytest.raises(ConnectorError, match="expected offset 25"):
        await read_results(FakeResults(pages), URL)


async def test_unexpected_response_fails() -> None:
    with pytest.raises(ConnectorError, match="unexpected search response"):
        await read_results(FakeResults([{"error": "denied"}]), URL)


class _FixtureConnector(DayforceConnector):
    async def read_list(self, http: HttpClient, url: str) -> list[dict[str, Any]]:
        return await read_results(FakeResults([_data("dayforce_loco_p1.json")]), url)


async def test_postings_are_mapped() -> None:
    http, seen = mock_http({})
    async with http:
        jobs = await _FixtureConnector(http).fetch_jobs("loco/CANDIDATEPORTAL")
    assert not seen  # everything comes from the page; no other request
    job = next(j for j in jobs if j.external_id == "33501").normalized()
    assert job.title == "DIRECTOR/SR. DIRECTOR, STRATEGY, INSIGHTS & ANALYTICS"
    assert job.city == "Costa Mesa"
    assert job.region == "CA"
    assert job.country == "US"
    assert job.posted_at is not None
    assert job.description_text
    assert "&rsquo;" not in (job.description_text or "")
    assert job.source_url == ("https://jobs.dayforcehcm.com/en-US/loco/CANDIDATEPORTAL/jobs/33501")
    costa = DayforceConnector(http)._to_job(
        "costa/CANDIDATEPORTAL", _data("dayforce_costa_p1.json")["jobPostings"][0]
    )
    assert costa.country == "GB"
    assert costa.city


@pytest.mark.live
@pytest.mark.parametrize("board", ["loco/CANDIDATEPORTAL", "costa/CANDIDATEPORTAL"])
async def test_live(board: str) -> None:
    pytest.importorskip("playwright")
    async with HttpClient.from_settings() as http:
        jobs = await DayforceConnector(http).fetch_jobs(board)
    assert jobs
    assert all(j.title for j in jobs)
    # a few postings have an (almost) empty description at the source
    assert sum(bool(j.description_text) for j in jobs) >= 0.8 * len(jobs)
