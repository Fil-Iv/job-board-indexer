"""Personio connector.

Fixtures (captured 2026-11-07):
    tado.xml       GET https://tado.jobs.personio.de/xml?language=en (both positions;
                   English account, one with salaryInformation)
    thermondo.xml  GET https://thermondo.jobs.personio.de/xml (2 of 196 positions kept
                   verbatim; German, one with additionalOffices)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.personio import PersonioConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import HttpClient, RobotsDisallowedError
from tests.fakes import load_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://tado.jobs.personio.de", "tado"),
        ("https://tado.jobs.personio.de/", "tado"),
        ("https://tado.jobs.personio.de/job/2502124", "tado"),
        ("https://tado.jobs.personio.de/job/2502124?language=en&display=en#apply", "tado"),
        ("https://tado.jobs.personio.de/xml?language=en", "tado"),
        ("https://prodevelop.jobs.personio.com/", "prodevelop"),
        ("https://Thermondo.jobs.personio.de/", "thermondo"),
        ("https://www.personio.de/", None),
        ("https://tado.personio.de/", None),  # the HR app, not the job page
        ("https://www.tado.com/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert PersonioConnector.detect(url) == expected


async def test_custom_domain_iframe_resolves() -> None:
    page = '<iframe src="https://tado.jobs.personio.de/?display=en" title="Jobs"></iframe>'
    http, _ = mock_http({"careers.example.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/", http)
    assert board == ResolvedBoard("personio", "tado")


def _routes() -> dict[str, httpx.Response]:
    return {
        f"{c}.jobs.personio.de/xml": httpx.Response(
            200, text=load_fixture("personio", f"{c}.xml"), headers={"content-type": "text/xml"}
        )
        for c in ("tado", "thermondo")
    }


async def test_fetch_jobs_from_fixture() -> None:
    http, seen = mock_http(_routes())
    async with http:
        lead, student = await PersonioConnector(http).fetch_jobs("tado")
        mechanic, mobile = await PersonioConnector(http).fetch_jobs("thermondo")
    feed = next(r for r in seen if r.url.path == "/xml")
    assert "language" not in feed.url.params  # see module docstring

    assert (lead.external_id, lead.title) == ("2502124", "Team Lead Accounting (m/f/d)")
    assert lead.company == "tado GmbH"
    assert lead.location_raw == "Germany - Munich"
    assert lead.employment_type == "full_time"  # permanent + full-time
    assert (lead.salary_min, lead.salary_max) == (Decimal(80000), Decimal(100000))
    assert (lead.salary_currency, lead.salary_period) == ("EUR", "year")
    assert lead.posted_at is not None
    assert lead.posted_at.isoformat() == "2026-01-22T09:48:25+00:00"
    assert lead.description_html is not None
    assert "<h3>What you’ll be up to</h3>" in lead.description_html
    assert lead.source_url == "https://tado.jobs.personio.de/job/2502124"

    assert student.employment_type == "internship"  # employmentType "intern"
    assert student.salary_min is None
    assert student.description_html is None  # <jobDescriptions></jobDescriptions>

    assert mechanic.description_html  # German account: described without ?language=
    assert mobile.location_raw == "Bremen; Hamburg"  # office + additionalOffices


async def test_unknown_company_is_refused_by_robots() -> None:
    # Unknown subdomains serve this robots.txt (verified 2026-11-07).
    http, _ = mock_http({}, robots="User-agent: *\nDisallow: /\n")
    async with http:
        with pytest.raises(RobotsDisallowedError):
            await PersonioConnector(http).fetch_jobs("nosuch")


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>maintenance</body></html>",
        "not xml at all",
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><workzag-jobs>&a;</workzag-jobs>',
    ],
)
async def test_bad_feed_is_refused(body: str) -> None:
    http, _ = mock_http({"x.jobs.personio.de/xml": httpx.Response(200, text=body)})
    async with http:
        # Wrong root, invalid XML, or entity declarations (refused by defusedxml).
        with pytest.raises(ConnectorError):
            await PersonioConnector(http).fetch_jobs("x")


@pytest.mark.live
@pytest.mark.parametrize("company", ["tado", "thermondo"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await PersonioConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.source_url for j in jobs)
    assert sum(bool(j.description_html) for j in jobs) >= len(jobs) * 0.5
