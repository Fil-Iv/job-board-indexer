"""JazzHR connector (HTML list + HTML job pages).

Fixtures (captured 2026-11-07; <style>, <script>, <link>, <svg> removed):
    ilsos_apply.html                    GET https://ilsos.applytojob.com/apply (24 jobs)
    firstadvantage_job_5BCvO7JygF.html  GET https://firstadvantage.applytojob.com/apply/
                                        5BCvO7JygF/Senior-Technical-Product-Manager-IC-Remote
    nonprofittalent_legacy_list.html    GET https://nonprofittalent.applytojob.com/apply
                                        (2026-11-08; legacy "resumator" theme, 1 job)
    nonprofittalent_legacy_job.html     GET .../apply/n5RRfdb50q/Chief-Executive-Officer-
                                        Hello-Neighbor (2026-11-08, legacy theme)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.jazzhr import JazzHRConnector
from jobindex.http import HttpClient
from tests.fakes import load_fixture, mock_http

JOB = "firstadvantage.applytojob.com/apply/5BCvO7JygF/Senior-Technical-Product-Manager-IC-Remote"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ilsos.applytojob.com/apply", "ilsos"),
        ("https://FirstAdvantage.applytojob.com/apply/5BCvO7JygF/Senior-PM", "firstadvantage"),
        ("https://www.applytojob.com/", None),
        ("https://app.applytojob.com/notfound", None),
        ("https://www.jazzhr.com/job-seekers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JazzHRConnector.detect(url) == expected


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("jazzhr", name))


async def test_fetch_jobs_from_fixture() -> None:
    http, _ = mock_http({"ilsos.applytojob.com/apply": _page("ilsos_apply.html")})
    async with http:
        jobs = await JazzHRConnector(http).fetch_jobs("ilsos")
    assert len(jobs) == 24
    first = jobs[0]
    assert (first.external_id, first.title) == (
        "6aWiXpHhZf",
        "Deputy Director - Administrative Hearings",
    )
    assert first.location_raw == "Chicago, IL"  # department ("Administrative Hearings") dropped
    assert first.source_url == (
        "https://ilsos.applytojob.com/apply/6aWiXpHhZf/Deputy-Director-Administrative-Hearings"
    )


async def test_fetch_job_detail_from_fixture() -> None:
    http, _ = mock_http(
        {
            JOB: _page("firstadvantage_job_5BCvO7JygF.html"),
            "firstadvantage.applytojob.com/apply/ZZZZZZZZZZ/Old-Job": httpx.Response(410),
        }
    )
    stub = RawJob(
        external_id="5BCvO7JygF",
        title="Senior Technical Product Manager- (IC)- Remote",
        location_raw="Bangalore, Karnataka, India",
        source_url=f"https://{JOB}",
    )
    gone_stub = RawJob(
        external_id="ZZZZZZZZZZ",
        title="x",
        source_url="https://firstadvantage.applytojob.com/apply/ZZZZZZZZZZ/Old-Job",
    )
    async with http:
        connector = JazzHRConnector(http)
        job = await connector.fetch_job_detail("firstadvantage", stub)
        gone = await connector.fetch_job_detail("firstadvantage", gone_stub)
    assert job is not None
    assert job.employment_type == "full_time"
    assert job.description_html is not None
    assert "At First Advantage" in job.description_html
    assert job.normalized().country == "IN"
    assert gone is None


async def test_unknown_company_and_changed_template_raise() -> None:
    def to_jazz(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://www.jazzhr.com/job-seekers"})

    http, _ = mock_http(
        {
            "nosuch.applytojob.com/apply": to_jazz,
            "www.jazzhr.com/job-seekers": httpx.Response(200, text="<html>JazzHR</html>"),
            "odd.applytojob.com/apply": httpx.Response(200, text="<html><body></body></html>"),
        }
    )
    async with http:
        with pytest.raises(ConnectorError, match="redirected"):
            await JazzHRConnector(http).fetch_jobs("nosuch")
        with pytest.raises(ConnectorError, match="no job list"):
            await JazzHRConnector(http).fetch_jobs("odd")


def test_job_drop_guard_enabled() -> None:
    http, _ = mock_http({})
    assert JazzHRConnector(http).max_job_drop() is not None


@pytest.mark.live
@pytest.mark.parametrize("company", ["firstadvantage", "ilsos"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = JazzHRConnector(http)
        jobs = await connector.fetch_jobs(company)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(company, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html


async def test_legacy_resumator_theme() -> None:
    host = "nonprofittalent.applytojob.com"
    job_path = "/apply/n5RRfdb50q/Chief-Executive-Officer-Hello-Neighbor"
    routes = {
        f"{host}/apply": httpx.Response(
            200, text=load_fixture("jazzhr", "nonprofittalent_legacy_list.html")
        ),
        f"{host}{job_path}": httpx.Response(
            200, text=load_fixture("jazzhr", "nonprofittalent_legacy_job.html")
        ),
    }
    http, _ = mock_http(routes)
    async with http:
        connector = JazzHRConnector(http)
        jobs = await connector.fetch_jobs("nonprofittalent")
        assert [(j.external_id, j.title, j.location_raw) for j in jobs] == [
            ("n5RRfdb50q", "Chief Executive Officer, Hello Neighbor", "Pittsburgh, PA")
        ]
        detail = await connector.fetch_job_detail("nonprofittalent", jobs[0])
    assert detail is not None
    assert detail.description_html
    assert detail.employment_type == "full_time"
