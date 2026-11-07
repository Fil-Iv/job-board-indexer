"""Niceboard connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed: scripts other than JSON-LD/external, styles removed):
    sitemap.xml        GET https://jobs.nonprofittalent.com/sitemap.xml (index, verbatim)
    sitemap-jobs.xml   GET https://jobs.nonprofittalent.com/sitemap-jobs.xml (5 of 83 urls)
    job_1441455.html   GET https://jobs.nonprofittalent.com/job/1441455-board-member-...
                       (remote: jobLocationType TELECOMMUTE, no jobLocation)
    job_2555753.html   GET https://jobs.forcebrands.com/job/2555753-market-development-...
"""

import pytest

from jobindex.connectors.niceboard import NiceboardConnector
from jobindex.models.enums import RemoteType
from tests.connectors.jsonld_helpers import (
    crawl_fixture,
    fixture_routes,
    live_check,
    sitemap_route,
)
from tests.fakes import load_fixture

BOARD = "jobs.nonprofittalent.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.niceboard.co/", "acme.niceboard.co"),
        ("https://acme.niceboard.co/job/12-engineer", "acme.niceboard.co"),
        ("https://niceboard.co/", None),  # vendor site
        ("https://cdn2.niceboard.co/x.css", None),
        ("https://jobs.nonprofittalent.com/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert NiceboardConnector.detect(url) == expected


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("niceboard", "job_2555753.html")
    assert NiceboardConnector.detect_in_html(page) == {"jobs.forcebrands.com": 1}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "niceboard",
        {
            f"{BOARD}/sitemap.xml": "sitemap.xml",
            f"{BOARD}/sitemap-jobs.xml": "sitemap-jobs.xml",
            f"{BOARD}/job/1441455-board-member-2026-28-term-pittsburgh-equality-center": (
                "job_1441455.html"
            ),
        },
    )
    stubs, details = await crawl_fixture(NiceboardConnector, BOARD, routes)
    assert len(stubs) == 5
    assert "1441455" in {s.external_id for s in stubs}
    assert all(s.list_version == "2026-11-07" for s in stubs)
    job = details["1441455"].normalized()
    assert (job.title, job.company) == ("Board Member (2026-28 Term)", "Pittsburgh Equality Center")
    assert job.remote_type is RemoteType.REMOTE
    assert job.posted_at is not None
    assert job.posted_at.date().isoformat() == "2026-10-24"
    assert job.expires_at is not None
    assert job.expires_at.date().isoformat() == "2026-11-26"
    assert job.description_text
    assert "board" in job.description_text.lower()


async def test_structured_location_and_salary() -> None:
    host = "jobs.forcebrands.com"
    path = "/job/2555753-market-development-representative-on-premise-lafayette-sazerac"
    routes = {
        **fixture_routes("niceboard", {f"{host}{path}": "job_2555753.html"}),
        **sitemap_route(f"https://{host}/sitemap.xml", f"https://{host}{path}"),
    }
    _, details = await crawl_fixture(NiceboardConnector, host, routes)
    job = details["2555753"].normalized()
    assert (job.company, job.city, job.country) == ("Sazerac", "Lafayette", "US")


@pytest.mark.live
@pytest.mark.parametrize("board", ["jobs.nonprofittalent.com", "jobs.forcebrands.com"])
async def test_live(board: str) -> None:
    await live_check(NiceboardConnector, board)
