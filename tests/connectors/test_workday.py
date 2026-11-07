"""Workday connector.

Fixtures (captured 2026-11-07):
    nvidia_jobs_page1.json  POST https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
                            {"limit":20,"offset":0,...}; jobPostings trimmed to 3, facets untouched
                            (total=2000 capped, timeType facet sums to 2810)
    nvidia_job_detail.json  GET  .../wday/cxs/nvidia/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/
                            Senior-Manager--Engineering---Data-Center-Firmware_JR2019986
"""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from jobindex.connectors import workday
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.workday import WorkdayConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

BOARD = "nvidia.wd5.myworkdayjobs.com/nvidia/NVIDIAExternalCareerSite"
API = "nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
DETAIL_PATH = "/job/US-CA-Santa-Clara/Senior-Manager--Engineering---Data-Center-Firmware_JR2019986"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", BOARD),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", BOARD),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite{DETAIL_PATH}",
            BOARD,
        ),
        (
            f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite{DETAIL_PATH}/apply",
            BOARD,
        ),
        (f"https://{API}/jobs", BOARD),
        (
            "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site",
            "salesforce.wd12.myworkdayjobs.com/salesforce/External_Career_Site",
        ),
        (
            "https://wd3.myworkdaysite.com/recruiting/pwc/Global_Experienced_Careers",
            "wd3.myworkdaysite.com/pwc/Global_Experienced_Careers",
        ),
        (
            "https://pwc.wd3.myworkdaysite.com/en-US/recruiting/pwc/Global_Experienced_Careers/job/x_1",
            "wd3.myworkdaysite.com/pwc/Global_Experienced_Careers",
        ),
        (
            "https://wd3.myworkdaysite.com/wday/cxs/pwc/Global_Experienced_Careers/jobs",
            "wd3.myworkdaysite.com/pwc/Global_Experienced_Careers",
        ),
        ("https://nvidia.wd5.myworkdayjobs.com/", None),  # no site: cannot crawl
        ("https://wd3.myworkdaysite.com/recruiting/pwc", None),
        ("https://www.workday.com/en-us/company/careers.html", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert WorkdayConnector.detect(url) == expected


def test_board_url() -> None:
    assert WorkdayConnector.board_url(BOARD) == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    )
    assert WorkdayConnector.board_url("wd3.myworkdaysite.com/pwc/Global_Experienced_Careers") == (
        "https://wd3.myworkdaysite.com/recruiting/pwc/Global_Experienced_Careers"
    )


def test_detect_in_html() -> None:
    page = (
        '<a class="btn" href="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite">'
        "View open roles</a>"
    )
    assert WorkdayConnector.detect_in_html(page) == {BOARD: 1}


def _board(postings: list[dict[str, Any]], *, total: int, facet_total: int | None) -> Any:
    """A paging handler that behaves like the real CXS API (verified on NVIDIA 2026-11-07):
    `total` only on page 1, and offsets past the end wrap around to page 1 instead of
    returning an empty page."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset, limit = body["offset"], body["limit"]
        page = postings[offset : offset + limit] if offset < len(postings) else postings[:limit]
        facets = (
            [{"facetParameter": "timeType",
              "values": [{"descriptor": "Full time", "count": facet_total, "id": "x"}]}]
            if offset == 0 and facet_total is not None
            else []
        )
        data = {"total": total if offset == 0 else 0, "jobPostings": page, "facets": facets}
        return httpx.Response(200, json=data)

    return handle


def _postings(n: int) -> list[dict[str, Any]]:
    real = load_json_fixture("workday", "nvidia_jobs_page1.json")["jobPostings"]
    out = []
    for i in range(n):
        p = dict(real[i % len(real)])
        p["externalPath"] = p["externalPath"].rsplit("_", 1)[0] + f"_JR{i:07d}"
        out.append(p)
    return out


async def test_fetch_jobs_from_fixture() -> None:
    body = load_json_fixture("workday", "nvidia_jobs_page1.json")
    body["facets"] = []  # 3 postings, no facet totals -> trust total=3
    body["total"] = 3
    http, seen = mock_http({f"{API}/jobs": httpx.Response(200, json=body)})
    async with http:
        jobs = await WorkdayConnector(http).fetch_jobs(BOARD)

    request = seen[-1]
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "limit": 20,
        "offset": 0,
        "appliedFacets": {},
        "searchText": "",
    }
    assert [j.external_id for j in jobs] == ["JR2019986", "JR1999599", "JR2025894"]
    first, second, _ = jobs
    assert first.title == "Senior Manager, Engineering - Data Center Firmware"
    assert first.location_raw is None  # "2 Locations" is not a location
    assert second.location_raw == "US, CA, Santa Clara"
    assert first.source_url == (
        f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite{DETAIL_PATH}"
    )
    assert first.description_html is None  # comes from the detail request


async def test_pages_past_the_2000_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Like NVIDIA: `total` says 2000 (capped) but there are more; facets know the truth.
    monkeypatch.setattr(workday, "PAGE_SIZE", 5)
    postings = _postings(23)
    http, seen = mock_http({f"{API}/jobs": _board(postings, total=20, facet_total=23)})
    async with http:
        jobs = await WorkdayConnector(http).fetch_jobs(BOARD)
    assert len(jobs) == 23
    offsets = [json.loads(r.content)["offset"] for r in seen if r.method == "POST"]
    assert offsets == [0, 5, 10, 15, 20]


async def test_stops_when_pages_wrap_around(monkeypatch: pytest.MonkeyPatch) -> None:
    # No usable total: only the wrap-around (a page with nothing new) ends the paging.
    monkeypatch.setattr(workday, "PAGE_SIZE", 5)
    http, seen = mock_http({f"{API}/jobs": _board(_postings(10), total=0, facet_total=None)})
    async with http:
        jobs = await WorkdayConnector(http).fetch_jobs(BOARD)
    assert len(jobs) == 10
    offsets = [json.loads(r.content)["offset"] for r in seen if r.method == "POST"]
    assert offsets == [0, 5, 10]  # offset 10 wraps to page 1 -> nothing new -> stop


async def test_short_result_is_retried_then_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workday, "PAGE_SIZE", 5)
    http, seen = mock_http({f"{API}/jobs": _board(_postings(8), total=10, facet_total=10)})
    async with http:
        with pytest.raises(ConnectorError, match="got 8 of 10"):
            await WorkdayConnector(http).fetch_jobs(BOARD)
    assert len([r for r in seen if r.method == "POST"]) == 4  # 2 pages x 2 attempts


async def test_fetch_job_detail() -> None:
    body = load_json_fixture("workday", "nvidia_jobs_page1.json")
    body.update(total=3, facets=[])
    http, _ = mock_http(
        {
            f"{API}/jobs": httpx.Response(200, json=body),
            f"{API}{DETAIL_PATH}": httpx.Response(
                200, text=load_fixture("workday", "nvidia_job_detail.json")
            ),
        }
    )
    async with http:
        connector = WorkdayConnector(http)
        jobs = await connector.fetch_jobs(BOARD)
        detailed = await connector.fetch_job_detail(BOARD, jobs[0])
        assert await connector.fetch_job_detail(BOARD, jobs[1]) is None  # 404: gone

    assert detailed is not None
    assert detailed.external_id == "JR2019986"
    assert detailed.description_html is not None
    assert detailed.description_html.startswith("<p>")
    assert detailed.location_raw == "US, CA, Santa Clara; US, CA, Remote"
    assert detailed.country == "US"
    assert detailed.employment_type == "full_time"
    assert detailed.posted_at is not None
    assert detailed.posted_at.date().isoformat() == "2026-11-06"

    n = detailed.normalized()
    assert (n.city, n.region, n.country) == ("Santa Clara", "CA", "US")
    assert n.remote_type is RemoteType.REMOTE  # "US, CA, Remote" is an additional location
    assert (n.salary_min, n.salary_max, n.salary_currency) == (
        Decimal(272000),
        Decimal("431250"),
        "USD",
    )


@pytest.mark.live
@pytest.mark.parametrize(
    "url",
    [
        "https://adobe.wd5.myworkdayjobs.com/external_experienced",
        "https://wd3.myworkdaysite.com/recruiting/pwc/Global_Experienced_Careers",  # 2nd domain
    ],
)
async def test_live(url: str) -> None:
    board = WorkdayConnector.detect(url)
    assert board is not None
    async with HttpClient.from_settings() as http:
        connector = WorkdayConnector(http)
        jobs = await connector.fetch_jobs(board)
        assert len(jobs) > 20  # more than one page
        assert len({j.external_id for j in jobs}) == len(jobs)
        detailed = await connector.fetch_job_detail(board, jobs[0])
    assert detailed is not None
    assert detailed.description_html
    assert detailed.posted_at is not None
