"""Hireology connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap_index.xml   GET https://careers.hireology.com/sitemap_index.xml (verbatim)
    sitemap-1.xml       GET https://careers.hireology.com/sitemaps/sitemap-1.xml (9 of
                        50000 urls: all sandiegopadres jobs + nextpoint)
    sitemap-2.xml       GET .../sitemaps/sitemap-2.xml (2 of 7358 urls, other tenants)
    sandiegopadres_job_2699852.html
                        GET https://careers.hireology.com/sandiegopadres/2699852/description
                        (open, "Apply Now", but validThrough 2026-04-18)
"""

from decimal import Decimal

import pytest

from jobindex.connectors.hireology import HireologyConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check

H = "careers.hireology.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://careers.hireology.com/sandiegopadres", "sandiegopadres"),
        ("https://careers.hireology.com/SanDiegoPadres/2699852/description", "sandiegopadres"),
        ("https://careers.hireology.com/sitemaps/sitemap-1.xml", None),
        ("https://careers.hireology.com/", None),
        ("https://www.hireology.com/sandiegopadres", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert HireologyConnector.detect(url) == expected


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "hireology",
        {
            f"{H}/sitemap_index.xml": "sitemap_index.xml",
            f"{H}/sitemaps/sitemap-1.xml": "sitemap-1.xml",
            f"{H}/sitemaps/sitemap-2.xml": "sitemap-2.xml",
            f"{H}/sandiegopadres/2699852/description": "sandiegopadres_job_2699852.html",
        },
    )
    stubs, details = await crawl_fixture(HireologyConnector, "sandiegopadres", routes)
    assert [s.external_id for s in stubs] == [
        "2699852",
        "2706790",
        "2813570",
        "2813576",
        "2924326",
        "2924575",
    ]  # only this tenant's jobs from the shared sitemap
    job = details["2699852"].normalized()
    assert (job.title, job.company) == ("Security Officer (Part-Time)", "San Diego Padres")
    assert job.expires_at is None  # validThrough (past) is not trusted on Hireology
    assert (job.salary_min, job.salary_period) == (Decimal("21.14"), "hour")


@pytest.mark.live
@pytest.mark.parametrize("slug", ["sandiegopadres", "nextpoint"])
async def test_live(slug: str) -> None:
    await live_check(HireologyConnector, slug)
