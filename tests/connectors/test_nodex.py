"""Nodex connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-11 from www.mytraveljobs.co.uk):
    robots.txt    GET /robots.txt (verbatim; no Sitemap line)
    sitemap.xml   GET /sitemap.xml (2 content pages + 3 of the 120 job pages kept)
    job.html      GET /job/full-time-training-officer-ground-handling-operations-stansted
"""

import pytest

from jobindex.connectors.nodex import NodexConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "www.mytraveljobs.co.uk"
JOB = "/job/full-time-training-officer-ground-handling-operations-stansted"


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("nodex", "job.html")
    assert set(NodexConnector.detect_in_html(page)) == {BOARD}


def test_urls_alone_are_not_detected() -> None:
    assert NodexConnector.detect(f"https://{BOARD}{JOB}") is None


async def test_sitemap_and_job_page() -> None:
    routes = fixture_routes(
        "nodex",
        {
            f"{BOARD}/robots.txt": "robots.txt",
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}{JOB}": "job.html",
        },
    )
    stubs, details = await crawl_fixture(NodexConnector, BOARD, routes)
    assert len(stubs) == 3  # the home page and blog post are skipped
    assert all(s.list_version and s.list_version.startswith("2026-11-") for s in stubs)

    job = details[JOB.removeprefix("/job/")].normalized()
    assert job.title == "Training Officer - Ground Handling Operations"  # columns dropped
    assert job.company == "Ryanair"
    assert job.city == "Stansted"
    assert job.employment_type == "full_time"
    assert job.posted_at is not None
    assert job.expires_at is not None  # validThrough 2026-12-09
    assert job.description_text


@pytest.mark.live
async def test_live() -> None:
    await live_check(NodexConnector, BOARD)
