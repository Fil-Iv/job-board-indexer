"""ADP Workforce Now connector.

Fixtures (captured 2026-11-07, verbatim, re-indented):
    us_requisitions.json      GET .../staffing/v1/job-requisitions?cid=d4d64eb4-d344-423a-868e-
                              ae5d37380d2f&lang=en_US&locale=en_US&$top=100 (7 of 7)
    us_requisition_9200864836847_1.json   GET .../job-requisitions/9200864836847_1?cid=...
    ca_requisitions.json      GET ...?cid=8d16626f-48ae-43f6-b57e-79339726ccf4&lang=en_CA
                              &locale=en_CA&$top=20 (3 of 3)
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from jobindex.connectors import adp
from jobindex.connectors.adp import ADPConnector
from jobindex.connectors.base import ConnectorError
from jobindex.http import FetchError, HttpClient
from tests.fakes import load_fixture, load_json_fixture, mock_http

API = "workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
US = "d4d64eb4-d344-423a-868e-ae5d37380d2f"  # fixtures; employer never named in its data
NOU_SYSTEMS = "9e26f425-d064-4c19-9e7b-cc8236837a7c"  # live example (names itself)
CA = "8d16626f-48ae-43f6-b57e-79339726ccf4"
PAGE = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{PAGE}?cid={US}&ccId=19000101_000001&lang=en_US", US),
        (f"{PAGE}?cid={US}", US),
        (f"{PAGE}?cid={CA}&ccId=19000101_000001&lang=en_CA", f"{CA}:en_CA"),
        (f"https://{API}?cid={US.upper()}&lang=en_US&$top=20", US),
        (f"{PAGE}?cid=not-a-uuid", None),
        ("https://recruiting.adp.com/srccar/public/RTI.home?c=123", None),
        ("https://www.adp.com/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert ADPConnector.detect(url) == expected


def _json(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("adp", name))


async def test_fetch_jobs_and_detail_from_fixture() -> None:
    def lists(request: httpx.Request) -> httpx.Response:
        if request.url.params["cid"] == CA:
            assert request.url.params["lang"] == "en_CA"
            return _json("ca_requisitions.json")
        return _json("us_requisitions.json")

    http, _ = mock_http(
        {API: lists, f"{API}/9200864836847_1": _json("us_requisition_9200864836847_1.json")}
    )
    async with http:
        connector = ADPConnector(http)
        us = await connector.fetch_jobs(US)
        ca = await connector.fetch_jobs(f"{CA}:en_CA")
        detail = await connector.fetch_job_detail(US, us[0])
        gone = await connector.fetch_job_detail(US, us[1])  # 404
    assert len(us) == 7
    assert len(ca) == 3
    cook = us[0]
    assert (cook.external_id, cook.title) == ("9200864836847_1", "Prep Cook")
    assert (cook.city, cook.region) == ("Miami Beach", "FL")
    assert cook.employment_type == "full_time"
    assert (cook.salary_min, cook.salary_max, cook.salary_currency, cook.salary_period) == (
        None,  # minimum 0.0 = none
        Decimal("16.0"),
        "USD",
        "hour",
    )
    assert cook.posted_at == datetime(2026, 8, 14, 6, 55, tzinfo=UTC)
    assert cook.source_url == f"{PAGE}?cid={US}&lang=en_US&jobId=552809"
    assert ca[0].source_url is not None
    assert "lang=en_CA" in ca[0].source_url
    assert detail is not None
    assert detail.description_html is not None
    assert "About the Role" in detail.description_html
    assert gone is None


async def test_skip_is_one_based_and_count_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    items = load_json_fixture("adp", "us_requisitions.json")["jobRequisitions"]

    def pages(total: int) -> object:
        def handle(request: httpx.Request) -> httpx.Response:
            skip = int(request.url.params["$skip"])  # 1-based start position
            page = items[skip - 1 : skip - 1 + 3]
            return httpx.Response(
                200, json={"meta": {"totalNumber": total}, "jobRequisitions": page}
            )

        return handle

    monkeypatch.setattr(adp, "PAGE_SIZE", 3)
    http, seen = mock_http({API: pages(7)})  # type: ignore[dict-item]
    async with http:
        jobs = await ADPConnector(http).fetch_jobs(US)
    assert len(jobs) == 7
    skips = [r.url.params["$skip"] for r in seen if r.url.path.endswith("requisitions")]
    assert skips == ["1", "4", "7"]
    http, _ = mock_http({API: pages(9)})  # type: ignore[dict-item]
    async with http:
        with pytest.raises(ConnectorError, match="reports 9 jobs"):
            await ADPConnector(http).fetch_jobs(US)


async def test_unknown_board_and_bad_payload() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.params["cid"] == US:
            return httpx.Response(200, json={"error": "x"})
        return httpx.Response(404, json={})

    http, _ = mock_http({API: route})
    async with http:
        with pytest.raises(FetchError, match="404"):
            await ADPConnector(http).fetch_jobs(CA)
        with pytest.raises(ConnectorError, match="unexpected response"):
            await ADPConnector(http).fetch_jobs(US)


@pytest.mark.live
@pytest.mark.parametrize("board", [NOU_SYSTEMS, f"{CA}:en_CA"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = ADPConnector(http)
        jobs = await connector.fetch_jobs(board)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(board, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html
