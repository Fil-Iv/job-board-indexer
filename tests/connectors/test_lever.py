"""Lever connector.

Fixture (captured 2026-11-07, trimmed to 3 postings: hybrid, remote, onsite):
    spotify_postings.json  https://api.lever.co/v0/postings/spotify?mode=json
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors import lever
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.lever import LeverConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_json_fixture, mock_http

API = "api.lever.co/v0/postings/spotify"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.lever.co/spotify", "spotify"),
        ("https://jobs.lever.co/spotify/2a55d6c9-c564-443a-be3e-a3851db5a93b", "spotify"),
        ("https://jobs.lever.co/spotify/2a55d6c9-c564-443a-be3e-a3851db5a93b/apply", "spotify"),
        ("https://jobs.lever.co/spotify?team=Engineering", "spotify"),
        ("https://api.lever.co/v0/postings/spotify?mode=json", "spotify"),
        ("https://jobs.eu.lever.co/acme", "eu:acme"),
        ("https://api.eu.lever.co/v0/postings/acme", "eu:acme"),
        ("https://jobs.lever.co/", None),
        ("https://www.lever.co/careers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert LeverConnector.detect(url) == expected


def test_detect_in_html() -> None:
    embed = """<script>window.leverJobsOptions = {accountName: 'acme', includeCss: true};</script>
               <script src="https://andreasmb.github.io/lever-jobs-embed/index.js"></script>"""
    assert LeverConnector.detect_in_html(embed) == {"acme": 1}
    links = (
        '<a href="https://jobs.lever.co/acme/123">Apply</a><a href="//jobs.lever.co/acme">All</a>'
    )
    assert LeverConnector.detect_in_html(links) == {"acme": 2}


def _paged(postings: list[dict[str, object]]) -> httpx.MockTransport | object:
    def handle(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["skip"])
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=postings[skip : skip + limit])

    return handle


async def test_fetch_jobs_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lever, "PAGE_SIZE", 2)
    postings = load_json_fixture("lever", "spotify_postings.json")
    http, seen = mock_http({API: _paged(postings)})  # type: ignore[dict-item]
    async with http:
        jobs = await LeverConnector(http).fetch_jobs("spotify")

    api_calls = [r for r in seen if r.url.path != "/robots.txt"]
    assert [r.url.params["skip"] for r in api_calls] == ["0", "2"]
    assert len(jobs) == 3

    hybrid, remote, onsite = jobs
    assert hybrid.title == "Account Executive - Commercial, Spotify Technology"
    assert hybrid.remote_type is RemoteType.HYBRID
    assert remote.remote_type is RemoteType.REMOTE
    assert onsite.remote_type is RemoteType.ONSITE
    assert hybrid.employment_type == "full_time"  # "Permanent"
    assert hybrid.country == "US"
    assert hybrid.location_raw == "New York, NY"
    assert hybrid.apply_url == (
        "https://jobs.lever.co/spotify/2a55d6c9-c564-443a-be3e-a3851db5a93b/apply"
    )
    assert hybrid.posted_at is not None
    assert hybrid.description_html is not None
    assert "<h3>What You'll Do</h3>" in hybrid.description_html

    n = hybrid.normalized()
    assert (n.city, n.region, n.country) == ("New York", "NY", "US")
    # No structured salaryRange: found in the "base range" sentence of the description.
    assert (n.salary_min, n.salary_max, n.salary_currency, n.salary_period) == (
        Decimal(87069),
        Decimal(124385),
        "USD",
        "year",
    )


async def test_pagination_that_repeats_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lever, "PAGE_SIZE", 2)
    postings = load_json_fixture("lever", "spotify_postings.json")[:2]
    http, _ = mock_http({API: httpx.Response(200, json=postings)})  # ignores skip
    async with http:
        with pytest.raises(ConnectorError, match="repeated"):
            await LeverConnector(http).fetch_jobs("spotify")


async def test_eu_board_uses_eu_api() -> None:
    http, _ = mock_http({"api.eu.lever.co/v0/postings/acme": httpx.Response(200, json=[])})
    async with http:
        assert await LeverConnector(http).fetch_jobs("eu:acme") == []
    assert LeverConnector.board_url("eu:acme") == "https://jobs.eu.lever.co/acme"


@pytest.mark.live
@pytest.mark.parametrize("site", ["spotify", "palantir"])
async def test_live(site: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await LeverConnector(http).fetch_jobs(site)
    assert len(jobs) > 0
    assert len({j.external_id for j in jobs}) == len(jobs)
    assert all(j.title and j.source_url for j in jobs)
