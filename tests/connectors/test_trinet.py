"""TriNet Hire connector (HTML list + HTML job pages).

Fixtures (captured 2026-11-07; <style>, <script>, <link>, <svg> removed):
    curative_jobs.html        GET https://app.trinethire.com/companies/130621-curative-hr-llc/jobs
                              (18 jobs)
    curative_job_120420.html  GET .../companies/130621-curative-hr-llc/jobs/
                              120420-sr-help-desk-technician (salary $95,000 - $110,000)
"""

from decimal import Decimal

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.trinet import TriNetConnector
from jobindex.http import FetchError, HttpClient
from tests.fakes import load_fixture, mock_http

HOST = "app.trinethire.com"
JOB = f"{HOST}/companies/130621-curative-hr-llc/jobs/120420-sr-help-desk-technician"
OLD = "/companies/130621-curative-hr-llc/jobs/1-old-job"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}/companies/130621-curative-hr-llc/jobs", "130621"),
        (f"https://{HOST}/companies/593631/jobs/110210-software-engineer", "593631"),
        (f"https://{JOB}", "130621"),
        (f"https://{HOST}/companies/", None),
        (f"https://{HOST}/login", None),
        ("https://www.trinet.com/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert TriNetConnector.detect(url) == expected


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("trinet", name))


async def test_list_and_detail_from_fixture() -> None:
    http, _ = mock_http(
        {
            f"{HOST}/companies/130621/jobs": _page("curative_jobs.html"),
            JOB: _page("curative_job_120420.html"),
            f"{HOST}{OLD}": httpx.Response(404),
        }
    )
    async with http:
        connector = TriNetConnector(http)
        jobs = await connector.fetch_jobs("130621")
        by_id = {j.external_id: j for j in jobs}
        detail = await connector.fetch_job_detail("130621", by_id["120420"])
        old = RawJob(external_id="1", title="x", source_url=f"https://{HOST}{OLD}")
        gone = await connector.fetch_job_detail("130621", old)
    assert len(jobs) == 18
    help_desk = by_id["120420"]
    assert help_desk.title == "Sr. Help Desk Technician"
    assert help_desk.company == "Curative HR LLC"
    assert help_desk.employment_type == "full_time"
    assert help_desk.source_url == f"https://{JOB}"
    assert detail is not None
    assert detail.description_html is not None
    assert "About Curative" in detail.description_html
    assert (detail.salary_min, detail.salary_max, detail.salary_currency) == (
        Decimal(95000),
        Decimal(110000),
        "USD",
    )
    assert gone is None


async def test_unknown_company_and_changed_template() -> None:
    http, _ = mock_http(
        {f"{HOST}/companies/2/jobs": httpx.Response(200, text="<html><title>x</title></html>")}
    )
    async with http:
        with pytest.raises(FetchError, match="404"):
            await TriNetConnector(http).fetch_jobs("999999999")
        with pytest.raises(ConnectorError, match="no job table"):
            await TriNetConnector(http).fetch_jobs("2")


@pytest.mark.live
@pytest.mark.parametrize("company", ["130621", "203100"])
async def test_live(company: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = TriNetConnector(http)
        jobs = await connector.fetch_jobs(company)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(company, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html
