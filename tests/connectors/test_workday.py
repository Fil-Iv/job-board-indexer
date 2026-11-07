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


def _board(
    postings: list[dict[str, Any]],
    *,
    cap: int = 2000,
    families: list[str] | None = None,
    facet_total: bool = True,
    report_total: bool = True,
) -> Any:
    """A paging handler modelled on the real CXS API (verified on NVIDIA 2026-11-07):
    at most ``cap`` results per query; ``total`` only on page 1 and itself capped; offsets
    past the end (or the cap) wrap to page 1; ``appliedFacets.jobFamilyGroup`` filters by
    ``families[i]``; facets report true counts for the filtered set."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset, limit = body["offset"], body["limit"]
        wanted = body["appliedFacets"].get("jobFamilyGroup")
        rows = [
            p for i, p in enumerate(postings) if not wanted or (families and families[i] in wanted)
        ]
        reachable = rows[:cap]
        page = reachable[offset : offset + limit] if offset < len(reachable) else rows[:limit]
        facets = []
        if offset == 0 and facet_total:
            facets.append(
                {
                    "facetParameter": "timeType",
                    "values": [{"descriptor": "Full time", "count": len(rows), "id": "ft"}],
                }
            )
        if offset == 0 and families:
            counts: dict[str, int] = {}
            for i, _ in enumerate(postings):
                if not wanted or families[i] in wanted:
                    counts[families[i]] = counts.get(families[i], 0) + 1
            facets.append(
                {
                    "facetParameter": "jobFamilyGroup",
                    "values": [{"descriptor": f, "count": c, "id": f} for f, c in counts.items()],
                }
            )
        total = min(len(rows), cap) if offset == 0 and report_total else 0
        return httpx.Response(200, json={"total": total, "jobPostings": page, "facets": facets})

    return handle


def _postings(n: int) -> list[dict[str, Any]]:
    real = load_json_fixture("workday", "nvidia_jobs_page1.json")["jobPostings"]
    out = []
    for i in range(n):
        p = dict(real[i % len(real)])
        p["externalPath"] = p["externalPath"].rsplit("_", 1)[0] + f"_JR{i:07d}"
        out.append(p)
    return out


def _post_bodies(seen: list[httpx.Request]) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in seen if r.method == "POST"]


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


async def test_board_above_cap_is_split_by_facet(monkeypatch: pytest.MonkeyPatch) -> None:
    # Like NVIDIA: 23 postings but a query only reaches 10; jobFamilyGroup partitions them.
    monkeypatch.setattr(workday, "PAGE_SIZE", 5)
    monkeypatch.setattr(workday, "RESULT_CAP", 10)
    families = ["eng"] * 9 + ["sales"] * 8 + ["ops"] * 6
    handler = _board(_postings(23), cap=10, families=families)
    http, seen = mock_http({f"{API}/jobs": handler})
    async with http:
        jobs = await WorkdayConnector(http).fetch_jobs(BOARD)
    assert len(jobs) == 23
    applied = [tuple(b["appliedFacets"].get("jobFamilyGroup", [])) for b in _post_bodies(seen)]
    assert set(applied) == {(), ("eng",), ("sales",), ("ops",)}


async def test_above_cap_without_partition_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workday, "PAGE_SIZE", 5)
    monkeypatch.setattr(workday, "RESULT_CAP", 10)
    http, _ = mock_http({f"{API}/jobs": _board(_postings(23), cap=10)})  # no family facet
    async with http:
        with pytest.raises(ConnectorError, match="no facet partitions them"):
            await WorkdayConnector(http).fetch_jobs(BOARD)


async def test_non_json_page_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workday, "NON_JSON_BACKOFF_SECONDS", 0)
    body = load_json_fixture("workday", "nvidia_jobs_page1.json")
    body.update(total=3, facets=[])
    replies = [httpx.Response(200, text=""), httpx.Response(200, json=body)]
    http, _ = mock_http({f"{API}/jobs": lambda request: replies.pop(0)})
    async with http:
        assert len(await WorkdayConnector(http).fetch_jobs(BOARD)) == 3


async def test_stops_when_pages_wrap_around(monkeypatch: pytest.MonkeyPatch) -> None:
    # No usable total: only the wrap-around (a page with nothing new) ends the paging.
    monkeypatch.setattr(workday, "PAGE_SIZE", 5)
    http, seen = mock_http(
        {f"{API}/jobs": _board(_postings(10), facet_total=False, report_total=False)}
    )
    async with http:
        jobs = await WorkdayConnector(http).fetch_jobs(BOARD)
    assert len(jobs) == 10
    assert [b["offset"] for b in _post_bodies(seen)] == [0, 5, 10]  # 10 wraps to page 1


def test_partition_facet_prefers_smallest_largest_slice() -> None:
    page = load_json_fixture("workday", "nvidia_jobs_page1.json")  # real NVIDIA facets
    split = workday._partition_facet(page, 2810, {})
    assert split is not None
    param, values = split
    assert param == "jobFamilyGroup"  # max 1779, vs workerSubType 2307 / timeType 2809
    assert sum(c for _, c in values) == 2810


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
