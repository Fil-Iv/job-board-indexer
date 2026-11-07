"""Kula connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07):
    archetype_sitemap.xml     GET https://careers.kula.ai/archetype-ai/sitemap.xml
                              (board page + 2 of 11 jobs)
    archetype_job_51545.html  GET https://careers.kula.ai/archetype-ai/51545-enterprise-
                              account-executive (<style>, <link>, <svg>, non-JSON-LD
                              <script> removed; salary range 200,000-300,000)
"""

from decimal import Decimal

import pytest

from jobindex.connectors.kula import KulaConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check

HOST = "careers.kula.ai"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://careers.kula.ai/archetype-ai", "archetype-ai"),
        ("https://careers.kula.ai/DeepScribe/123-engineer", "deepscribe"),
        ("https://careers.kula.ai/", None),
        ("https://careers.kula.ai/api/jobs", None),
        ("https://kula.ai/careers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert KulaConnector.detect(url) == expected


async def test_sitemap_and_job_page_from_fixture() -> None:
    routes = fixture_routes(
        "kula",
        {
            f"{HOST}/archetype-ai/sitemap.xml": "archetype_sitemap.xml",
            f"{HOST}/archetype-ai/51545-enterprise-account-executive": "archetype_job_51545.html",
        },
    )
    stubs, details = await crawl_fixture(KulaConnector, "archetype-ai", routes)
    assert sorted(s.external_id for s in stubs) == ["51545", "53624"]  # board page skipped
    job = details["51545"].normalized()
    assert (job.title, job.company) == ("Enterprise Account Executive", "Archetype AI")
    assert job.country == "US"
    assert job.employment_type == "full_time"
    assert (job.salary_min, job.salary_max) == (Decimal(200000), Decimal(300000))
    assert job.description_text


@pytest.mark.live
@pytest.mark.parametrize("company", ["archetype-ai", "deepscribe"])
async def test_live(company: str) -> None:
    await live_check(KulaConnector, company)
