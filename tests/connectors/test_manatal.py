"""Manatal connector.

Fixtures (captured 2026-11-07):
    eastwest_bank_page1.json        GET https://api.manatal.com/open/v3/career-page/
                                    eastwest-bank/jobs/ (page 1 of 22 at 10/page; results
                                    trimmed to 3; count 216, next -> core.api.manatal.com)
    mr_diy_philippines_page1.json   GET .../career-page/mr-diy-philippines/jobs/?page_size=100
                                    (results trimmed to 2; count 2260)
"""

import copy
import json
from typing import Any

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.manatal import ManatalConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import FetchError, HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_json_fixture, mock_http

API = "api.manatal.com/open/v3/career-page"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.careers-page.com/eastwest-bank", "eastwest-bank"),
        ("https://www.careers-page.com/eastwest-bank/", "eastwest-bank"),
        ("https://www.careers-page.com/eastwest-bank/job/V6YWXX48", "eastwest-bank"),
        ("https://careers-page.com/eastwest-bank/job/V6YWXX48/apply", "eastwest-bank"),
        ("https://www.careers-page.com/jobsultant-solutions?page=11", "jobsultant-solutions"),
        ("https://axioshq.careers-page.com/", "axioshq"),  # subdomain style
        (f"https://{API}/mr-diy-philippines/jobs/?page=2", "mr-diy-philippines"),
        ("https://core.api.manatal.com/open/v3/career-page/eastwest-bank/jobs/", "eastwest-bank"),
        ("https://www.careers-page.com/", None),
        ("https://www.manatal.com/careers", None),
        ("https://www.eastwestbanker.com/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert ManatalConnector.detect(url) == expected


async def test_custom_domain_iframe_resolves() -> None:
    page = '<iframe src="https://www.careers-page.com/eastwest-bank" width="100%"></iframe>'
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("manatal", "eastwest-bank")


def _paged(fixture: str, n: int, count: int, page_size: int) -> Any:
    real = load_json_fixture("manatal", fixture)
    template = real["results"]
    rows = []
    for i in range(n):
        row = copy.deepcopy(template[i % len(template)])
        row["id"] = str(1000 + i)
        rows.append(row)

    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        chunk = rows[(page - 1) * page_size : page * page_size]
        more = page * page_size < n
        body = {
            "count": count,
            "next": f"https://core.api.manatal.com/x?page={page + 1}" if more else None,
            "previous": None,
            "results": chunk,
        }
        return httpx.Response(200, text=json.dumps(body))

    return handle


async def test_fetch_jobs_follows_pages_on_same_host() -> None:
    http, seen = mock_http(
        {f"{API}/eastwest-bank/jobs/": _paged("eastwest_bank_page1.json", 7, 7, 3)}
    )
    async with http:
        jobs = await ManatalConnector(http).fetch_jobs("eastwest-bank")
    assert len(jobs) == 7
    lists = [r for r in seen if r.url.path.endswith("/jobs/")]
    assert [r.url.params["page"] for r in lists] == ["1", "2", "3"]
    assert {r.url.params["ordering"] for r in lists} == {"created_at"}  # stable order
    assert {r.url.host for r in seen} == {"api.manatal.com"}  # never core.api.manatal.com


async def test_map_from_fixture() -> None:
    real = load_json_fixture("manatal", "eastwest_bank_page1.json")
    real.update(count=3, next=None)
    http, _ = mock_http({f"{API}/eastwest-bank/jobs/": httpx.Response(200, json=real)})
    async with http:
        first, binondo, _ = await ManatalConnector(http).fetch_jobs("eastwest-bank")
    assert (first.external_id, first.title) == ("3745637", "Account Associate")
    assert first.location_raw == "National Capital Region, Philippines"
    assert first.country == "Philippines"
    assert first.employment_type == "full_time"
    assert first.remote_type is RemoteType.UNKNOWN  # is_remote: null
    assert first.description_html
    assert first.source_url == "https://www.careers-page.com/eastwest-bank/job/V6YWXX48"
    assert binondo.city == "Manila"


async def test_unstable_default_order_would_be_caught() -> None:
    # What the default order did on MR.DIY: rows 4-5 repeated on page 2, rows 6-7 never
    # served. Unique count < count -> re-paged once, then refused (never a partial list).
    real = load_json_fixture("manatal", "mr_diy_philippines_page1.json")
    rows = [dict(real["results"][0], id=str(i)) for i in range(8)]
    pages = {1: rows[0:4], 2: rows[2:4] + rows[4:6]}

    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body = {"count": 8, "next": "x" if page == 1 else None, "results": pages[page]}
        return httpx.Response(200, json=body)

    http, _ = mock_http({f"{API}/mr-diy-philippines/jobs/": handle})
    async with http:
        with pytest.raises(ConnectorError, match="got 6 of 8"):
            await ManatalConnector(http).fetch_jobs("mr-diy-philippines")


async def test_count_mismatch_is_refused() -> None:
    handler = _paged("mr_diy_philippines_page1.json", 4, 6, 2)  # claims 6, lists 4
    http, _ = mock_http({f"{API}/mr-diy-philippines/jobs/": handler})
    async with http:
        with pytest.raises(ConnectorError, match="got 4 of 6"):
            await ManatalConnector(http).fetch_jobs("mr-diy-philippines")


async def test_unknown_page_is_404() -> None:
    http, _ = mock_http({})
    async with http:
        with pytest.raises(FetchError, match="404"):
            await ManatalConnector(http).fetch_jobs("nosuch")


@pytest.mark.live
@pytest.mark.parametrize("slug", ["eastwest-bank", "mr-diy-philippines"])
async def test_live(slug: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await ManatalConnector(http).fetch_jobs(slug)
    assert len(jobs) > 0
    assert all(j.title and j.source_url for j in jobs)
