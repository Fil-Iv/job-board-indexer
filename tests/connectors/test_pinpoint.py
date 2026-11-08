"""Pinpoint connector.

Fixtures (2 postings each, otherwise untouched):
    lush_postings.json     GET https://lush.pinpointhq.com/postings.json (2026-11-08,
                           32 postings; hybrid AUD yearly and onsite GBP hourly salaries)
    davies_postings.json   GET https://davies.pinpointhq.com/postings.json (2026-11-07,
                           276 postings; "Home United States" remote, visible USD salary)
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

POSTING = "c9777e06-9c98-4003-8a62-b2dd5829f02f"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://lush.pinpointhq.com", "lush"),
        ("https://lush.pinpointhq.com/", "lush"),
        (f"https://lush.pinpointhq.com/en/postings/{POSTING}", "lush"),
        (f"https://lush.pinpointhq.com/postings/{POSTING}/applications/new", "lush"),
        ("https://lush.pinpointhq.com/jobs/607459", "lush"),
        ("https://lush.pinpointhq.com/postings.json", "lush"),
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
        for c in ("lush", "davies")
    }


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http(_routes())
    async with http:
        analyst, assistant = await PinpointConnector(http).fetch_jobs("lush")
        controller, expert = await PinpointConnector(http).fetch_jobs("davies")

    assert (analyst.external_id, analyst.title) == ("607459", "Digital Business Analyst - APAC")
    assert analyst.remote_type is RemoteType.HYBRID
    assert analyst.employment_type == "full_time"  # "permanent_full_time"
    assert analyst.location_raw == "Villawood, NSW, Villawood - Hybrid"
    assert analyst.source_url == f"https://lush.pinpointhq.com/en/postings/{POSTING}"
    assert analyst.description_html is not None
    assert "<h3>" in analyst.description_html
    assert (analyst.salary_min, analyst.salary_max) == (Decimal(85000), Decimal(100000))
    assert (analyst.salary_currency, analyst.salary_period) == ("AUD", "year")

    assert assistant.remote_type is RemoteType.ONSITE
    assert assistant.employment_type == "part_time"  # "seasonal_part_time"
    assert (assistant.salary_min, assistant.salary_max) == (Decimal("13.45"), Decimal("13.45"))
    assert (assistant.salary_currency, assistant.salary_period) == ("GBP", "hour")

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
@pytest.mark.parametrize("company", ["lush", "davies"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await PinpointConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
