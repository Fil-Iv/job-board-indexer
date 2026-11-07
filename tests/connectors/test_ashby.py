"""Ashby connector.

Fixture (captured 2026-11-07, trimmed to 3 jobs: hybrid+salary, remote+salary, onsite):
    ramp_job_board.json  https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.ashby import AshbyConnector
from jobindex.connectors.base import ConnectorError
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

API = "api.ashbyhq.com/posting-api/job-board/ramp"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.ashbyhq.com/ramp", "ramp"),
        ("https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245", "ramp"),
        ("https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245/application", "ramp"),
        ("https://jobs.ashbyhq.com/ramp/embed?version=2", "ramp"),
        ("https://jobs.ashbyhq.com/Acme%20Labs", "Acme Labs"),
        ("https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true", "ramp"),
        ("https://jobs.ashbyhq.com/api/non-user-graphql", None),
        ("https://www.ashbyhq.com/customers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert AshbyConnector.detect(url) == expected


def test_detect_in_html_with_json_escaped_urls() -> None:
    # Shape seen on linear.app/careers: URLs inside inline JSON, with escaped quotes.
    html = (
        '<script>self.__next_f.push([1,"{\\"href\\":\\"https://jobs.ashbyhq.com/linear/'
        'f272fc4a-91a7-4dcf-9cb5-3600ebaf14e1\\"},{\\"href\\":\\"https:\\/\\/jobs.ashbyhq.com'
        '\\/linear\\/f16dd5e9\\/application\\"}"])</script>'
    )
    assert AshbyConnector.detect_in_html(html) == {"linear": 2}
    embed = '<script src="https://jobs.ashbyhq.com/acme/embed?version=2"></script>'
    assert AshbyConnector.detect_in_html(embed) == {"acme": 1}


async def test_fetch_jobs_from_fixture() -> None:
    http, seen = mock_http(
        {API: httpx.Response(200, text=load_fixture("ashby", "ramp_job_board.json"))}
    )
    async with http:
        jobs = await AshbyConnector(http).fetch_jobs("ramp")

    assert seen[-1].url.params["includeCompensation"] == "true"
    security, account_manager, account_exec = jobs

    assert security.title == "Security Engineer, Cloud"
    assert security.remote_type is RemoteType.HYBRID
    assert security.employment_type == "full_time"
    assert (security.salary_min, security.salary_max) == (Decimal(211400), Decimal(290600))
    assert (security.salary_currency, security.salary_period) == ("USD", "year")
    assert security.location_raw is not None
    assert security.location_raw.startswith("New York, NY (HQ); Remote (Canada)")
    assert security.description_text is not None  # descriptionPlain is used as-is
    assert security.apply_url is not None
    assert security.apply_url.endswith("/application")

    n = security.normalized()
    assert (n.city, n.region, n.country) == ("New York City", "NY", "US")  # "USA" -> "US"

    assert account_manager.remote_type is RemoteType.REMOTE

    london = account_exec.normalized()
    assert london.remote_type is RemoteType.ONSITE
    assert (london.city, london.country) == ("London", "GB")
    # The description mentions "Fertility HRA (up to $10,000 per year)": not a salary.
    assert london.salary_min is None


async def test_unlisted_jobs_are_skipped() -> None:
    body = load_json_fixture("ashby", "ramp_job_board.json")
    body["jobs"][0]["isListed"] = False
    http, _ = mock_http({API: httpx.Response(200, json=body)})
    async with http:
        jobs = await AshbyConnector(http).fetch_jobs("ramp")
    assert len(jobs) == 2


async def test_unexpected_payload() -> None:
    http, _ = mock_http({API: httpx.Response(200, json={"error": "nope"})})
    async with http:
        with pytest.raises(ConnectorError):
            await AshbyConnector(http).fetch_jobs("ramp")


@pytest.mark.live
@pytest.mark.parametrize("name", ["linear", "ramp"])
async def test_live(name: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await AshbyConnector(http).fetch_jobs(name)
    assert len(jobs) > 0
    assert all(j.title and j.description_html for j in jobs)
