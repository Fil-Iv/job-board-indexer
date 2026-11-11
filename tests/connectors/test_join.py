"""JOIN connector (paged __NEXT_DATA__ list + JSON-LD job pages).

Fixtures (captured 2026-11-11; Next.js ``messages`` (UI strings), the Sentry DSN and
inline ``<style>`` removed):
    bring_page1.html   GET https://join.com/companies/bring (4 jobs, 1 page)
    zbv_page1.html     GET https://join.com/companies/zbv-automationcom (6 jobs, page 1/2)
    zbv_page2.html     GET https://join.com/companies/zbv-automationcom?page=2
    hallo_theo.html    GET https://join.com/companies/hallo-theo (older layout:
                       initialState instead of pageData, 1 job)
    bring_job.html     GET https://join.com/companies/bring/16935431-senior-accountant-und-
                       controller-80-100-w-m-x (the listed ad 16880565 301-redirects here)
    zbv_job_ausbildung.html  GET https://join.com/companies/zbv-automationcom/16904076-
                       ausbildung-zum-mechatroniker-m-w-d (JSON-LD INTERN, name Ausbildung)
"""

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.join import JoinConnector
from jobindex.models.enums import RemoteType
from tests.connectors.jsonld_helpers import live_check
from tests.fakes import load_fixture, mock_http

ZBV = "join.com/companies/zbv-automationcom"


def _page(name: str) -> httpx.Response:
    return httpx.Response(200, text=load_fixture("join", name))


def _zbv(request: httpx.Request) -> httpx.Response:
    return _page("zbv_page2.html" if request.url.params.get("page") == "2" else "zbv_page1.html")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://join.com/companies/bring", "bring"),
        ("https://join.com/companies/Bring/16880565-senior-accountant", "bring"),
        ("https://join.com/companies/sitemap.xml", None),
        ("https://join.com/companies", None),
        ("https://join.com/pricing", None),
        ("https://bring.join.com/careers", None),
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JoinConnector.detect(url) == expected


async def test_pages_are_read_until_the_total() -> None:
    http, seen = mock_http({ZBV: _zbv})
    async with http:
        stubs = await JoinConnector(http).fetch_jobs("zbv-automationcom")
    assert len(stubs) == 6  # 5 on page 1 + 1 on page 2 = pagination.total
    assert [r.url.params.get("page") for r in seen if r.url.path != "/robots.txt"] == [None, "2"]
    first = next(s for s in stubs if s.external_id == "16852907")  # the job id, not the ad
    assert first.source_url == (
        "https://join.com/companies/zbv-automationcom/16904076-ausbildung-zum-mechatroniker-m-w-d"
    )
    assert first.list_version


async def test_older_layout() -> None:
    http, _ = mock_http({"join.com/companies/hallo-theo": _page("hallo_theo.html")})
    async with http:
        stubs = await JoinConnector(http).fetch_jobs("hallo-theo")
    assert [s.external_id for s in stubs] == ["14956116"]


async def test_missing_page_is_refused() -> None:
    http, _ = mock_http({ZBV: _page("zbv_page1.html")})  # ?page=2 answers page 1 again
    async with http:
        with pytest.raises(ConnectorError, match="returned page 1"):
            await JoinConnector(http).fetch_jobs("zbv-automationcom")


async def test_more_than_100_jobs_is_refused() -> None:
    # the site serves 20 pages of 5 at most (simplyrightde: 101 jobs, ?page=21 -> page 20)
    html = load_fixture("join", "zbv_page1.html").replace('"pageCount":2,', '"pageCount":21,')
    http, _ = mock_http({ZBV: httpx.Response(200, text=html)})
    async with http:
        with pytest.raises(ConnectorError, match="at most 20 pages"):
            await JoinConnector(http).fetch_jobs("zbv-automationcom")


async def test_job_page() -> None:
    listed = "16880565-senior-accountant-und-controller-80-100-w-m-x"
    current = "16935431-senior-accountant-und-controller-80-100-w-m-x"

    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301, headers={"location": f"https://join.com/companies/bring/{current}"}
        )

    http, _ = mock_http(
        {
            "join.com/companies/bring": _page("bring_page1.html"),
            f"join.com/companies/bring/{listed}": redirect,
            f"join.com/companies/bring/{current}": _page("bring_job.html"),
        }
    )
    async with http:
        connector = JoinConnector(http)
        stubs = {s.external_id: s for s in await connector.fetch_jobs("bring")}
        assert len(stubs) == 4
        job = await connector.fetch_job_detail("bring", stubs["16880565"])
        assert await connector.fetch_company_name("bring") == "Bring! Labs AG"
    assert job is not None
    job = job.normalized()
    assert job.title == "(Senior) Accountant & Controller 80-100% (w/m/x)"
    assert job.company == "Bring! Labs AG"
    assert (job.city, job.country) == ("Zürich", "CH")
    assert job.employment_type == "full_time"
    assert job.remote_type is RemoteType.ONSITE  # pageData.job.workplaceType
    assert job.expires_at is None
    assert (job.description_text or "").startswith("Deine Aufgaben")


async def test_apprenticeship_is_not_an_internship() -> None:
    path = "/companies/zbv-automationcom/16904076-ausbildung-zum-mechatroniker-m-w-d"
    http, _ = mock_http({ZBV: _zbv, f"join.com{path}": _page("zbv_job_ausbildung.html")})
    async with http:
        connector = JoinConnector(http)
        stubs = {s.external_id: s for s in await connector.fetch_jobs("zbv-automationcom")}
        job = await connector.fetch_job_detail("zbv-automationcom", stubs["16852907"])
    assert job is not None
    assert job.title == "Ausbildung zum Mechatroniker (m/w/d)"
    assert job.employment_type == "apprenticeship"


@pytest.mark.live
@pytest.mark.parametrize("board", ["bring", "zbv-automationcom"])
async def test_live(board: str) -> None:
    await live_check(JoinConnector, board)
