"""Trakstar Hire connector.

Fixtures (captured 2026-11-07):
    stax_openings.json            GET https://jsapi.recruiterbox.com/v1/openings
                                  ?client_name=stax&limit=100 (2 of 31 kept, meta.total
                                  adjusted to 2)
    timessquarenyc_openings.json  GET ...?client_name=timessquarenyc (all 4)
"""

import httpx
import pytest

from jobindex.connectors import trakstar
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.connectors.trakstar import TrakstarConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

API = "jsapi.recruiterbox.com/v1/openings"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://stax.hire.trakstar.com", "stax"),
        ("https://stax.hire.trakstar.com/", "stax"),
        ("https://stax.hire.trakstar.com/jobs/fk0znd5/", "stax"),
        ("https://stax.hire.trakstar.com/jobs/fk0znd5/apply/", "stax"),
        ("https://TimesSquareNYC.hire.trakstar.com/", "timessquarenyc"),
        ("https://stax.recruiterbox.com/jobs", "stax"),  # pre-rebrand URLs
        (f"https://{API}?client_name=stax", "stax"),
        ("https://support.hire.trakstar.com/article/30-welcome-to-trakstar-hire", None),
        ("https://status.hire.trakstar.com/", None),
        ("https://www.trakstar.com/", None),
        ("https://www.stax.com/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert TrakstarConnector.detect(url) == expected


async def test_custom_domain_link_resolves() -> None:
    page = '<a href="https://stax.hire.trakstar.com/jobs/fk0znd5/">Manager</a>'
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("trakstar", "stax")


async def test_fetch_jobs_from_fixture() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        name = request.url.params["client_name"]
        return httpx.Response(200, text=load_fixture("trakstar", f"{name}_openings.json"))

    http, seen = mock_http({API: handle})
    async with http:
        manager, _ = await TrakstarConnector(http).fetch_jobs("stax")
        analyst, worker, *_ = await TrakstarConnector(http).fetch_jobs("timessquarenyc")
    assert seen[-1].url.params["limit"] == "100"
    assert (manager.external_id, manager.title) == (
        "fk0znd5",
        "Manager (Commercial Due Diligence )",
    )
    assert (manager.city, manager.region, manager.country) == (
        "Charlotte",
        "North Carolina",
        "United States",
    )
    assert manager.remote_type is RemoteType.REMOTE  # allows_remote: true
    assert manager.employment_type == "full_time"
    assert manager.description_html
    assert manager.source_url == "https://stax.hire.trakstar.com/jobs/fk0znd5/"
    assert analyst.remote_type is RemoteType.REMOTE
    assert worker.remote_type is RemoteType.UNKNOWN  # allows_remote: false = not stated


async def test_pages_and_refuses_short_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trakstar, "PAGE_SIZE", 2)
    real = load_json_fixture("trakstar", "timessquarenyc_openings.json")
    rows = real["objects"]

    def handle(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200, json={"meta": {"total": 5}, "objects": rows[offset : offset + 2]}
        )

    http, seen = mock_http({API: handle})
    async with http:
        with pytest.raises(ConnectorError, match="got 4 of 5"):
            await TrakstarConnector(http).fetch_jobs("timessquarenyc")
    assert [r.url.params["offset"] for r in seen if r.url.path == "/v1/openings"] == ["0", "2", "4"]


async def test_unknown_company_is_an_error() -> None:
    http, _ = mock_http({API: httpx.Response(400, json={"error": "unknown client"})})
    async with http:
        with pytest.raises(Exception, match="400"):
            await TrakstarConnector(http).fetch_jobs("nosuch")


@pytest.mark.live
@pytest.mark.parametrize("company", ["stax", "timessquarenyc"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await TrakstarConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
