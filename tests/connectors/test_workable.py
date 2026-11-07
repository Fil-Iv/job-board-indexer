"""Workable connector.

Fixture (captured 2026-11-07, jobs trimmed to 3: remote, Athens, Vienna):
    blueground_widget.json  https://apply.workable.com/api/v1/widget/accounts/blueground?details=true
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.workable import WorkableConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http

API = "apply.workable.com/api/v1/widget/accounts/blueground"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://apply.workable.com/blueground/", "blueground"),
        ("https://apply.workable.com/blueground/j/186545F8C1/", "blueground"),
        ("https://apply.workable.com/blueground/j/186545F8C1/apply", "blueground"),
        ("https://apply.workable.com/Blueground", "blueground"),
        ("https://blueground.workable.com/", "blueground"),
        ("https://blueground.workable.com/j/186545F8C1", "blueground"),
        ("https://apply.workable.com/api/v1/widget/accounts/blueground?details=true", "blueground"),
        ("https://apply.workable.com/api/v3/accounts/blueground/jobs", "blueground"),
        ("https://www.workable.com/api/accounts/blueground", "blueground"),
        ("https://apply.workable.com/j/186545F8C1", None),  # job link without the account
        ("https://www.workable.com/careers", None),
        ("https://jobs.workable.com/view/abc", None),  # Workable's own aggregator
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert WorkableConnector.detect(url) == expected


def test_detect_in_html() -> None:
    html = '<a href="https://apply.workable.com/blueground/">See all jobs</a>'
    assert WorkableConnector.detect_in_html(html) == {"blueground": 1}


async def test_fetch_jobs_from_fixture() -> None:
    http, seen = mock_http(
        {API: httpx.Response(200, text=load_fixture("workable", "blueground_widget.json"))}
    )
    async with http:
        connector = WorkableConnector(http)
        jobs = await connector.fetch_jobs("blueground")
        assert await connector.fetch_company_name("blueground") == "Blueground"

    assert seen[-1].url.params["details"] == "true"
    remote, athens, vienna = jobs
    assert remote.external_id == "407973D643"
    assert remote.company == "Blueground"
    assert remote.remote_type is RemoteType.REMOTE  # telecommuting=true
    assert remote.normalized().country == "BR"

    assert athens.title == "Client Experience Coordinator (English & Greek Speaker) - Talent Pool"
    assert (athens.city, athens.region, athens.country) == ("Athens", "Attica", "GR")
    assert athens.location_raw == "Athens, Attica, Greece"
    assert athens.employment_type == "full_time"
    assert athens.posted_at is not None
    assert athens.posted_at.date().isoformat() == "2026-02-12"
    assert athens.source_url == "https://apply.workable.com/j/186545F8C1"
    assert athens.apply_url == "https://apply.workable.com/j/186545F8C1/apply"
    text = athens.normalized().description_text
    assert text is not None
    assert "Blueground" in text

    assert vienna.normalized().country == "AT"


async def test_unknown_account_is_an_error() -> None:
    http, _ = mock_http({})  # 404 for every path
    async with http:
        with pytest.raises(Exception, match="404"):
            await WorkableConnector(http).fetch_jobs("nope")


async def test_unexpected_payload() -> None:
    http, _ = mock_http({API: httpx.Response(200, json={"name": "x"})})
    async with http:
        with pytest.raises(ConnectorError):
            await WorkableConnector(http).fetch_jobs("blueground")


@pytest.mark.live
@pytest.mark.parametrize("account", ["blueground", "skroutz"])
async def test_live(account: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await WorkableConnector(http).fetch_jobs(account)
    assert len(jobs) > 0
    assert all(j.title and j.source_url for j in jobs)
    assert any(j.description_html for j in jobs)
