"""Breezy HR connector.

Fixtures (captured 2026-11-07):
    kimmel_associates_verbose.json  3 of 478 positions from
                                    GET https://kimmel-associates.breezy.hr/json?verbose=true
                                    (salary text; one fully remote)
    everstar_verbose.json           1 of 387 positions from
                                    GET https://everstar.breezy.hr/json?verbose=true
                                    (Ukrainian text, no salary)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.breezy import BreezyConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import FetchError, HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://kimmel-associates.breezy.hr", "kimmel-associates"),
        ("https://kimmel-associates.breezy.hr/", "kimmel-associates"),
        (
            "https://kimmel-associates.breezy.hr/p/c2330e43269e-assembly-supervisor",
            "kimmel-associates",
        ),
        (
            "https://kimmel-associates.breezy.hr/p/c2330e43269e-assembly-supervisor/apply",
            "kimmel-associates",
        ),
        ("https://Everstar.breezy.hr/json", "everstar"),
        ("https://everstar.breezy.hr/embed/js?inline=true&group_by=dept", "everstar"),
        ("https://breezy.hr/", None),
        ("https://app.breezy.hr/signin", None),
        ("https://marketing.breezy.hr/", None),
        ("https://www.kimmel.com/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert BreezyConnector.detect(url) == expected


async def test_custom_domain_embed_resolves() -> None:
    # Breezy's embed snippet as pasted into a company's own careers page.
    page = (
        '<div id="breezy-positions"></div><script src="https://everstar.breezy.hr/embed/js'
        '?bzsrc=jsWidget&group_by=dept&link_to_pos=true"></script>'
    )
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("breezy", "everstar")


async def test_fetch_jobs_from_fixture() -> None:
    http, seen = mock_http(
        {
            "kimmel-associates.breezy.hr/json": httpx.Response(
                200, text=load_fixture("breezy", "kimmel_associates_verbose.json")
            ),
            "everstar.breezy.hr/json": httpx.Response(
                200, text=load_fixture("breezy", "everstar_verbose.json")
            ),
        }
    )
    async with http:
        connector = BreezyConnector(http)
        supervisor, automation, _ = await connector.fetch_jobs("kimmel-associates")
        (product,) = await connector.fetch_jobs("everstar")
    feed = next(r for r in seen if r.url.path == "/json")
    assert feed.url.params["verbose"] == "true"  # without it there are no descriptions

    assert (supervisor.external_id, supervisor.title) == ("c2330e43269e", "Assembly Supervisor")
    assert supervisor.company == "Kimmel & Associates"
    assert (supervisor.city, supervisor.region, supervisor.country) == ("Dayton", "OH", "US")
    assert supervisor.employment_type == "full_time"  # "fullTime"
    assert (supervisor.salary_min, supervisor.salary_max) == (Decimal(80000), Decimal(85000))
    assert (supervisor.salary_currency, supervisor.salary_period) == ("USD", "year")
    assert supervisor.posted_at is not None
    assert supervisor.posted_at.isoformat() == "2026-10-19T18:08:27.917000+00:00"
    assert supervisor.description_html
    assert supervisor.source_url == (
        "https://kimmel-associates.breezy.hr/p/c2330e43269e-assembly-supervisor"
    )

    assert automation.remote_type is RemoteType.REMOTE  # remote_details.value "remote"
    assert product.country == "UA"
    assert product.salary_min is None  # salary ""
    assert product.description_html is not None
    assert "EVERSTAR" in product.description_html


async def test_unknown_portal_and_bad_payload() -> None:
    http, _ = mock_http({"x.breezy.hr/json": httpx.Response(200, json={"error": "x"})})
    async with http:
        with pytest.raises(FetchError, match="404"):
            await BreezyConnector(http).fetch_jobs("nosuch")  # 404, never "0 jobs"
        with pytest.raises(ConnectorError):
            await BreezyConnector(http).fetch_jobs("x")


@pytest.mark.live
@pytest.mark.parametrize("company", ["kimmel-associates", "everstar"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await BreezyConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.source_url for j in jobs)
    assert sum(bool(j.description_html) for j in jobs) >= len(jobs) * 0.9
