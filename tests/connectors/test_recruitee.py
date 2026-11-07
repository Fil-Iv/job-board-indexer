"""Recruitee connector.

Fixtures (captured 2026-11-07):
    offers.json             1 offer from https://bunq.recruitee.com/api/offers/ and 2 from
                            https://channable.recruitee.com/api/offers/ (monthly salary; "0" salary)
    careers_bunq_com.html   excerpt of https://careers.bunq.com/ (custom domain, 873 KB page)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.recruitee import RecruiteeConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bunq.recruitee.com", "bunq"),
        ("https://bunq.recruitee.com/o/aml-manager-austria", "bunq"),
        ("https://bunq.recruitee.com/o/aml-manager-austria/c/new", "bunq"),
        ("https://bunq.recruitee.com/api/offers/", "bunq"),
        ("https://Channable.recruitee.com/", "channable"),
        ("https://recruitee.com/", None),
        ("https://www.recruitee.com/pricing", None),
        ("https://app.recruitee.com/", None),
        ("https://careers.bunq.com/", None),  # custom domain: needs the page (see below)
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert RecruiteeConnector.detect(url) == expected


async def test_custom_domain_resolves_via_page_html() -> None:
    page = load_fixture("recruitee", "careers_bunq_com.html")
    http, _ = mock_http({"careers.bunq.com/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.bunq.com/", http)
    assert board == ResolvedBoard("recruitee", "bunq")


async def test_fetch_jobs_from_fixture() -> None:
    offers = load_fixture("recruitee", "offers.json")
    http, seen = mock_http({"bunq.recruitee.com/api/offers/": httpx.Response(200, text=offers)})
    async with http:
        connector = RecruiteeConnector(http)
        jobs = await connector.fetch_jobs("bunq")
        assert await connector.fetch_company_name("bunq") == "bunq"
    assert seen[-1].url.path == "/api/offers/"

    aml, writer, open_application = jobs
    assert aml.external_id == "2804401"
    assert aml.title == "AML Manager, Austria"
    assert (aml.city, aml.region, aml.country) == ("Vienna", "Wien", "AT")
    assert aml.remote_type is RemoteType.HYBRID
    assert aml.employment_type == "full_time"  # "fulltime_permanent"
    assert aml.posted_at is not None
    assert aml.posted_at.isoformat() == "2026-10-30T16:32:58+00:00"
    assert aml.source_url == "https://careers.bunq.com/o/aml-manager-austria"
    assert aml.apply_url == "https://careers.bunq.com/o/aml-manager-austria/c/new"
    assert aml.description_html is not None  # description + requirements

    assert writer.company == "Channable"
    assert (writer.salary_min, writer.salary_max) == (Decimal(2976), Decimal(4252))
    assert (writer.salary_currency, writer.salary_period) == ("EUR", "month")
    assert writer.employment_type == "temporary"  # "fulltime_fixed_term"

    # {"min": "0", "max": "0", "currency": "DKK"} means "not published".
    assert open_application.salary_min is None
    assert open_application.salary_currency is None


@pytest.mark.live
@pytest.mark.parametrize("company", ["bunq", "channable"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await RecruiteeConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.description_html for j in jobs)


@pytest.mark.live
async def test_live_custom_domain() -> None:
    async with HttpClient.from_settings() as http:
        board = await default_registry().resolve_url("https://careers.bunq.com/", http)
    assert board == ResolvedBoard("recruitee", "bunq")
