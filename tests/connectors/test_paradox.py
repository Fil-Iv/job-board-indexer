"""Paradox career-site connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-08 from careers.fedex.com):
    robots.txt                   GET /robots.txt (verbatim: no rules, no Sitemap line)
    sitemap.xml                  GET /sitemap.xml (2 of 30 child sitemaps kept)
    sitemap-a0a4453b-en.xml      first 3 of 3,381 job URLs
    sitemap-a0a4453b-fr-ca.xml   2 of 405 job URLs: P25-394218-1 (also in the English
                                 fixture) and P25-359042-1 (English entry trimmed away, so
                                 here it is only listed in French)
    job.html                     GET /courier-swing-drvr-dot/job/P25-394285-1 (<style> and
                                 <svg> removed)
"""

import pytest

from jobindex.connectors.paradox import ParadoxConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "careers.fedex.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.fedex.com/courier/job/P25-394218-1",
        "https://fedex.sites.paradox.ai/",  # wildcard DNS; sitemaps name the employer domain
        "https://paradox.ai/",
    ],
)
def test_hosts_alone_are_not_detected(url: str) -> None:
    assert ParadoxConnector.detect(url) is None


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("paradox", "job.html")
    assert set(ParadoxConnector.detect_in_html(page)) == {BOARD}


@pytest.mark.parametrize(
    ("path", "job_id"),
    [
        ("/courier/job/P25-394218-1", "P25-394218-1"),
        ("/fr-ca/can-driver/job/P25-394218-1", "P25-394218-1"),
        (
            "/es/sous-chef-pasteler%C3%ADa-lodge/job/015BA33E862571A6E94AE1E167A85856__es",
            "015BA33E862571A6E94AE1E167A85856",
        ),
        ("/jobs", None),
        ("/career-areas/driver/", None),
    ],
)
def test_job_id(path: str, job_id: str | None) -> None:
    connector = ParadoxConnector.__new__(ParadoxConnector)
    connector.board_hosts = set()
    assert connector.job_id(f"https://{BOARD}{path}", BOARD) == job_id


async def test_sitemaps_and_job_page() -> None:
    routes = fixture_routes(
        "paradox",
        {
            f"{BOARD}/robots.txt": "robots.txt",
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}/sitemap-a0a4453b-en.xml": "sitemap-a0a4453b-en.xml",
            f"{BOARD}/sitemap-a0a4453b-fr-ca.xml": "sitemap-a0a4453b-fr-ca.xml",
            f"{BOARD}/courier-swing-drvr-dot/job/P25-394285-1": "job.html",
        },
    )
    stubs, details = await crawl_fixture(ParadoxConnector, BOARD, routes)
    urls = {s.external_id: s.source_url for s in stubs}
    assert set(urls) == {"P25-394285-1", "P25-331783-2", "P25-394218-1", "P25-359042-1"}
    # listed in English and French: the English page is kept
    assert urls["P25-394218-1"] == f"https://{BOARD}/courier/job/P25-394218-1"
    # listed only in French: kept
    assert urls["P25-359042-1"] == f"https://{BOARD}/fr-ca/can-driver/job/P25-359042-1"
    assert all(s.list_version and s.list_version.startswith("2026-") for s in stubs)

    job = details["P25-394285-1"].normalized()
    assert job.title == "Courier/Swing Drvr/DOT"
    assert job.company == "Federal Express Corporation"
    assert (job.city, job.region, job.country) == ("St. Joseph", "MO", "US")
    assert job.posted_at is not None
    assert job.description_text


@pytest.mark.live
@pytest.mark.parametrize("board", ["careers.fedex.com", "careers.marriott.com"])
async def test_live(board: str) -> None:
    await live_check(ParadoxConnector, board)
