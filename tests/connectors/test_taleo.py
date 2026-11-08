"""Oracle Taleo Enterprise connector (career-section sitemap + jobdetail pages).

Fixtures (captured 2026-11-08, verbatim):
    wsp_sitemap.xml            GET wsp.taleo.net/careersection/sitemap.jss
                               ?portalCode=globalcareersection&lang=en (first 3 of 160 jobs)
    wsp_job_26001063.html      GET .../globalcareersection/jobdetail.ftl?job=26001063&lang=en
    wsp_job_unavailable.html   the same for a job number that does not exist
                               (``unavailablerequisition.ftl``)
    hdr_job_183652.html        GET hdr.taleo.net/careersection/ex/jobdetail.ftl?job=183652
                               (description + qualifications + plain-text benefits, each once)
    stantec_job_240003MW.html  GET stantec.taleo.net/careersection/ex/jobdetail.ftl
                               ?job=240003MW (every description block sent twice)
    section_unavailable.html   GET aa224.taleo.net/careersection/2/jobsearch.ftl (switched-off
                               section; its sitemap is an empty <urlset>)
"""

import re
from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.taleo import TaleoConnector
from tests.connectors.jsonld_helpers import live_check
from tests.fakes import load_fixture, mock_http

WSP = "wsp.taleo.net/globalcareersection"
JOBS = {"26001063": "wsp_job_26001063.html"}  # others in the sitemap: unavailable page


def _job_pages(pages: dict[str, str]) -> httpx.Response | object:
    def handle(request: httpx.Request) -> httpx.Response:
        name = pages.get(request.url.params.get("job", ""), "wsp_job_unavailable.html")
        return httpx.Response(200, text=load_fixture("taleo", name))

    return handle


async def _detail(host: str, section: str, job: str, fixture: str) -> RawJob:
    routes = {f"{host}/careersection/{section}/jobdetail.ftl": _job_pages({job: fixture})}
    http, _ = mock_http(routes)  # type: ignore[arg-type]
    url = f"https://{host}/careersection/{section}/jobdetail.ftl?job={job}&lang=en"
    async with http:
        detail = await TaleoConnector(http).fetch_job_detail(
            f"{host}/{section}", RawJob(external_id=job, title="", source_url=url)
        )
    assert detail is not None
    return detail


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://wsp.taleo.net/careersection/globalcareersection/jobsearch.ftl?lang=en", WSP),
        ("https://hdr.taleo.net/careersection/ex/jobdetail.ftl?job=183652", "hdr.taleo.net/ex"),
        ("https://jobs.dubaicareers.ae/careersection/dc/moresearch.ftl", "jobs.dubaicareers.ae/dc"),
        ("https://hdr.taleo.net/careersection/2026PRD.1.5.9.3.0/css/ftl.ftl", None),
        ("https://hdr.taleo.net/careersection/sitemap.jss", None),
        ("https://wsp.taleo.net/", None),
    ],
)
def test_detect(url: str, board: str | None) -> None:
    assert TaleoConnector.detect(url) == board


def test_board_url() -> None:
    assert TaleoConnector.board_url(WSP) == (
        "https://wsp.taleo.net/careersection/globalcareersection/jobsearch.ftl?lang=en"
    )


async def test_sitemap_and_job_pages() -> None:
    routes = {
        "wsp.taleo.net/careersection/sitemap.jss": httpx.Response(
            200, text=load_fixture("taleo", "wsp_sitemap.xml")
        ),
        "wsp.taleo.net/careersection/globalcareersection/jobdetail.ftl": _job_pages(JOBS),
    }
    http, seen = mock_http(routes)  # type: ignore[arg-type]
    async with http:
        connector = TaleoConnector(http)
        stubs = await connector.fetch_jobs(WSP)
        details = {s.external_id: await connector.fetch_job_detail(WSP, s) for s in stubs}
    assert seen[1].url.params["portalCode"] == "globalcareersection"
    assert sorted(details) == ["26001063", "26001123", "26001172"]

    job = details["26001063"]
    assert job is not None
    assert job.expires_at is None
    job = job.normalized()
    assert job.title == "Ingeniero/a Mecánico/a Climatización (Junior)"
    assert job.company == "WSP"
    assert job.location_raw == "SANTIAGO, CL"
    assert job.country == "CL"
    assert job.employment_type == "full_time"
    assert job.posted_at == datetime(2026, 10, 30, 11, 18, 47, tzinfo=UTC)
    assert "Quiénes somos" in (job.description_text or "")
    assert "jobapply.ftl?job=26001063" in (job.apply_url or "")

    # job numbers listed but no longer open: the unavailable page -> stored closed
    for gone in ("26001123", "26001172"):
        closed = details[gone]
        assert closed is not None
        assert closed.expires_at is not None


async def test_description_blocks_once_each_and_us_location() -> None:
    job = (await _detail("hdr.taleo.net", "ex", "183652", "hdr_job_183652.html")).normalized()
    assert job.title == "Transmission Line Owner’s Engineer"
    assert job.company == "HDR"  # <title>Job Search | HDR</title>
    assert job.location_raw == "Billings, Montana, United States"
    assert (job.city, job.country) == ("Billings", "US")
    assert job.posted_at == datetime(2025, 2, 19, tzinfo=UTC)
    text = job.description_text or ""
    assert "employee-owners" in text
    assert "Required Qualifications" in text
    assert "comprehensive benefits package" in text  # plain-text block kept


async def test_google_markup_tenant_uses_its_json_ld() -> None:
    # Stantec turned on Taleo's optional JSON-LD ("scriptForGoogle"): the generic path.
    job = (
        await _detail("stantec.taleo.net", "ex", "240003MW", "stantec_job_240003MW.html")
    ).normalized()
    assert job.title == "Expressions of interest - Graduate Environmental Scientist"
    assert job.company == "Stantec Consulting Ltd."
    assert job.posted_at == datetime(2024, 10, 1, tzinfo=UTC)
    assert job.description_text


async def test_duplicated_description_blocks_are_kept_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = load_fixture("taleo", "stantec_job_240003MW.html")
    page = re.sub(r'<script id = "markupid" type="application/ld\+json">.*?</script>', "", page)
    assert "ld+json" not in page
    monkeypatch.setattr("tests.connectors.test_taleo.load_fixture", lambda *_: page)
    raw = await _detail("stantec.taleo.net", "ex", "240003MW", "stantec_job_240003MW.html")
    job = raw.normalized()
    assert job.title == "Expressions of interest - Graduate Environmental Scientist"
    assert job.company is None  # <title>Job Search</title>: from sources.company_name
    assert job.location_raw == "Perth, Western Australia, Australia"
    assert job.posted_at == datetime(2024, 10, 1, 16, 8, 57, tzinfo=UTC)
    html = raw.description_html or ""
    assert html.count(html[:200]) == 1


async def test_empty_sitemap_of_unavailable_section_fails() -> None:
    routes = {
        "aa224.taleo.net/careersection/sitemap.jss": httpx.Response(
            200, text='<?xml version="1.0"?><urlset></urlset>'
        ),
        "aa224.taleo.net/careersection/2/jobsearch.ftl": httpx.Response(
            200, text=load_fixture("taleo", "section_unavailable.html")
        ),
    }
    http, _ = mock_http(routes)
    async with http:
        with pytest.raises(ConnectorError, match="unavailable"):
            await TaleoConnector(http).fetch_jobs("aa224.taleo.net/2")


async def test_changed_layout_is_an_error() -> None:
    page = load_fixture("taleo", "wsp_job_26001063.html").replace(
        "'Ingeniero/a Mecánico/a Climatización (Junior)','26001063'", "'X','26001063'"
    )
    routes = {
        "wsp.taleo.net/careersection/globalcareersection/jobdetail.ftl": httpx.Response(
            200, text=page
        )
    }
    http, _ = mock_http(routes)
    url = "https://wsp.taleo.net/careersection/globalcareersection/jobdetail.ftl?job=26001063"
    async with http:
        with pytest.raises(ConnectorError, match="title mismatch"):
            await TaleoConnector(http).fetch_job_detail(
                WSP, RawJob(external_id="26001063", title="", source_url=url)
            )


@pytest.mark.live
@pytest.mark.parametrize("board", [WSP, "stantec.taleo.net/ex"])
async def test_live(board: str) -> None:
    await live_check(TaleoConnector, board)
