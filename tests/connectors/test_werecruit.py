"""WeRecruit connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml            GET https://careers.werecruit.io/sitemap.xml (6 of 23083 urls:
                           3 dessange jobs + other tenants)
    dessange_job_ee36bb.html
                           GET https://careers.werecruit.io/fr/dessange/offres/coiffeurse-polyvalente-ee36bb
"""

import pytest

from jobindex.connectors.werecruit import WeRecruitConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check

H = "careers.werecruit.io"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://careers.werecruit.io/fr/dessange", "dessange"),
        ("https://careers.werecruit.io/en/idiap/offers/technicien-80-100-c9038e", "idiap"),
        ("https://careers.werecruit.io/", None),
        ("https://www.werecruit.io/fr/dessange", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert WeRecruitConnector.detect(url) == expected


async def test_fetch_from_fixture() -> None:
    page = "/fr/dessange/offres/coiffeurse-polyvalente-ee36bb"
    routes = fixture_routes(
        "werecruit",
        {f"{H}/sitemap.xml": "sitemap.xml", f"{H}{page}": "dessange_job_ee36bb.html"},
    )
    stubs, details = await crawl_fixture(WeRecruitConnector, "dessange", routes)
    assert [s.external_id for s in stubs] == ["ee36bb", "a9ae18", "45d752"]
    job = details["ee36bb"].normalized()
    assert (job.title, job.company) == ("Coiffeur(se) polyvalent(e) F/H", "DESSANGE Dijon")
    assert (job.city, job.country, job.employment_type) == ("Dijon", "FR", "full_time")
    assert job.expires_at is None


@pytest.mark.live
@pytest.mark.parametrize("slug", ["dessange", "declic-immo"])
async def test_live(slug: str) -> None:
    await live_check(WeRecruitConnector, slug)
