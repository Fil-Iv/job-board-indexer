"""Polymer connector.

Fixtures (captured 2026-11-07):
    themis_jobs_p1.json     GET https://api.polymer.co/v1/hire/organizations/themis/jobs?page=1
                            (verbatim, 6 jobs, 1 page)
    invicta_jobs_p1.json    GET .../organizations/invicta-software/jobs?page=1 (verbatim;
                            salary_pretty ranges, one "No remote")
    themis_job_40452.json   GET .../organizations/themis/jobs/40452 (questions cut to 1)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.polymer import PolymerConnector
from jobindex.http import FetchError, HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, load_json_fixture, mock_http

API = "api.polymer.co/v1/hire/organizations"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.polymer.co/themis", "themis"),
        ("https://jobs.polymer.co/Themis/40452", "themis"),
        ("https://jobs.polymer.co/invicta-software/41338", "invicta-software"),
        ("https://api.polymer.co/v1/hire/organizations/themis/jobs?page=1", "themis"),
        ("https://jobs.polymer.co/", None),
        ("https://jobs.polymer.co/packs/css/x.css", None),
        ("https://polymer.co/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert PolymerConnector.detect(url) == expected


def _json(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("polymer", name))


async def test_fetch_jobs_and_detail_from_fixture() -> None:
    http, seen = mock_http(
        {
            f"{API}/themis/jobs": _json("themis_jobs_p1.json"),
            f"{API}/themis/jobs/40452": _json("themis_job_40452.json"),
            f"{API}/invicta-software/jobs": _json("invicta_jobs_p1.json"),
        }
    )
    async with http:
        connector = PolymerConnector(http)
        themis = await connector.fetch_jobs("themis")
        invicta = await connector.fetch_jobs("invicta-software")
        detail = await connector.fetch_job_detail("themis", themis[0])
        gone = await connector.fetch_job_detail("themis", themis[1])  # 404
    assert next(r for r in seen if r.url.path.endswith("/jobs")).url.params["page"] == "1"
    assert len(themis) == 6
    first = themis[0]
    assert (first.external_id, first.title) == ("40452", "Finance and Operations Manager")
    assert (first.company, first.city, first.region, first.country) == (
        "Themis",
        "New York",
        "NY",
        "US",
    )
    assert first.remote_type is RemoteType.REMOTE
    assert first.employment_type == "full_time"
    assert first.posted_at is not None
    assert first.posted_at.date().isoformat() == "2026-07-02"
    assert first.source_url == "https://jobs.polymer.co/themis/40452"
    assert first.description_html is None  # list has no descriptions

    assert detail is not None
    assert detail.description_html is not None
    assert detail.description_html.startswith("<h2>About Themis</h2>")
    assert gone is None

    by_id = {j.external_id: j for j in invicta}
    paid = by_id["41016"]
    assert (paid.salary_min, paid.salary_max, paid.salary_currency, paid.salary_period) == (
        Decimal(95000),
        Decimal(140000),
        "USD",
        "year",
    )
    assert by_id["39269"].remote_type is RemoteType.ONSITE  # "No remote"


async def test_reads_every_page_and_checks_count() -> None:
    page1 = load_json_fixture("polymer", "themis_jobs_p1.json")
    first, rest = page1["items"][:4], page1["items"][4:]

    def pages(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        items = first if page == 1 else rest if page == 2 else []
        meta = {**page1["meta"], "page": page, "total": 2, "count": 6, "is_last": False}
        return httpx.Response(200, json={"items": items, "meta": meta})

    http, seen = mock_http({f"{API}/themis/jobs": pages})
    async with http:
        jobs = await PolymerConnector(http).fetch_jobs("themis")
    assert len(jobs) == 6
    assert [r.url.params["page"] for r in seen if "jobs" in r.url.path] == ["1", "2"]

    def short(request: httpx.Request) -> httpx.Response:  # count says 7, only 6 listed
        return httpx.Response(200, json={"items": page1["items"], "meta": {"count": 7, "total": 1}})

    http, _ = mock_http({f"{API}/themis/jobs": short})
    async with http:
        with pytest.raises(ConnectorError, match="reports 7 jobs, got 6"):
            await PolymerConnector(http).fetch_jobs("themis")


async def test_unknown_board_and_bad_payload() -> None:
    http, _ = mock_http(
        {
            f"{API}/nosuch/jobs": httpx.Response(422, json={"error": "unprocessable"}),
            f"{API}/odd/jobs": httpx.Response(200, json=[]),
        }
    )
    async with http:
        with pytest.raises(FetchError, match="422"):
            await PolymerConnector(http).fetch_jobs("nosuch")
        with pytest.raises(ConnectorError):
            await PolymerConnector(http).fetch_jobs("odd")


@pytest.mark.live
@pytest.mark.parametrize("slug", ["themis", "invicta-software"])
async def test_live(slug: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = PolymerConnector(http)
        jobs = await connector.fetch_jobs(slug)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(slug, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html
