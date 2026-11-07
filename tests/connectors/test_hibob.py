"""HiBob connector.

Fixtures (captured 2026-11-07, GET /api/job-ad with header ``companyIdentifier``;
filterGroups/jobOpeningMetadata emptied):
    sapiens_job_ad.json    3 of 91 ads from https://sapiens.careers.hibob.com/api/job-ad
                           (annual pay range; one posting location; custom section label)
    liquidia_job_ad.json   1 of 21 ads from https://liquidia.careers.hibob.com/api/job-ad
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.hibob import HiBobConnector
from jobindex.http import FetchError, HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://sapiens.careers.hibob.com/", "sapiens"),
        ("https://Sapiens.careers.hibob.com/jobs/f09fcb8f-d8d0-4608-8aa1-2babf8e53969", "sapiens"),
        ("https://careers.hibob.com/", None),
        ("https://www.hibob.com/careers/", None),
        ("https://a.b.careers.hibob.com/", None),
        ("https://sapiens.com/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert HiBobConnector.detect(url) == expected


def _route(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("hibob", name))


async def test_fetch_jobs_from_fixture() -> None:
    http, seen = mock_http(
        {
            "sapiens.careers.hibob.com/api/job-ad": _route("sapiens_job_ad.json"),
            "liquidia.careers.hibob.com/api/job-ad": _route("liquidia_job_ad.json"),
        }
    )
    async with http:
        paid, located, labelled = await HiBobConnector(http).fetch_jobs("sapiens")
        (liquidia,) = await HiBobConnector(http).fetch_jobs("liquidia")
    assert seen[-1].headers["companyIdentifier"] == "liquidia"

    assert paid.title == "Director of Sales Operations, North America"
    assert (paid.salary_min, paid.salary_max) == (Decimal(150000), Decimal(200000))
    assert (paid.salary_currency, paid.salary_period) == ("USD", "year")
    assert paid.remote_type is RemoteType.REMOTE
    assert paid.posted_at is not None
    assert paid.posted_at.date().isoformat() == "2026-10-28"
    assert paid.source_url == (
        "https://sapiens.careers.hibob.com/jobs/a43005c5-9083-4d11-8668-4a94d9f7a19b"
    )
    assert paid.normalized().country == "US"

    assert (located.location_raw, located.city, located.region) == ("Canada", "Mississauga", "ON")
    assert located.normalized().country == "CA"

    assert labelled.description_html is not None
    assert "<h3>Why Join Sapiens?</h3>" in labelled.description_html
    assert "<h3>Responsibilities</h3>" in labelled.description_html

    assert liquidia.employment_type == "full_time"  # "Regular Full Time"
    assert liquidia.salary_min is None
    assert liquidia.salary_currency is None
    assert liquidia.normalized().description_text


async def test_unknown_company_and_bad_payload() -> None:
    http, _ = mock_http(
        {
            "nosuch.careers.hibob.com/api/job-ad": httpx.Response(401, json={"error": "x"}),
            "odd.careers.hibob.com/api/job-ad": httpx.Response(200, json={"jobs": []}),
            "html.careers.hibob.com/api/job-ad": httpx.Response(200, text="<html></html>"),
        }
    )
    async with http:
        with pytest.raises(FetchError, match="401"):
            await HiBobConnector(http).fetch_jobs("nosuch")
        with pytest.raises(ConnectorError, match="jobAdDetails"):
            await HiBobConnector(http).fetch_jobs("odd")
        with pytest.raises(ConnectorError, match="not JSON"):
            await HiBobConnector(http).fetch_jobs("html")


@pytest.mark.live
@pytest.mark.parametrize("company", ["sapiens", "liquidia"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await HiBobConnector(http).fetch_jobs(company)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
