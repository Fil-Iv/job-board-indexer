"""Greenhouse connector.

Fixtures (captured 2026-11-07, trimmed to 3 postings, ``meta.total`` adjusted):
    discord_jobs.json   https://boards-api.greenhouse.io/v1/boards/discord/jobs?content=true&pay_transparency=true
    discord_board.json  https://boards-api.greenhouse.io/v1/boards/discord
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.greenhouse import GreenhouseConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

API = "boards-api.greenhouse.io/v1/boards/discord"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/discord", "discord"),
        ("https://boards.greenhouse.io/discord/jobs/8917177002", "discord"),
        ("https://job-boards.greenhouse.io/Discord/jobs/8917177002?gh_src=x", "discord"),
        ("https://job-boards.eu.greenhouse.io/acme", "acme"),
        ("https://boards.eu.greenhouse.io/acme/jobs/1", "acme"),
        ("https://boards.greenhouse.io/embed/job_board?for=discord&b=https://x", "discord"),
        ("https://boards.greenhouse.io/embed/job_board/js?for=discord", "discord"),
        ("https://boards.greenhouse.io/embed/job_app?for=discord&token=8917177002", "discord"),
        ("https://job-boards.greenhouse.io/embed/job_board?for=discord", "discord"),
        ("https://boards-api.greenhouse.io/v1/boards/discord/jobs", "discord"),
        ("https://api.greenhouse.io/v1/boards/discord/embed/jobs", "discord"),
        ("boards.greenhouse.io/discord", "discord"),
        ("https://boards.greenhouse.io/embed/job_board", None),
        ("https://www.greenhouse.io/careers", None),
        ("https://jobs.lever.co/discord", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert GreenhouseConnector.detect(url) == expected


def test_detect_in_html() -> None:
    html = """
      <div id="grnhse_app"></div>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>
    """
    assert GreenhouseConnector.detect_in_html(html) == {"acme": 1}
    settings_only = "<script>Grnhse.Settings.boardToken = 'acme2';</script>"
    assert GreenhouseConnector.detect_in_html(settings_only) == {"acme2": 1}


async def test_fetch_jobs_from_fixture() -> None:
    http, seen = mock_http(
        {f"{API}/jobs": httpx.Response(200, text=load_fixture("greenhouse", "discord_jobs.json"))}
    )
    async with http:
        jobs = await GreenhouseConnector(http).fetch_jobs("discord")

    assert seen[-1].url.params["content"] == "true"
    assert seen[-1].url.params["pay_transparency"] == "true"
    assert [j.external_id for j in jobs] == ["8917177002", "8722538002", "8933480002"]

    counsel = jobs[0]
    assert counsel.title == "Associate Commercial Counsel"
    assert counsel.company == "Discord"
    assert counsel.description_html is not None
    assert counsel.description_html.startswith("<div")  # entity-escaped HTML was unescaped
    assert counsel.location_raw == "San Francisco Bay Area or Remote (U.S.)"
    assert (counsel.city, counsel.region, counsel.country) == ("San Francisco", "CA", "US")
    assert (counsel.salary_min, counsel.salary_max) == (Decimal(200000), Decimal(225000))
    assert counsel.salary_currency == "USD"
    assert counsel.posted_at is not None
    assert counsel.source_url == "https://job-boards.greenhouse.io/discord/jobs/8917177002"

    n = counsel.normalized()
    assert n.remote_type is RemoteType.REMOTE
    assert n.salary_period == "year"
    assert n.description_text is not None
    assert n.description_text.startswith("Discord has a highly engaged community")
    assert "<" not in n.description_text

    designer = jobs[2].normalized()
    assert designer.salary_min is None  # no pay range published
    assert designer.remote_type is RemoteType.UNKNOWN


async def test_rejects_inconsistent_total() -> None:
    body = load_json_fixture("greenhouse", "discord_jobs.json")
    body["meta"]["total"] = 99
    http, _ = mock_http({f"{API}/jobs": httpx.Response(200, json=body)})
    async with http:
        with pytest.raises(ConnectorError, match=r"meta\.total"):
            await GreenhouseConnector(http).fetch_jobs("discord")


async def test_fetch_company_name() -> None:
    http, _ = mock_http(
        {API: httpx.Response(200, text=load_fixture("greenhouse", "discord_board.json"))}
    )
    async with http:
        assert await GreenhouseConnector(http).fetch_company_name("discord") == "Discord"


@pytest.mark.live
@pytest.mark.parametrize("token", ["discord", "gitlab"])
async def test_live(token: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await GreenhouseConnector(http).fetch_jobs(token)
    assert len(jobs) > 0
    assert all(j.external_id and j.title for j in jobs)
    assert any(j.description_html for j in jobs)
