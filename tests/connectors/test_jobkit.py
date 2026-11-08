"""JobKit connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-08, trimmed):
    sitemap.xml     GET https://jobs.rubyonrails.org/sitemap.xml (4 of 14 urls)
    job_44740.html  GET https://jobs.rubyonrails.org/jobs/44740-lead-engineer-ai-accounting-exponent
                    (<style>, <svg>, non-JSON-LD <script> and non-canonical <link> removed)
"""

from decimal import Decimal

import pytest

from jobindex.connectors.jobkit import JobKitConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "jobs.rubyonrails.org"
JOB = "jobs/44740-lead-engineer-ai-accounting-exponent"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://remote.jobkit.io/jobs/12-engineer", "remote.jobkit.io"),
        ("https://www.jobkit.io/", None),
        ("https://api.jobkit.io/v1/jobs", None),
        ("https://jobs.rubyonrails.org/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JobKitConnector.detect(url) == expected


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("jobkit", "job_44740.html")
    assert JobKitConnector.detect_in_html(page) == {BOARD: 1}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "jobkit", {f"{BOARD}/sitemap.xml": "sitemap.xml", f"{BOARD}/{JOB}": "job_44740.html"}
    )
    stubs, details = await crawl_fixture(JobKitConnector, BOARD, routes)
    assert [s.external_id for s in stubs] == ["44740", "44739", "44738"]  # not the home page
    assert stubs[0].list_version == "2026-11-03"
    job = details["44740"].normalized()
    assert (job.title, job.company) == ("Lead Engineer, AI Accounting", "Exponent")
    assert (job.city, job.region, job.country) == ("New York", "NY", "US")
    assert (job.salary_min, job.salary_max) == (Decimal(160000), Decimal(200000))
    assert (job.salary_currency, job.salary_period) == ("USD", "year")
    assert job.employment_type == "full_time"
    assert job.description_html
    assert "AI Accountant" in job.description_html


@pytest.mark.live
async def test_live() -> None:
    await live_check(JobKitConnector, BOARD)
