"""WP Job Manager connector.

Fixtures (captured 2026-11-07, query ``per_page=100&orderby=id&order=asc&_fields=...`` as
sent by the connector; lists trimmed to a few postings):
    startjob_listings.json    3 of 14 from https://startjob.dk/wp-json/wp/v2/job-listings
                              (one ``_filled``, one with ``_company_name``)
    startjob_job_types.json   https://startjob.dk/wp-json/wp/v2/job-types (Danish names)
    startjob_home.html        <head> excerpt of https://startjob.dk/ (plugin assets, api.w.org)
    fmals_listings_p1/p2.json 2 + 1 of 393 from https://fmals.de/wp-json/wp/v2/job-listings
                              (served below as two pages)
    fmals_job_types.json      https://fmals.de/wp-json/wp/v2/job-types
    cls_listings.json         2 of 29 from https://www.clsrecruitment.ie/wp-json/wp/v2/job-listings
                              (HTML entity in title; e-mail and garbage ``_application``)
    cls_job_types.json        https://www.clsrecruitment.ie/wp-json/wp/v2/job-types
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.connectors.wpjobmanager import WpJobManagerConnector, _salary
from jobindex.http import FetchError, HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


def _listings(fixture: str, total: int, pages: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        text=load_fixture("wpjobmanager", fixture),
        headers={"X-WP-Total": str(total), "X-WP-TotalPages": str(pages)},
    )


def _types(fixture: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("wpjobmanager", fixture))


def test_detect_has_no_url_pattern() -> None:
    assert WpJobManagerConnector.detect("https://startjob.dk/stilling/assistancer/") is None


async def test_resolves_from_page_html() -> None:
    page = load_fixture("wpjobmanager", "startjob_home.html")
    http, _ = mock_http({"www.startjob.dk/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://www.startjob.dk/", http)
    # the REST discovery link names the site root (no www.)
    assert board == ResolvedBoard("wpjobmanager", "startjob.dk")


@pytest.mark.parametrize(
    ("html", "page_url", "expected"),
    [
        (  # WordPress in a subdirectory
            '<link href="/wp-content/plugins/wp-job-manager/x.css">'
            '<link rel="https://api.w.org/" href="https://Example.org/careers/wp-json/" />',
            "https://example.org/careers/jobs/",
            {"example.org/careers": 1},
        ),
        (  # discovery link removed: fall back to the page host
            "<script src='/wp-content/plugins/wp-job-manager/a.js'></script>",
            "https://jobs.example.com/jobs/",
            {"jobs.example.com": 1},
        ),
        (  # plain-permalink site: REST lives at ?rest_route=, unsupported
            '<link href="/wp-content/plugins/wp-job-manager/x.css">'
            "<link rel='https://api.w.org/' href='https://example.org/?rest_route=/' />",
            "https://example.org/",
            {},
        ),
        ("<link rel='https://api.w.org/' href='https://example.org/wp-json/' />", None, {}),
    ],
)
def test_detect_in_html(html: str, page_url: str | None, expected: dict[str, int]) -> None:
    assert WpJobManagerConnector.detect_in_html(html, page_url) == expected


async def test_fetch_jobs_startjob() -> None:
    http, seen = mock_http(
        {
            "startjob.dk/wp-json/wp/v2/job-listings": _listings("startjob_listings.json", 3),
            "startjob.dk/wp-json/wp/v2/job-types": _types("startjob_job_types.json"),
        }
    )
    async with http:
        jobs = await WpJobManagerConnector(http).fetch_jobs("startjob.dk")

    listing_request = seen[-1]
    assert listing_request.url.params["per_page"] == "100"
    assert listing_request.url.params["orderby"] == "id"

    # 15088 is ``_filled`` and dropped
    assert [j.external_id for j in jobs] == ["24301", "24304"]
    zency, window = jobs
    assert zency.title.startswith("Massør, kropsterapeut eller fysioterapeut")
    assert zency.company == "Zency ApS"
    assert zency.location_raw == "Hele Danmark"
    assert zency.employment_type is None  # "Deltidsjob" is not mapped
    assert zency.posted_at is not None
    assert zency.posted_at.isoformat() == "2026-10-15T13:03:30+00:00"
    assert zency.apply_url == "https://www.zency.dk/massoer-job/"
    assert zency.source_url is not None
    assert zency.source_url.startswith("https://startjob.dk/stilling/zency-aps-hele-danmark")
    assert zency.description_html is not None
    assert "<p>" in zency.description_html
    assert zency.remote_type is RemoteType.UNKNOWN
    assert window.company is None  # empty ``_company_name``: source company is used


async def test_fetch_jobs_pages_and_maps_types() -> None:
    pages = {"1": "fmals_listings_p1.json", "2": "fmals_listings_p2.json"}

    def listings(request: httpx.Request) -> httpx.Response:
        return _listings(pages[request.url.params["page"]], total=3, pages=2)

    http, seen = mock_http(
        {
            "fmals.de/wp-json/wp/v2/job-listings": listings,
            "fmals.de/wp-json/wp/v2/job-types": _types("fmals_job_types.json"),
        }
    )
    async with http:
        jobs = await WpJobManagerConnector(http).fetch_jobs("fmals.de")

    assert [r.url.params.get("page") for r in seen if "job-listings" in r.url.path] == ["1", "2"]
    assert [j.external_id for j in jobs] == ["6639", "6641", "6643"]
    first = jobs[0].normalized()
    assert first.employment_type == "full_time"  # "Vollzeit"
    assert (first.city, first.country) == ("Köln", "DE")
    # ``_application`` is an e-mail address: apply on the posting page
    assert first.apply_url == first.source_url


async def test_fetch_jobs_cls_entities_and_bad_application() -> None:
    http, _ = mock_http(
        {
            "www.clsrecruitment.ie/wp-json/wp/v2/job-listings": _listings("cls_listings.json", 2),
            "www.clsrecruitment.ie/wp-json/wp/v2/job-types": _types("cls_job_types.json"),
        }
    )
    async with http:
        manager, groundworker = await WpJobManagerConnector(http).fetch_jobs(
            "www.clsrecruitment.ie"
        )
    assert manager.title == "Contracts Manager – Dublin Area"  # &#8211;
    assert manager.employment_type == "full_time"
    assert groundworker.employment_type is None
    assert groundworker.apply_url == groundworker.source_url  # "http://,person@..." ignored


@pytest.mark.parametrize(
    ("total", "second_page_total", "match"),
    [
        (4, 4, "got 3 listings"),  # a page came back short
        (3, 2, "X-WP-Total changed"),  # a listing vanished while paging
    ],
)
async def test_incomplete_listing_raises(total: int, second_page_total: int, match: str) -> None:
    def listings(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return _listings("fmals_listings_p1.json", total=total, pages=2)
        return _listings("fmals_listings_p2.json", total=second_page_total, pages=2)

    http, _ = mock_http(
        {
            "fmals.de/wp-json/wp/v2/job-listings": listings,
            "fmals.de/wp-json/wp/v2/job-types": _types("fmals_job_types.json"),
        }
    )
    async with http:
        with pytest.raises(ConnectorError, match=match):
            await WpJobManagerConnector(http).fetch_jobs("fmals.de")


async def test_failed_page_raises() -> None:
    def listings(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return _listings("fmals_listings_p1.json", total=3, pages=2)
        return httpx.Response(500)

    http, _ = mock_http(
        {
            "fmals.de/wp-json/wp/v2/job-listings": listings,
            "fmals.de/wp-json/wp/v2/job-types": _types("fmals_job_types.json"),
        }
    )
    async with http:
        with pytest.raises(FetchError):
            await WpJobManagerConnector(http).fetch_jobs("fmals.de")


async def test_rest_api_disabled_raises() -> None:
    http, _ = mock_http({})  # kiwiconseil.fr: 404 on /wp-json/wp/v2/...
    async with http:
        with pytest.raises(FetchError):
            await WpJobManagerConnector(http).fetch_jobs("kiwiconseil.fr")


async def test_missing_total_header_raises() -> None:
    http, _ = mock_http(
        {
            "fmals.de/wp-json/wp/v2/job-listings": httpx.Response(
                200, text=load_fixture("wpjobmanager", "fmals_listings_p1.json")
            ),
            "fmals.de/wp-json/wp/v2/job-types": _types("fmals_job_types.json"),
        }
    )
    async with http:
        with pytest.raises(ConnectorError, match="X-WP-Total"):
            await WpJobManagerConnector(http).fetch_jobs("fmals.de")


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({}, None),
        (
            {"_job_salary": "45000", "_job_salary_currency": "eur", "_job_salary_unit": "YEAR"},
            (Decimal(45000), Decimal(45000), "EUR", "year"),
        ),
        ({"_job_salary": "0"}, None),
        (
            {"_job_salary": "$20 - $25", "_job_salary_unit": "HOUR"},
            (Decimal(20), Decimal(25), "USD", "hour"),
        ),
    ],
)
def test_salary_meta(
    meta: dict[str, str], expected: tuple[Decimal, Decimal, str, str] | None
) -> None:
    salary = _salary(meta)
    got = (salary.min, salary.max, salary.currency, salary.period) if salary else None
    assert got == expected


@pytest.mark.live
@pytest.mark.parametrize("site", ["startjob.dk", "fmals.de"])
async def test_live(site: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await WpJobManagerConnector(http).fetch_jobs(site)
    assert jobs
    assert all(j.title and j.source_url for j in jobs)
