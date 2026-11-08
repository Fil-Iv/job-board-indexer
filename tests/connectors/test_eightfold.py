"""Eightfold AI connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-08 from paypal.eightfold.ai):
    robots.txt              GET /robots.txt (verbatim)
    sitemap_index.xml       GET /careers/sitemap_index.xml?domain=paypal.com (verbatim)
    sitemap.xml             GET /careers/sitemap.xml?domain=paypal.com (board page + 3 of
                            305 jobs)
    job_274922421647.html   GET /careers/job/274922421647-senior-manager-product-marketing-
                            platform-expansion-new-york-city-new-york-united-states-of-america
                            ?domain=paypal.com (<style>, <svg>, non-JSON-LD <script> and the
                            hidden pcsx-data/navbar-data JSON blobs removed)
"""

import pytest

from jobindex.connectors.eightfold import EightfoldConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "paypal.eightfold.ai"
JOB = (
    "/careers/job/274922421647-senior-manager-product-marketing-platform-expansion-new-york-"
    "city-new-york-united-states-of-america"
)


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://paypal.eightfold.ai/careers?domain=paypal.com", BOARD),
        (f"https://paypal.eightfold.ai{JOB}?domain=paypal.com", BOARD),
        ("https://app.eightfold.ai/careers?domain=qualcomm.com", None),  # platform host
        ("https://careers.qualcomm.com/careers", None),  # custom domain: page fingerprint
        ("https://eightfold.ai/", None),
    ],
)
def test_detect(url: str, board: str | None) -> None:
    assert EightfoldConnector.detect(url) == board


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("eightfold", "job_274922421647.html")
    assert set(EightfoldConnector.detect_in_html(page)) == {BOARD}  # canonical URL


async def test_sitemap_and_job_page() -> None:
    routes = fixture_routes(
        "eightfold",
        {
            f"{BOARD}/robots.txt": "robots.txt",
            f"{BOARD}/careers/sitemap_index.xml": "sitemap_index.xml",
            f"{BOARD}/careers/sitemap.xml": "sitemap.xml",
            f"{BOARD}{JOB}": "job_274922421647.html",
        },
    )
    stubs, details = await crawl_fixture(EightfoldConnector, BOARD, routes)
    assert len(stubs) == 3  # board page skipped; sitemap_cat.xml is never requested
    assert all(s.list_version and s.list_version.startswith("2026-") for s in stubs)
    job = details["274922421647"].normalized()
    assert job.title
    assert job.company
    assert job.posted_at is not None
    assert job.description_text
    assert "PayPal" in job.description_text
    assert job.country == "US"


@pytest.mark.live
@pytest.mark.parametrize("board", ["paypal.eightfold.ai", "vodafone.eightfold.ai"])
async def test_live(board: str) -> None:
    await live_check(EightfoldConnector, board)
