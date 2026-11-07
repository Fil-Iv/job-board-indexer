"""JBoard.io connector (generic sitemap + JSON-LD).

Fixtures (captured 2026-11-07, trimmed):
    sitemap.xml          GET https://nuclearjobscanada.ca/sitemap.xml (index, verbatim)
    sitemap-jobs-1.xml   GET https://nuclearjobscanada.ca/sitemap-jobs-1.xml (6 of 939; no
                         <lastmod> on JBoard)
    job_164823949.html   GET https://nuclearjobscanada.ca/jobs/164823949-technicien-ne-mecanique
                         (keeps an excerpt of the inline config naming app.jboard.io)
"""

import pytest

from jobindex.connectors.jboard import JBoardConnector
from tests.connectors.jsonld_helpers import crawl_fixture, fixture_routes, live_check
from tests.fakes import load_fixture

BOARD = "nuclearjobscanada.ca"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.jboard.io/jobs/123456-engineer", "acme.jboard.io"),
        ("https://app.jboard.io/api", None),  # platform admin
        ("https://nuclearjobscanada.ca/", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JBoardConnector.detect(url) == expected


def test_custom_domain_detected_from_page() -> None:
    page = load_fixture("jboard", "job_164823949.html")
    assert JBoardConnector.detect_in_html(page) == {"nuclearjobscanada.ca": 2}


async def test_fetch_from_fixture() -> None:
    routes = fixture_routes(
        "jboard",
        {
            f"{BOARD}/sitemap.xml": "sitemap.xml",  # children other than jobs-N: not read
            f"{BOARD}/sitemap-jobs-1.xml": "sitemap-jobs-1.xml",
            f"{BOARD}/jobs/164823949-technicien-ne-mecanique": "job_164823949.html",
        },
    )
    stubs, details = await crawl_fixture(JBoardConnector, BOARD, routes)
    assert len(stubs) == 6
    assert all((s.list_version or "").startswith("refresh:") for s in stubs)  # no lastmod
    job = details["164823949"].normalized()
    assert (job.title, job.company) == ("Technicien(ne), Mécanique", "AtkinsRéalis")
    assert job.city == "Jonquière"
    assert job.expires_at is not None
    assert job.expires_at.date().isoformat() == "2026-12-03"
    assert job.description_text
    assert len(job.description_text) > 1000


@pytest.mark.live
@pytest.mark.parametrize("board", ["nuclearjobscanada.ca", "couriergigs.com"])
async def test_live(board: str) -> None:
    await live_check(JBoardConnector, board)
