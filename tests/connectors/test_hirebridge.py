"""HireBridge connector (HTML list + HTML job pages).

Fixtures (captured 2026-11-07; <style>, <script>, <link>, <svg>, __VIEWSTATE removed):
    eab_list.html         GET https://recruit.hirebridge.com/v3/Jobs/list.aspx?cid=7856
                          (99 links = 44 jobs, some at several locations)
    eab_job_604256.html   GET .../v3/Jobs/JobDetails.aspx?cid=7856&jid=604256&locvalue=1036
    eab_job_unknown.html  GET .../JobDetails.aspx?cid=7856&jid=1&locvalue=1036 (empty fields)
    menard_job_413822.html  GET .../JobDetails.aspx?cid=5535&jid=413822&bid=1 (2026-11-08;
                          the list shows no location for Menard jobs, the page does)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.hirebridge import HireBridgeConnector
from jobindex.http import HttpClient
from tests.fakes import load_fixture, mock_http

HOST = "recruit.hirebridge.com"
DETAILS = f"{HOST}/v3/Jobs/JobDetails.aspx"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}/v3/Jobs/list.aspx?cid=7856", "7856"),
        (f"https://{HOST}/v3/jobs/JobDetails.aspx?CID=5535&jid=1&locvalue=2", "5535"),
        (f"https://{HOST}/v3/Jobs/list.aspx", None),
        (f"https://{HOST}/v3/Application/AppErrMsg.aspx?cid=7856", None),
        ("https://www.hirebridge.com/", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert HireBridgeConnector.detect(url) == expected


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("hirebridge", name))


def _detail(request: httpx.Request) -> httpx.Response:
    jid = request.url.params["jid"]
    return _page("eab_job_604256.html" if jid == "604256" else "eab_job_unknown.html")


async def test_list_groups_locations_and_detail() -> None:
    http, _ = mock_http({f"{HOST}/v3/Jobs/list.aspx": _page("eab_list.html"), DETAILS: _detail})
    async with http:
        connector = HireBridgeConnector(http)
        jobs = await connector.fetch_jobs("7856")
        by_id = {j.external_id: j for j in jobs}
        detail = await connector.fetch_job_detail("7856", by_id["604256"])
        stub = RawJob(external_id="1", title="x", source_url=f"https://{DETAILS}?cid=7856&jid=1")
        closed = await connector.fetch_job_detail("7856", stub)
    assert len(jobs) == 44
    director = by_id["612567"]
    assert director.title == "Account Director, Hybrid"
    assert director.location_raw is not None
    assert director.location_raw.count(";") == 2  # three locations, one posting
    assert director.company == "EAB"
    assert detail is not None
    assert detail.employment_type == "full_time"
    assert detail.description_html is not None
    assert "About EAB" in detail.description_html
    assert closed is None


async def test_unknown_cid_and_changed_template() -> None:
    def error_page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cid") == "1":
            return httpx.Response(200, text="<html><body>no ids</body></html>")
        location = f"https://{HOST}/v3/Application/AppErrMsg.aspx?cid=9&errorType=badurl"
        return httpx.Response(302, headers={"Location": location})

    http, _ = mock_http(
        {
            f"{HOST}/v3/Jobs/list.aspx": error_page,
            f"{HOST}/v3/Application/AppErrMsg.aspx": httpx.Response(200, text="<html>x</html>"),
            DETAILS: error_page,
        }
    )
    async with http:
        with pytest.raises(ConnectorError, match="redirected"):
            await HireBridgeConnector(http).fetch_jobs("9")
        stub = RawJob(external_id="2", title="x", source_url=f"https://{DETAILS}?cid=1&jid=2")
        with pytest.raises(ConnectorError, match="template changed"):
            await HireBridgeConnector(http).fetch_job_detail("1", stub)


@pytest.mark.live
@pytest.mark.parametrize("cid", ["7856", "5535"])
async def test_live(cid: str) -> None:
    async with HttpClient.from_settings() as http:
        connector = HireBridgeConnector(http)
        jobs = await connector.fetch_jobs(cid)
        assert len(jobs) > 0
        detail = await connector.fetch_job_detail(cid, jobs[0])
    assert all(j.title and j.source_url for j in jobs)
    assert detail is not None
    assert detail.description_html


async def test_location_from_job_page_when_the_list_has_none() -> None:
    page = load_fixture("hirebridge", "menard_job_413822.html")
    http, _ = mock_http({DETAILS: httpx.Response(200, text=page)})
    url = f"https://{DETAILS}?cid=5535&jid=413822&bid=1"
    stub = RawJob(external_id="413822", title="Part-Time Sales", source_url=url)
    async with http:
        job = await HireBridgeConnector(http).fetch_job_detail("5535", stub)
    assert job is not None
    assert job.location_raw == "EFFINGHAM, IL"
    assert job.employment_type == "part_time"


def test_cache_key_is_the_list_location_not_the_stored_one() -> None:
    # location_raw is a detail field; the list location lives in list_version
    assert "location_raw" in HireBridgeConnector.detail_fields
    assert "location_raw" not in HireBridgeConnector.detail_cache_key
