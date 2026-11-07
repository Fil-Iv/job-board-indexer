"""Pinpoint connector.

Fixtures (captured 2026-11-07, 2 postings each, otherwise untouched):
    indrive_postings.json  GET https://indrive.pinpointhq.com/postings.json (174 postings)
    davies_postings.json   GET https://davies.pinpointhq.com/postings.json (276 postings;
                           "Home United States" remote location, visible USD salary)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.pinpoint import PinpointConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import HttpClient, RobotsDisallowedError
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http

POSTING = "4bfcf26d-9a07-4a83-89ac-bf4686a9509e"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://indrive.pinpointhq.com", "indrive"),
        ("https://indrive.pinpointhq.com/", "indrive"),
        (f"https://indrive.pinpointhq.com/en/postings/{POSTING}", "indrive"),
        (f"https://indrive.pinpointhq.com/postings/{POSTING}/applications/new", "indrive"),
        ("https://indrive.pinpointhq.com/jobs/436036", "indrive"),
        ("https://indrive.pinpointhq.com/postings.json", "indrive"),
        ("https://Davies.PinpointHQ.com/en", "davies"),
        ("https://www.pinpointhq.com/", None),
        ("https://developers.pinpointhq.com/docs", None),
        ("https://careers.davies-group.com/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert PinpointConnector.detect(url) == expected


async def test_custom_domain_iframe_resolves() -> None:
    page = '<iframe src="https://davies.pinpointhq.com/en?embed=true" title="Jobs"></iframe>'
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("pinpoint", "davies")


def _routes() -> dict[str, httpx.Response]:
    return {
        f"{c}.pinpointhq.com/postings.json": httpx.Response(
            200, text=load_fixture("pinpoint", f"{c}_postings.json")
        )
        for c in ("indrive", "davies")
    }


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http(_routes())
    async with http:
        risk, support = await PinpointConnector(http).fetch_jobs("indrive")
        controller, expert = await PinpointConnector(http).fetch_jobs("davies")

    assert (risk.external_id, risk.title) == ("426478", "Risk Ops Engineer")
    assert risk.remote_type is RemoteType.HYBRID
    assert risk.employment_type == "full_time"
    assert risk.location_raw == "Limassol, Cyprus"
    assert risk.source_url == f"https://indrive.pinpointhq.com/en/postings/{POSTING}"
    assert risk.description_html is not None
    assert "<h3>Key Responsibilities</h3>" in risk.description_html
    assert risk.salary_min is None  # compensation_minimum: null

    assert (support.salary_min, support.salary_max) == (Decimal(2300), Decimal(2300))
    assert (support.salary_currency, support.salary_period) == ("PEN", "month")

    assert controller.remote_type is RemoteType.REMOTE
    assert controller.location_raw == "Alabama, United States"  # "Home" placeholder dropped
    assert (controller.salary_min, controller.salary_max) == (Decimal(125000), Decimal(150000))
    assert (controller.salary_currency, controller.salary_period) == ("USD", "year")
    assert controller.employment_type == "full_time"  # "permanent_full_time"
    assert expert.remote_type is RemoteType.ONSITE


async def test_tenant_disallowing_robots_is_never_fetched() -> None:
    # Real robots.txt of dazn.pinpointhq.com (2026-11-07).
    http, seen = mock_http({}, robots="User-Agent: *\nDisallow: /\n")
    async with http:
        with pytest.raises(RobotsDisallowedError):
            await PinpointConnector(http).fetch_jobs("dazn")
    assert [r.url.path for r in seen] == ["/robots.txt"]


async def test_unexpected_payload_is_refused() -> None:
    http, _ = mock_http({"x.pinpointhq.com/postings.json": httpx.Response(200, json=[])})
    async with http:
        with pytest.raises(ConnectorError):
            await PinpointConnector(http).fetch_jobs("x")


@pytest.mark.live
@pytest.mark.parametrize("company", ["indrive", "davies"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await PinpointConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
