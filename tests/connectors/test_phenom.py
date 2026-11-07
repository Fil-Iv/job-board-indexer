"""Phenom People connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    butterball_sitemap.xml    GET https://careers.butterball.com/us/en/sitemap.xml (6 of 200)
    shutterstock_sitemap.xml  GET https://careers.shutterstock.com/us/en/sitemap.xml (7 of
                              63, incl. the template entry /job/${jobSeqNo}/${title})
    butterball_job_DISTR004109.html
                              GET https://careers.butterball.com/us/en/job/DISTR004109/Distribution-Superintendent
                              (open; carries the hidden "has been filled" apply modal)
"""

import pytest

from jobindex.connectors.phenom import PhenomConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BB = "careers.butterball.com/us/en"


def test_detect_from_page() -> None:
    assert PhenomConnector.detect(f"https://{BB}/job/X/y") is None  # custom domains only
    page = load_fixture("phenom", "butterball_job_DISTR004109.html")
    assert PhenomConnector.detect_in_html(page) == {BB: 2}
    assert PhenomConnector.board_from_page_url("https://careers.acme.com/global/en/home") == (
        "careers.acme.com/global/en"
    )


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "phenom",
        {
            f"{BB}/sitemap.xml": "butterball_sitemap.xml",
            f"{BB}/job/DISTR004109/Distribution-Superintendent": "butterball_job_DISTR004109.html",
        },
    )
    stubs, details = await crawl_fixture(PhenomConnector, BB, routes)
    assert [s.external_id for s in stubs] == ["DISTR004109", "QUALI003965", "TEAML004120"]
    job = details["DISTR004109"].normalized()
    assert (job.title, job.company) == ("Distribution Superintendent", "Butterball")
    assert (job.city, job.country) == ("Carthage", "US")
    assert job.expires_at is None  # the hidden "filled" modal is not a closed notice


async def test_template_entry_is_not_a_job() -> None:
    host = "careers.shutterstock.com/us/en"
    routes = fixture_routes("phenom", {f"{host}/sitemap.xml": "shutterstock_sitemap.xml"})
    stubs, _ = await crawl_fixture(PhenomConnector, host, routes)
    assert [s.external_id for s in stubs] == ["R0003169", "R0003176", "R0003171"]


@pytest.mark.live
@pytest.mark.parametrize("board", [BB, "careers.shutterstock.com/us/en"])
async def test_live(board: str) -> None:
    await live_check(PhenomConnector, board)
