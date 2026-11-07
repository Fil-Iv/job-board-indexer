"""Deel connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07):
    guardare_sitemap.xml        GET https://jobs.deel.com/guardare/sitemap.xml (verbatim)
    guardare_job_46770eff.html  GET https://jobs.deel.com/guardare/job-details/
                                46770eff-42e3-46aa-aa0f-fb45732bb80a/overview
                                (<style>, <link>, <svg>, non-JSON-LD <script> removed)
"""

import pytest

from jobindex.connectors.deel import DeelConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check

HOST = "jobs.deel.com"
JOB = "46770eff-42e3-46aa-aa0f-fb45732bb80a"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.deel.com/guardare", "guardare"),
        ("https://jobs.deel.com/job-boards/apmc-careers", "apmc-careers"),
        (f"https://jobs.deel.com/Guardare/job-details/{JOB}/overview", "guardare"),
        ("https://jobs.deel.com/", None),
        ("https://jobs.deel.com/api/x", None),
        ("https://www.deel.com/careers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert DeelConnector.detect(url) == expected


async def test_sitemap_and_job_page_from_fixture() -> None:
    routes = fixture_routes(
        "deel",
        {
            f"{HOST}/guardare/sitemap.xml": "guardare_sitemap.xml",
            f"{HOST}/guardare/job-details/{JOB}/overview": "guardare_job_46770eff.html",
        },
    )
    stubs, details = await crawl_fixture(DeelConnector, "guardare", routes)
    assert len(stubs) == 3  # the board page itself is not a job
    assert JOB in {s.external_id for s in stubs}
    assert all(s.list_version and s.list_version.startswith("2026-") for s in stubs)
    job = details[JOB].normalized()
    assert (job.title, job.company) == ("Join Our Talent Network", "Guardare")
    assert job.employment_type == "full_time"
    assert job.posted_at is not None
    assert job.posted_at.date().isoformat() == "2026-01-19"
    assert job.description_text


@pytest.mark.live
@pytest.mark.parametrize("org", ["guardare", "apmc-careers"])
async def test_live(org: str) -> None:
    await live_check(DeelConnector, org)
