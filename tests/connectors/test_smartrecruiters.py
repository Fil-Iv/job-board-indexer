"""SmartRecruiters connector.

Fixtures (captured 2026-11-07):
    ubisoft_postings.json                https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings?limit=2
                                         (``totalFound`` adjusted to 2)
    ubisoft_posting_744000160228509.json https://api.smartrecruiters.com/v1/companies/Ubisoft2/postings/744000160228509
"""

import httpx
import pytest

from jobindex.connectors import smartrecruiters
from jobindex.connectors.base import ConnectorError
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
        connector = SmartRecruitersConnector(http)
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
        jobs = await SmartRecruitersConnector(http).fetch_jobs("Ubisoft2")
    assert len(jobs) == 2
    assert [r.url.params["offset"] for r in seen if r.url.path != "/robots.txt"] == ["0", "1"]

    short = {**body, "totalFound": 5}
    http, _ = mock_http({API: httpx.Response(200, json=short)})
    monkeypatch.setattr(smartrecruiters, "PAGE_SIZE", 100)
    async with http:
        with pytest.raises(ConnectorError, match="got 2 of 5"):
            await SmartRecruitersConnector(http).fetch_jobs("Ubisoft2")


async def test_robots_txt_is_respected() -> None:
    # The real api.smartrecruiters.com/robots.txt (2026-11-07).
    robots = "User-agent: LinkedInBot\nAllow: /v1/companies/\nUser-agent: *\nDisallow: /\n"
    http, seen = mock_http(_routes(), robots=robots)
    async with http:
        with pytest.raises(RobotsDisallowedError):
            await SmartRecruitersConnector(http).fetch_jobs("Ubisoft2")
    assert [r.url.path for r in seen] == ["/robots.txt"]


@pytest.mark.live
@pytest.mark.parametrize("company", ["Ubisoft2", "BoschGroup"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        try:
            jobs = await SmartRecruitersConnector(http).fetch_jobs(company)
        except RobotsDisallowedError:
            pytest.skip("api.smartrecruiters.com/robots.txt disallows our User-Agent")
    assert len(jobs) > 0
