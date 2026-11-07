"""Comeet connector.

Fixture (captured 2026-11-07):
    safefleet.html   GET https://www.comeet.com/jobs/safefleet/95.001, trimmed to the
                     <title> and the inline script with COMPANY_DATA (token redacted) and
                     COMPANY_POSITIONS_DATA, cut to 3 of 58 positions (on-site, remote,
                     hybrid with ``is_remote: true``).
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.comeet import ComeetConnector
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_fixture, mock_http

BOARD = "safefleet/95.001"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.comeet.com/jobs/safefleet/95.001", BOARD),
        ("https://www.comeet.com/jobs/SafeFleet/95.001/", BOARD),
        ("https://www.comeet.com/jobs/safefleet/95.001/3rd-shift-machine-operator/D4.466", BOARD),
        ("https://comeet.co/jobs/vastdata/43.001", "vastdata/43.001"),
        ("https://www.comeet.com/jobs/vastdata/E0.00B", "vastdata/E0.00B"),
        ("https://www.comeet.com/jobs/safefleet", None),  # no company uid
        ("https://www.comeet.com/jobs/safefleet/careers", None),
        ("https://www.comeet.com/resources/", None),
        ("https://www.safefleet.net/careers", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert ComeetConnector.detect(url) == expected


async def test_fetch_jobs_from_fixture() -> None:
    page = httpx.Response(200, text=load_fixture("comeet", "safefleet.html"))
    http, _ = mock_http({"www.comeet.com/jobs/safefleet/95.001": page})
    async with http:
        onsite, remote, hybrid = await ComeetConnector(http).fetch_jobs(BOARD)
        company = await ComeetConnector(http).fetch_company_name(BOARD)
    assert company == "Safe Fleet"
    assert (onsite.external_id, onsite.title) == ("D4.466", "3rd Shift Machine Operator")
    assert onsite.company == "Safe Fleet"
    assert (onsite.city, onsite.region, onsite.country) == ("Eagan", "MN", "US")
    assert onsite.remote_type is RemoteType.ONSITE
    assert onsite.employment_type == "full_time"
    assert onsite.description_html is not None
    assert "<h3>Description</h3>" in onsite.description_html
    assert "<h3>Requirements</h3>" not in onsite.description_html  # empty section skipped
    assert remote.description_html is not None
    assert "<h3>Requirements</h3>" in remote.description_html
    assert onsite.source_url == (
        "https://www.comeet.com/jobs/safefleet/95.001/3rd-shift-machine-operator/D4.466"
    )
    assert remote.remote_type is RemoteType.REMOTE
    assert hybrid.remote_type is RemoteType.HYBRID  # workplace_type beats is_remote
    job = onsite.normalized()
    assert job.description_text
    assert job.description_text.startswith("Description")


async def test_unknown_board_raises() -> None:
    # Unknown boards redirect to the Comeet home page (no positions data).
    home = httpx.Response(200, text="<html><head><title>Comeet</title></head></html>")
    http, _ = mock_http({"www.comeet.com/jobs/nosuch/00.000": home})
    async with http:
        with pytest.raises(ConnectorError, match="COMPANY_POSITIONS_DATA"):
            await ComeetConnector(http).fetch_jobs("nosuch/00.000")


async def test_malformed_positions_raise() -> None:
    page = "<script>COMPANY_DATA = {};\nCOMPANY_POSITIONS_DATA = [{oops}];\n</script>"
    http, _ = mock_http({"www.comeet.com/jobs/x/00.000": httpx.Response(200, text=page)})
    async with http:
        with pytest.raises(ConnectorError, match="not valid JSON"):
            await ComeetConnector(http).fetch_jobs("x/00.000")


@pytest.mark.live
@pytest.mark.parametrize("board", ["safefleet/95.001", "vastdata/43.001"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await ComeetConnector(http).fetch_jobs(board)
    assert len(jobs) > 0
    assert all(j.title and j.description_html and j.source_url for j in jobs)
