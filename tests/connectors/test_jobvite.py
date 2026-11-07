"""Jobvite connector.

Fixtures (captured 2026-11-07; <style>, <link> and non-JSON-LD <script> removed):
    egnyte_search_p0.html      GET https://jobs.jobvite.com/egnyte/search/?p=0
                               (one page, "1-26 of 26")
    oneco_search_p0.html       GET https://jobs.jobvite.com/oneco/search/?p=0  ("1-50 of 63",
    oneco_search_p1.html       GET .../oneco/search/?p=1   "51-63 of 63"; Jobvite's demo
                               company, rows without location)
    compas_search_p0.html      GET https://jobs.jobvite.com/compas/search/?p=0
                               (no open jobs: "No results found.")
    nutanix_job_ock3AfwT.html  GET https://jobs.jobvite.com/nutanix/job/ock3AfwT
                               (no JSON-LD)
    webmd_job_o20PAfwb.html    GET https://jobs.jobvite.com/webmd/job/o20PAfwb
                               (JSON-LD JobPosting with baseSalary)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.jobvite import JobviteConnector
from jobindex.http import HttpClient
from tests.fakes import load_fixture, mock_http

HOST = "jobs.jobvite.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.jobvite.com/nutanix/jobs", "nutanix"),
        ("https://jobs.jobvite.com/Nutanix", "nutanix"),
        ("https://jobs.jobvite.com/webmd/job/o20PAfwb", "webmd"),
        ("https://jobs.jobvite.com/webmd/job/o20PAfwb/apply", "webmd"),
        ("https://jobs.jobvite.com/compas/jobAlerts", "compas"),
        ("https://jobs.jobvite.com/careers/oneco/jobs", "oneco"),
        ("https://jobs.jobvite.com/", None),
        ("https://jobs.jobvite.com/__assets__/x.css", None),
        ("https://www.jobvite.com/support/job-seeker-support/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JobviteConnector.detect(url) == expected


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("jobvite", name))


def _paged(*names: str) -> httpx.Response | object:
    def handle(request: httpx.Request) -> httpx.Response:
        return _page(names[int(request.url.params["p"])])

    return handle


async def test_single_page_list() -> None:
    http, _ = mock_http({f"{HOST}/egnyte/search/": _page("egnyte_search_p0.html")})
    async with http:
        jobs = await JobviteConnector(http).fetch_jobs("egnyte")
    assert len(jobs) == 26
    first = jobs[0]
    assert (first.external_id, first.title) == ("oV43Afwm", "Account Executive 2, AEC - East")
    assert first.location_raw == "Raleigh, North Carolina"
    assert first.source_url == "https://jobs.jobvite.com/egnyte/job/oV43Afwm"
    assert first.apply_url == "https://jobs.jobvite.com/egnyte/job/oV43Afwm/apply"


async def test_reads_every_page_until_total() -> None:
    routes = {f"{HOST}/oneco/search/": _paged("oneco_search_p0.html", "oneco_search_p1.html")}
    http, seen = mock_http(routes)  # type: ignore[arg-type]
    async with http:
        jobs = await JobviteConnector(http).fetch_jobs("oneco")
    assert len(jobs) == 63
    assert len({j.external_id for j in jobs}) == 63
    assert [r.url.params["p"] for r in seen if r.url.path.endswith("/search/")] == ["0", "1"]
    assert jobs[0].location_raw is None  # demo rows have no location


async def test_missing_page_raises_instead_of_returning_fewer_jobs() -> None:
    # page 1 repeats page 0: 50 of 63 jobs, so the crawl must fail
    routes = {f"{HOST}/oneco/search/": _paged("oneco_search_p0.html", "oneco_search_p0.html")}
    http, _ = mock_http(routes)  # type: ignore[arg-type]
    async with http:
        with pytest.raises(ConnectorError, match="reports 63 jobs"):
            await JobviteConnector(http).fetch_jobs("oneco")


async def test_empty_board_and_unknown_company() -> None:
    support = httpx.Response(200, text="<html><body><h1>Job Seeker Support</h1></body></html>")
    http, _ = mock_http(
        {
            f"{HOST}/compas/search/": _page("compas_search_p0.html"),
            f"{HOST}/nosuch/search/": support,  # unknown companies redirect to jobvite.com
        }
    )
    async with http:
        assert await JobviteConnector(http).fetch_jobs("compas") == []
        with pytest.raises(ConnectorError, match="not a job list"):
            await JobviteConnector(http).fetch_jobs("nosuch")


def _stub(company: str, job_id: str, location: str) -> RawJob:
    return RawJob(
        external_id=job_id,
        title="x",
        location_raw=location,
        source_url=f"https://{HOST}/{company}/job/{job_id}",
    )


async def test_detail_from_html_and_jsonld() -> None:
    http, _ = mock_http(
        {
            f"{HOST}/nutanix/job/ock3AfwT": _page("nutanix_job_ock3AfwT.html"),
            f"{HOST}/webmd/job/o20PAfwb": _page("webmd_job_o20PAfwb.html"),
        }
    )
    async with http:
        connector = JobviteConnector(http)
        nutanix = await connector.fetch_job_detail(
            "nutanix", _stub("nutanix", "ock3AfwT", "Sacramento, California")
        )
        webmd = await connector.fetch_job_detail(
            "webmd", _stub("webmd", "o20PAfwb", "United States")
        )
        gone = await connector.fetch_job_detail("webmd", _stub("webmd", "nosuch", "x"))  # 404
    assert nutanix is not None
    assert nutanix.description_html is not None
    assert "Senior Account Manager at Nutanix" in nutanix.description_html
    assert nutanix.location_raw == "Sacramento, California"  # from the list
    assert nutanix.posted_at is None  # no JSON-LD on this tenant
    assert webmd is not None
    assert (webmd.company, webmd.employment_type) == ("WebMD", "full_time")
    assert webmd.salary_min is None  # JSON-LD baseSalary is empty ...
    normalized = webmd.normalized()  # ... the range is in the description text
    assert (normalized.salary_min, normalized.salary_max, normalized.salary_currency) == (
        Decimal(90000),
        Decimal(95000),
        "USD",
    )
    assert webmd.posted_at is not None
    assert webmd.posted_at.date().isoformat() == "2026-09-18"
    assert gone is None


async def test_detail_redirected_away_is_closed() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": f"https://{HOST}/webmd/jobs?error=404"})

    job_list = httpx.Response(200, text="<html><body>All jobs</body></html>")
    http, _ = mock_http({f"{HOST}/webmd/job/oOld1234": redirect, f"{HOST}/webmd/jobs": job_list})
    async with http:
        assert (
            await JobviteConnector(http).fetch_job_detail("webmd", _stub("webmd", "oOld1234", "x"))
            is None
        )


@pytest.mark.live
@pytest.mark.parametrize("company", ["webmd", "nutanix"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = JobviteConnector(http)
        jobs = await connector.fetch_jobs(company)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(company, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html
