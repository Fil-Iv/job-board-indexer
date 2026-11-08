"""Zoho Recruit connector (job list embedded in the careers page + job page data).

Fixtures (captured 2026-11-08 from cause-capacity.zohorecruit.com; pages are ~1.7 MB of
inlined app code, so only <head> without scripts/styles and the element with the job data
are kept):
    careers.html                  GET /jobs/Careers (<input type="hidden" id="jobs">, 6 jobs)
    job_623910000010874001.html   GET /jobs/Careers/623910000010874001/TransLash-Media-Social-
                                  Media-Coordinator (the <script> with var jobs = JSON.parse)
    job_gone.html                 GET us-careers.zohorecruit.in/jobs/Careers/195077000010199999
                                  /Gone-Job (verbatim: "This job is no longer available.")
"""

from datetime import UTC, datetime

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.zoho import ZohoRecruitConnector
from jobindex.models.enums import RemoteType
from tests.connectors.jsonld_helpers import crawl_fixture, live_check
from tests.fakes import load_fixture, mock_http

HOST = "cause-capacity.zohorecruit.com"
BOARD = f"{HOST}/Careers"
JOB = "623910000010874001"


def _routes() -> dict[str, httpx.Response]:
    gone = httpx.Response(200, text=load_fixture("zoho", "job_gone.html"))
    routes = {
        f"{HOST}/jobs/Careers": httpx.Response(200, text=load_fixture("zoho", "careers.html")),
        f"{HOST}/jobs/Careers/{JOB}": httpx.Response(
            200, text=load_fixture("zoho", f"job_{JOB}.html")
        ),
    }
    for job_id in (
        "623910000010887004",
        "623910000009433013",
        "623910000009854001",
        "623910000010914095",
        "623910000010743065",
    ):
        routes[f"{HOST}/jobs/Careers/{job_id}"] = gone
    return routes


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://cause-capacity.zohorecruit.com/jobs/Careers", BOARD),
        (f"https://cause-capacity.zohorecruit.com/jobs/Careers/{JOB}/TransLash", BOARD),
        ("https://us-careers.zohorecruit.in/jobs/Careers", "us-careers.zohorecruit.in/Careers"),
        ("https://eu.zohorecruit.eu/jobs/careers", "eu.zohorecruit.eu/careers"),
        ("https://www.zohorecruit.com/jobs/Careers", None),
        ("https://cause-capacity.zohorecruit.com/recruit/Portal.na", None),
        ("https://zohorecruit.example.com/jobs/Careers", None),
    ],
)
def test_detect(url: str, board: str | None) -> None:
    assert ZohoRecruitConnector.detect(url) == board


async def test_careers_list_and_job_page() -> None:
    stubs, details = await crawl_fixture(ZohoRecruitConnector, BOARD, _routes())
    assert len(stubs) == 6
    assert all(s.source_url == f"https://{HOST}/jobs/Careers/{s.external_id}" for s in stubs)

    job = details[JOB].normalized()
    assert job.title == "TransLash Media- Social Media Coordinator"
    assert job.company == "Cause Capacity"  # og:site_name
    assert job.remote_type is RemoteType.REMOTE
    assert job.employment_type == "full_time"
    assert job.posted_at == datetime(2026, 11, 2, tzinfo=UTC)
    assert "This is a remote position." in (job.description_text or "")
    assert "\\x22" not in (job.description_html or "")  # JS escapes decoded
    assert job.expires_at is None

    # listed in the fixture but answered with "This job is no longer available."
    closed = details["623910000009854001"]
    assert closed.expires_at is not None


async def test_careers_page_without_job_list_fails() -> None:
    routes = {f"{HOST}/jobs/Careers": httpx.Response(200, text="<html><body>new</body></html>")}
    http, _ = mock_http(routes)
    async with http:
        with pytest.raises(ConnectorError, match="no job list"):
            await ZohoRecruitConnector(http).fetch_jobs(BOARD)


@pytest.mark.live
@pytest.mark.parametrize("board", [BOARD, "us-careers.zohorecruit.in/Careers"])
async def test_live(board: str) -> None:
    await live_check(ZohoRecruitConnector, board)
