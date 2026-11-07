"""BambooHR connector.

Fixtures (captured 2026-11-07):
    scribd_list.json        GET https://scribd.bamboohr.com/careers/list (2 openings, on-site)
    kpm_list.json           GET https://kpm.bamboohr.com/careers/list (1 opening, remote,
                            structured atsLocation)
    scribd_detail_144.json  GET https://scribd.bamboohr.com/careers/144/detail
    kpm_detail_128.json     GET https://kpm.bamboohr.com/careers/128/detail (compensation)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.bamboohr import BambooHRConnector
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://scribd.bamboohr.com/careers", "scribd"),
        ("https://scribd.bamboohr.com/careers/", "scribd"),
        ("https://scribd.bamboohr.com/careers/144", "scribd"),
        ("https://scribd.bamboohr.com/careers/144/detail", "scribd"),
        ("https://scribd.bamboohr.com/careers/list", "scribd"),
        ("https://KPM.bamboohr.com/careers/128", "kpm"),  # jobOpeningShareUrl casing
        ("https://kpm.bamboohr.com/jobs/", "kpm"),  # legacy careers page
        ("https://kpm.bamboohr.com/jobs/view.php?id=128", "kpm"),
        ("https://kpm.bamboohr.com/jobs/embed2.php", "kpm"),  # embed script
        ("https://kpm.bamboohr.com", "kpm"),
        ("https://kpm.bamboohr.com/home", None),  # employee login app, not a job board
        ("https://www.bamboohr.com/careers/", None),  # BambooHR's own marketing site
        ("https://api.bamboohr.com/api/gateway.php/kpm/v1/", None),
        ("https://www.kpmanalytics.com/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert BambooHRConnector.detect(url) == expected


async def test_custom_domain_with_embed_resolves() -> None:
    # BambooHR's documented embed code, as pasted into a company's own careers page.
    page = (
        '<div id="BambooHR" data-domain="scribd.bamboohr.com" data-version="1.0.0"></div>'
        '<script src="https://scribd.bamboohr.com/js/embed.js" type="text/javascript" '
        'async defer></script><a href="https://scribd.bamboohr.com/careers">All jobs</a>'
    )
    http, _ = mock_http({"careers.example.com/jobs": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://careers.example.com/jobs", http)
    assert board == ResolvedBoard("bamboohr", "scribd")


def _routes() -> dict[str, httpx.Response]:
    return {
        "scribd.bamboohr.com/careers/list": httpx.Response(
            200, text=load_fixture("bamboohr", "scribd_list.json")
        ),
        "kpm.bamboohr.com/careers/list": httpx.Response(
            200, text=load_fixture("bamboohr", "kpm_list.json")
        ),
        "scribd.bamboohr.com/careers/144/detail": httpx.Response(
            200, text=load_fixture("bamboohr", "scribd_detail_144.json")
        ),
        "kpm.bamboohr.com/careers/128/detail": httpx.Response(
            200, text=load_fixture("bamboohr", "kpm_detail_128.json")
        ),
    }


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http(_routes())
    async with http:
        scribd = await BambooHRConnector(http).fetch_jobs("scribd")
        (kpm,) = await BambooHRConnector(http).fetch_jobs("kpm")

    senior, engineer = scribd
    assert (senior.external_id, senior.title) == ("144", "Senior Software Engineer")
    assert (senior.city, senior.region) == ("Amsterdam", "North Holland")
    assert senior.remote_type is RemoteType.ONSITE  # locationType "0"
    assert senior.employment_type == "full_time"  # "Full-Time Netherlands"
    assert senior.source_url == "https://scribd.bamboohr.com/careers/144"
    assert senior.description_html is None  # detail-only
    assert engineer.location_raw == "San Francisco, California"

    assert (kpm.city, kpm.region, kpm.country) == ("Chicago", "Illinois", "United States")
    assert kpm.remote_type is RemoteType.REMOTE  # locationType "1"


async def test_fetch_job_detail() -> None:
    http, _ = mock_http(_routes())
    async with http:
        connector = BambooHRConnector(http)
        (kpm,) = await connector.fetch_jobs("kpm")
        detailed = await connector.fetch_job_detail("kpm", kpm)
        senior, _ = await connector.fetch_jobs("scribd")
        senior_detailed = await connector.fetch_job_detail("scribd", senior)
    assert detailed is not None
    assert detailed.description_html is not None
    assert "Technical Sales Manager" in detailed.description_html
    assert (detailed.salary_min, detailed.salary_max) == (Decimal(110000), Decimal(120000))
    assert detailed.salary_period == "year"
    assert detailed.posted_at is not None
    assert detailed.posted_at.date().isoformat() == "2026-10-30"
    assert senior_detailed is not None
    assert senior_detailed.country == "Netherlands"  # location.addressCountry, detail only
    assert senior_detailed.salary_min is None  # compensation: null


async def test_closed_job_detail_returns_none() -> None:
    http, _ = mock_http({})  # every path 404, like a closed opening
    async with http:
        job = BambooHRConnector._map_list({"id": "1", "jobOpeningName": "X"}, "scribd")
        assert await BambooHRConnector(http).fetch_job_detail("scribd", job) is None


async def test_unknown_company_html_page_is_an_error_not_zero_jobs() -> None:
    http, _ = mock_http(
        {"nosuch.bamboohr.com/careers/list": httpx.Response(200, text="<html>BambooHR</html>")}
    )
    async with http:
        with pytest.raises(ConnectorError, match="did not return JSON"):
            await BambooHRConnector(http).fetch_jobs("nosuch")


async def test_count_mismatch_is_refused() -> None:
    body = '{"meta": {"totalCount": 3}, "result": [{"id": "1", "jobOpeningName": "A"}]}'
    http, _ = mock_http({"x.bamboohr.com/careers/list": httpx.Response(200, text=body)})
    async with http:
        with pytest.raises(ConnectorError, match="totalCount=3"):
            await BambooHRConnector(http).fetch_jobs("x")


@pytest.mark.live
@pytest.mark.parametrize("company", ["scribd", "kpm"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = BambooHRConnector(http)
        jobs = await connector.fetch_jobs(company)
        assert len(jobs) > 0
        detailed = await connector.fetch_job_detail(company, jobs[0])
    assert all(j.title for j in jobs)
    assert detailed is not None
    assert detailed.description_html
