"""SmartRecruiters connector — Posting API path (disabled by default) and career-page path.

Fixtures (captured 2026-11-07):
    careers_ubisoft2_page0.html               https://careers.smartrecruiters.com/Ubisoft2
    careers_ubisoft2_api_groups_page1.html    .../Ubisoft2/api/groups?page=1
    careers_ubisoft2_api_more_page{0,1,13}.html  .../Ubisoft2/api/more?page=N (13 = past the end)
    job_744000160228509.html                  https://jobs.smartrecruiters.com/Ubisoft2/744000160228509
API path:
    ubisoft_postings.json                https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings?limit=2
                                         (``totalFound`` adjusted to 2)
    ubisoft_posting_744000160228509.json https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings/744000160228509
"""

from typing import Any

import httpx
import pytest

from jobindex.connectors import smartrecruiters
from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.smartrecruiters import SmartRecruitersConnector
from jobindex.http import HttpClient, RobotsDisallowedError
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

API = "api.smartrecruiters.com/v1/companies/Ubisoft2/postings"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.smartrecruiters.com/Ubisoft2", "Ubisoft2"),
        ("https://jobs.smartrecruiters.com/Ubisoft2/744000160228509-superviseur-e", "Ubisoft2"),
        ("https://careers.smartrecruiters.com/BoschGroup", "BoschGroup"),
        ("https://careers.smartrecruiters.com/BoschGroup/students", "BoschGroup"),
        (
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Ubisoft2/publication/abc",
            "Ubisoft2",
        ),
        ("https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings?limit=1", "Ubisoft2"),
        ("https://jobs.smartrecruiters.com/", None),
        ("https://www.smartrecruiters.com/resources", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert SmartRecruitersConnector.detect(url) == expected


def test_detect_in_html() -> None:
    widget = """<script src="https://static.smartrecruiters.com/job-widget/1.6.1/script/smart_widget.min.js"></script>
                <script>widget({"company_code": "Ubisoft2", "bg_color_widget": "#fff"});</script>"""
    assert SmartRecruitersConnector.detect_in_html(widget) == {"Ubisoft2": 1}
    # "company_code" without any SmartRecruiters reference is ignored.
    assert SmartRecruitersConnector.detect_in_html('{"company_code": "x"}') == {}


def _routes() -> dict[str, httpx.Response]:
    return {
        API: httpx.Response(200, text=load_fixture("smartrecruiters", "ubisoft_postings.json")),
        f"{API}/744000160228509": httpx.Response(
            200, text=load_fixture("smartrecruiters", "ubisoft_posting_744000160228509.json")
        ),
        # 744000160226335 is not routed -> 404, i.e. unpublished meanwhile.
    }


async def test_fetch_jobs_and_details() -> None:
    http, _ = mock_http(_routes())
    async with http:
        connector = SmartRecruitersConnector(http, use_api=True)
        jobs = await connector.fetch_jobs("Ubisoft2")
        assert [j.external_id for j in jobs] == ["744000160228509", "744000160226335"]

        montreal, bordeaux = jobs
        assert montreal.title == "Superviseur·e Texture & LookDev - Hybride VFX"
        assert montreal.company == "Ubisoft"
        assert (montreal.city, montreal.region, montreal.country) == ("Montreal", "QC", "CA")
        assert montreal.remote_type is RemoteType.ONSITE  # remote=false, hybrid=false
        assert montreal.employment_type == "full_time"
        assert montreal.description_html is None  # list endpoint has no description
        assert bordeaux.country == "FR"

        detailed = await connector.fetch_job_detail("Ubisoft2", montreal)
        assert detailed is not None
        assert detailed.description_html is not None
        assert "<h3>Description du poste</h3>" in detailed.description_html
        assert detailed.apply_url is not None
        assert detailed.apply_url.startswith("https://jobs.smartrecruiters.com/Ubisoft2/")
        text = detailed.normalized().description_text
        assert text is not None
        assert "Hybride" in text

        assert await connector.fetch_job_detail("Ubisoft2", bordeaux) is None


async def test_pagination_and_incomplete_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smartrecruiters, "PAGE_SIZE", 1)
    body = load_json_fixture("smartrecruiters", "ubisoft_postings.json")

    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={**body, "content": body["content"][offset : offset + 1]})

    http, seen = mock_http({API: page})
    async with http:
        jobs = await SmartRecruitersConnector(http, use_api=True).fetch_jobs("Ubisoft2")
    assert len(jobs) == 2
    assert [r.url.params["offset"] for r in seen if r.url.path != "/robots.txt"] == ["0", "1"]

    short = {**body, "totalFound": 5}
    http, _ = mock_http({API: httpx.Response(200, json=short)})
    monkeypatch.setattr(smartrecruiters, "PAGE_SIZE", 100)
    async with http:
        with pytest.raises(ConnectorError, match="got 2 of 5"):
            await SmartRecruitersConnector(http, use_api=True).fetch_jobs("Ubisoft2")


async def test_robots_txt_is_respected() -> None:
    # The real api.smartrecruiters.com/robots.txt (2026-11-07).
    robots = "User-agent: LinkedInBot\nAllow: /v1/companies/\nUser-agent: *\nDisallow: /\n"
    http, seen = mock_http(_routes(), robots=robots)
    async with http:
        with pytest.raises(RobotsDisallowedError):
            await SmartRecruitersConnector(http, use_api=True).fetch_jobs("Ubisoft2")
    assert [r.url.path for r in seen] == ["/robots.txt"]


# -- career-page path (default) ---------------------------------------------------

CAREERS = "careers.smartrecruiters.com/Ubisoft2"
WWW_ROBOTS = "www.smartrecruiters.com/robots.txt"
REAL_WWW_ROBOTS = (
    "User-agent: *\nDisallow: /BoschGroup/*\nDisallow: /Ubisoft/*\nDisallow: /search/\n"
)


def _html_routes(*, groups_pages: int = 1, flat_pages: int = 2) -> dict[str, Any]:
    page0 = load_fixture("smartrecruiters", "careers_ubisoft2_page0.html")
    # The real board has 5 group pages (298 jobs); serving page 0 alone -> expected = 43.
    page0 = page0.replace('data-groups-pages="5"', f'data-groups-pages="{groups_pages}"')
    flat = [
        load_fixture("smartrecruiters", f"careers_ubisoft2_api_more_page{p}.html") for p in (0, 1)
    ]

    def more(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, text=flat[page] if page < flat_pages else "")

    return {
        WWW_ROBOTS: httpx.Response(200, text=REAL_WWW_ROBOTS),
        f"{CAREERS}": httpx.Response(200, text=page0),
        f"{CAREERS}/api/groups": httpx.Response(
            200, text=load_fixture("smartrecruiters", "careers_ubisoft2_api_groups_page1.html")
        ),
        f"{CAREERS}/api/more": more,
        "jobs.smartrecruiters.com/Ubisoft2/744000160228509": httpx.Response(
            200, text=load_fixture("smartrecruiters", "job_744000160228509.html")
        ),
    }


def test_html_path_is_the_default() -> None:
    http, _ = mock_http({})
    assert SmartRecruitersConnector(http).use_api is False


async def test_html_listing() -> None:
    http, seen = mock_http(_html_routes())
    async with http:
        jobs = await SmartRecruitersConnector(http).fetch_jobs("Ubisoft2")
    assert len(jobs) == 49 + 24  # flat pages 0 and 1, then an empty page
    assert len({j.external_id for j in jobs}) == len(jobs)
    first = next(j for j in jobs if j.external_id == "744000160228509")
    assert first.title == "Superviseur·e Texture & LookDev - Hybride VFX"
    assert first.source_url == "https://jobs.smartrecruiters.com/Ubisoft2/744000160228509"
    assert first.description_html is None  # from the detail page
    hosts = {r.url.host for r in seen}
    assert "api.smartrecruiters.com" not in hosts  # the disallowed API is never touched


async def test_html_listing_short_of_group_total_is_refused() -> None:
    # Group counts promise 43 + 29 (two group pages) = 72 jobs; the flat list gives 49.
    http, _ = mock_http(_html_routes(groups_pages=2, flat_pages=1))
    async with http:
        with pytest.raises(ConnectorError, match="got 49 of"):
            await SmartRecruitersConnector(http).fetch_jobs("Ubisoft2")


async def test_company_opt_out_on_www_is_respected() -> None:
    http, seen = mock_http(_html_routes())
    async with http:
        with pytest.raises(RobotsDisallowedError, match="Disallow: /BoschGroup/"):
            await SmartRecruitersConnector(http).fetch_jobs("BoschGroup")
    assert all(r.url.host == "www.smartrecruiters.com" for r in seen)  # nothing else fetched


async def test_html_detail_from_schemaorg_microdata() -> None:
    http, _ = mock_http(_html_routes())
    async with http:
        connector = SmartRecruitersConnector(http)
        listed = RawJob(
            external_id="744000160228509",
            title="Superviseur·e Texture & LookDev - Hybride VFX",
            source_url="https://jobs.smartrecruiters.com/Ubisoft2/744000160228509",
        )
        job = await connector.fetch_job_detail("Ubisoft2", listed)
        gone = await connector.fetch_job_detail("Ubisoft2", RawJob(external_id="1", title="x"))
        assert await connector.fetch_company_name("Ubisoft2") == "Ubisoft"
    assert gone is None  # 404
    assert job is not None
    assert job.company == "Ubisoft"
    assert (job.city, job.region, job.country) == ("Montreal", "Québec", "Canada")
    assert job.remote_type is RemoteType.ONSITE  # <spl-job-location workplaceType="on_site">
    assert job.employment_type == "full_time"  # "Temps plein"
    assert job.posted_at is not None
    assert job.posted_at.isoformat() == "2026-11-06T15:01:26.091000+00:00"
    assert job.description_html is not None
    n = job.normalized()
    assert (n.city, n.region, n.country) == ("Montreal", "QC", "CA")
    assert n.description_text is not None
    assert n.description_text.startswith("Description de l'entreprise")


@pytest.mark.live
@pytest.mark.parametrize("company", ["Ubisoft2", "Wise"])
async def test_live_html(company: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = SmartRecruitersConnector(http)
        jobs = await connector.fetch_jobs(company)
        assert len(jobs) > 0
        detailed = await connector.fetch_job_detail(company, jobs[0])
    assert detailed is not None
    assert detailed.description_html
    assert detailed.posted_at is not None


@pytest.mark.live
async def test_live_opted_out_company_is_refused() -> None:
    async with HttpClient.from_settings() as http:
        with pytest.raises(RobotsDisallowedError, match="BoschGroup"):
            await SmartRecruitersConnector(http).fetch_jobs("BoschGroup")
