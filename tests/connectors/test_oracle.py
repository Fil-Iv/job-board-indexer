"""Oracle Recruiting Cloud connector.

Fixtures (captured 2026-11-07):
    eeho_requisitions_offset2700.json  GET https://eeho.fa.us2.oraclecloud.com/hcmRestApi/
        resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.
        secondaryLocations&finder=findReqs;siteNumber=CX_45001,limit=200,offset=2700,...
        (Oracle's own careers site; requisitionList trimmed to 3 of 141, facets to 3 values;
        TotalJobsCount 2843 although the full list returns 2841)
    eeho_detail_345736.json            GET .../recruitingCEJobRequisitionDetails?expand=all
        &onlyData=true&finder=ById;Id="345736",siteNumber=CX_45001
"""

import copy
import json
from typing import Any

import httpx
import pytest

from jobindex.connectors import oracle
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.oracle import OracleConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

HOST = "eeho.fa.us2.oraclecloud.com"
BOARD = f"{HOST}/CX_45001"
LIST = f"{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL = f"{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/requisitions", BOARD),
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/job/345736", BOARD),
        (f"https://{HOST}/hcmUI/CandidateExperience/fr/sites/CX_45001/job/345736/apply", BOARD),
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001", BOARD),
        (f"https://{LIST}?onlyData=true&finder=findReqs;siteNumber=CX_45001,limit=25", BOARD),
        (
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/requisitions",
            "jpmc.fa.oraclecloud.com/CX_1001",
        ),
        (f"https://{HOST}/hcmUI/CandidateExperience/en/sites", None),  # no site
        (f"https://{HOST}/fscmUI/faces/FuseWelcome", None),  # the ERP login, not a career site
        ("https://www.oracle.com/careers/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert OracleConnector.detect(url) == expected


async def test_custom_domain_link_resolves() -> None:
    page = (
        '<a href="https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001'
        '/requisitions?location=United+States">Search jobs</a>'
    )
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("oracle", "jpmc.fa.oraclecloud.com/CX_1001")


def _paged(n: int, total: int, page_size: int) -> Any:
    """List handler: ``n`` requisitions built from the real page, ``total`` reported."""
    real = load_json_fixture("oracle", "eeho_requisitions_offset2700.json")
    template = real["items"][0]["requisitionList"]
    rows = []
    for i in range(n):
        row = copy.deepcopy(template[i % len(template)])
        row["Id"] = str(100000 + i)
        rows.append(row)

    def handle(request: httpx.Request) -> httpx.Response:
        finder = request.url.params["finder"]
        offset = int(finder.split("offset=")[1].split(",")[0])
        body = copy.deepcopy(real)
        body["items"][0]["requisitionList"] = rows[offset : offset + page_size]
        body["items"][0]["TotalJobsCount"] = total
        return httpx.Response(200, text=json.dumps(body))

    return handle


async def test_fetch_jobs_from_fixture() -> None:
    page = load_fixture("oracle", "eeho_requisitions_offset2700.json")
    http, seen = mock_http({LIST: httpx.Response(200, text=page)})
    async with http:
        # 3 rows < page size ends the paging; TotalJobsCount 2843 is far above -> refused.
        with pytest.raises(ConnectorError, match="got 3 of 2843"):
            await OracleConnector(http).fetch_jobs(BOARD)
    finder = next(r for r in seen if r.url.path.endswith("Requisitions")).url.params["finder"]
    assert finder.startswith("findReqs;siteNumber=CX_45001,limit=200,offset=0")


async def test_fetch_jobs_pages_and_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle, "PAGE_SIZE", 2)
    http, _ = mock_http({LIST: _paged(5, 5, 2)})
    async with http:
        jobs = await OracleConnector(http).fetch_jobs(BOARD)
    assert [j.external_id for j in jobs] == [str(100000 + i) for i in range(5)]
    first, _, multi, *_ = jobs
    assert first.title
    assert first.posted_at is not None
    assert first.source_url == (
        f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_45001/job/100000"
    )
    # The third fixture row has 3 secondary locations: all are listed, country left open.
    assert multi.location_raw is not None
    assert "San Jose, CA, United States" in multi.location_raw
    assert multi.country is None


async def test_small_count_gap_is_tolerated_large_gap_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "PAGE_SIZE", 2)
    http, _ = mock_http({LIST: _paged(5, 7, 2)})  # like 2841/2843: 2 counted, never listed
    async with http:
        assert len(await OracleConnector(http).fetch_jobs(BOARD)) == 5
    http, seen = mock_http({LIST: _paged(5, 9, 2)})
    async with http:
        with pytest.raises(ConnectorError, match="got 5 of 9"):
            await OracleConnector(http).fetch_jobs(BOARD)
    assert sum(r.url.path.endswith("Requisitions") for r in seen) == 6  # re-paged once


async def test_fetch_job_detail() -> None:
    detail = load_fixture("oracle", "eeho_detail_345736.json")
    http, seen = mock_http({DETAIL: httpx.Response(200, text=detail)})
    async with http:
        job = OracleConnector._map_list({"Id": "345736", "Title": "X"}, BOARD)
        detailed = await OracleConnector(http).fetch_job_detail(BOARD, job)
    assert seen[-1].url.params["finder"] == 'ById;Id="345736",siteNumber=CX_45001'
    assert detailed is not None
    assert detailed.description_html is not None
    assert "About the Role" in detailed.description_html
    assert "What You’ll Do" in detailed.description_html  # responsibilities section
    assert detailed.remote_type is RemoteType.UNKNOWN  # WorkplaceTypeCode null


async def test_closed_job_detail_returns_none() -> None:
    http, _ = mock_http({DETAIL: httpx.Response(200, json={"items": [], "count": 0})})
    async with http:
        job = OracleConnector._map_list({"Id": "1", "Title": "X"}, BOARD)
        assert await OracleConnector(http).fetch_job_detail(BOARD, job) is None


@pytest.mark.live
@pytest.mark.parametrize("board", [BOARD, "jpmc.fa.oraclecloud.com/CX_1001"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = OracleConnector(http)
        jobs = await connector.fetch_jobs(board)
        assert len(jobs) > 0
        detailed = await connector.fetch_job_detail(board, jobs[0])
    assert len({j.external_id for j in jobs}) == len(jobs)
    assert detailed is not None
    assert detailed.description_html
