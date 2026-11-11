"""Freshteam connector (server-rendered career pages).

Fixtures (captured 2026-11-11; S3 pre-signed logo URLs cut at "?" and Cloudflare
e-mail-protection hashes blanked, since they carry tokens / addresses):
    renous_jobs.html         GET https://renous.freshteam.com/jobs (department template,
                             90 jobs, og:description "#90 Jobs - ")
    renous_job.html          GET https://renous.freshteam.com/jobs/tWy9uetKRLu_
                             (ends with the template's "Liquid error" line)
    crimsoniteam_jobs.html   GET https://crimsoniteam.freshteam.com/jobs (default
                             template, 95 jobs)
    crimsoniteam_job.html    GET https://crimsoniteam.freshteam.com/jobs/aDAPTG_8FV2U/
                             content-writer
    unknown_tenant.html      GET https://scrut.freshteam.com/jobs (200 "We couldn't find")
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.freshteam import FreshteamConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("freshteam", name))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://renous.freshteam.com/jobs", "renous"),
        ("https://CrimsoniTeam.freshteam.com/jobs/aDAPTG_8FV2U/content-writer", "crimsoniteam"),
        ("https://assets.freshteam.com/assets/job_filter.js", None),
        ("https://www.freshteam.com/", None),
        ("https://freshteam.com/jobs", None),
        ("https://renous.freshdesk.com/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert FreshteamConnector.detect(url) == expected


async def test_department_template() -> None:
    http, _ = mock_http({"renous.freshteam.com/jobs": _page("renous_jobs.html")})
    async with http:
        jobs = {j.external_id: j for j in await FreshteamConnector(http).fetch_jobs("renous")}
    assert len(jobs) == 90  # = "#90 Jobs" in og:description
    job = jobs["tWy9uetKRLu_"]
    assert job.title == "3156 | SAP Fiori | 3+ Years | Bangalore"
    assert job.source_url is not None
    assert job.source_url.startswith("https://renous.freshteam.com/jobs/tWy9uetKRLu_/")
    assert job.employment_type == "contract"
    assert all(j.title for j in jobs.values())
    # the card's data-portal-remote-location flag
    assert any(j.remote_type is RemoteType.REMOTE for j in jobs.values())


async def test_default_template() -> None:
    http, _ = mock_http({"crimsoniteam.freshteam.com/jobs": _page("crimsoniteam_jobs.html")})
    async with http:
        jobs = {j.external_id: j for j in await FreshteamConnector(http).fetch_jobs("crimsoniteam")}
    assert len(jobs) == 95
    job = jobs["aDAPTG_8FV2U"]
    assert (job.title, job.location_raw, job.employment_type) == (
        "Content Writer",
        "Beijing, Beijing",
        "full_time",
    )


async def test_unknown_company_is_an_error_not_an_empty_board() -> None:
    http, _ = mock_http({"scrut.freshteam.com/jobs": _page("unknown_tenant.html")})
    async with http:
        with pytest.raises(ConnectorError, match="not a Freshteam company"):
            await FreshteamConnector(http).fetch_jobs("scrut")


async def test_count_mismatch_is_refused() -> None:
    html = load_fixture("freshteam", "crimsoniteam_jobs.html").replace("#95 Jobs", "#96 Jobs")
    http, _ = mock_http({"crimsoniteam.freshteam.com/jobs": httpx.Response(200, text=html)})
    async with http:
        with pytest.raises(ConnectorError, match="refusing a partial list"):
            await FreshteamConnector(http).fetch_jobs("crimsoniteam")


async def _detail(path: str, fixture: str) -> RawJob | None:
    stub = RawJob(external_id="x", title="t", source_url=f"https://{path}")
    http, _ = mock_http({path: _page(fixture)})
    async with http:
        return await FreshteamConnector(http).fetch_job_detail("b", stub)


async def test_detail_drops_template_error_and_reads_company() -> None:
    job = await _detail("renous.freshteam.com/jobs/tWy9uetKRLu_", "renous_job.html")
    assert job is not None
    assert job.company == "Renous Extenserve Private Limited"
    assert job.description_html is not None
    assert "Develop and maintain SAP FIORI applications." in job.description_html
    assert "Liquid error" not in job.description_html


async def test_detail_default_template() -> None:
    job = await _detail(
        "crimsoniteam.freshteam.com/jobs/aDAPTG_8FV2U/content-writer", "crimsoniteam_job.html"
    )
    assert job is not None
    assert job.company == "Crimson Interactive Inc"
    assert (job.normalized().description_text or "").startswith("We are a leading international")


async def test_closed_job_is_none() -> None:
    stub = RawJob(external_id="x", title="t", source_url="https://renous.freshteam.com/jobs/gone1")
    http, _ = mock_http({})
    async with http:
        assert await FreshteamConnector(http).fetch_job_detail("renous", stub) is None


@pytest.mark.live
@pytest.mark.parametrize("board", ["renous", "crimsoniteam"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = FreshteamConnector(http)
        jobs = await connector.fetch_jobs(board)
        assert jobs
        detail = await connector.fetch_job_detail(board, jobs[0])
        assert detail is not None
        assert detail.description_html
