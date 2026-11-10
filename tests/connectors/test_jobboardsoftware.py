"""Exenzo (jobboardsoftware.com) connector.

Fixtures (captured 2026-11-10; recruiter_* / apply_email fields set to null and e-mail
addresses in the texts replaced by contact@example.org, the repository is public):
    overheid_configuration.json  GET https://overheid.api.exenzo.com/api/public/v1/
                                 configuration (trimmed to settings subset, job_boards,
                                 sites)
    bb_configuration.json        GET https://bb.api.exenzo.com/api/public/v1/configuration
                                 (same trim; two boards, slugs binnenlands-bestuur[2])
    overheid_jobs_page1.json     GET .../job-boards/1/jobs?page=1&limit=100 (38 jobs, data
                                 trimmed to the first 3, aggregations removed)
    greenjobs_jobs_page1.json    GET https://greenjobs.api.exenzo.com/api/public/v1/
                                 job-boards/1/jobs?page=1&limit=100 (230 jobs; data trimmed
                                 to 3: remote, hourly salary, no salary; aggregations removed)
"""

import copy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.jobboardsoftware import JobBoardSoftwareConnector
from jobindex.connectors.registry import ResolvedBoard, default_registry
from jobindex.http import HttpClient
from jobindex.models.enums import RemoteType
from tests.fakes import load_json_fixture, mock_http

PLATFORM = "jobboardsoftware"
OVERHEID = "overheid.api.exenzo.com/api/public/v1"
GREENJOBS = "greenjobs.api.exenzo.com/api/public/v1"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://overheid.api.exenzo.com/api/public/v1/configuration", "overheid/1"),
        ("https://bb.api.exenzo.com/api/public/v1/job-boards/2/jobs?page=1", "bb/2"),
        (
            "https://storage.googleapis.com/exenzo-jobboard/greenjobs/public/thumb/logo.png",
            "greenjobs/1",
        ),
        ("https://storage.googleapis.com/other-bucket/greenjobs/x.png", None),
        ("https://api.exenzo.com/api/public/v1/configuration", None),
        ("https://exenzo.com/en/jobboard", None),
        ("https://greenjobs.nl/vacature/x", None),  # custom domain: needs the page
    ],
)
def test_detect(url: str, expected: str | None) -> None:
    assert JobBoardSoftwareConnector.detect(url) == expected


async def test_custom_domain_resolves_from_asset_urls() -> None:
    page = (
        '<img src="https://storage.googleapis.com/exenzo-jobboard/overheid/public/thumb/a.png">'
        '<img src="https://overheid.api.exenzo.com/api/public/v1/image/vbdo-profile-png">'
    )
    http, _ = mock_http({"vacaturebijdeoverheid.nl/": httpx.Response(200, text=page)})
    async with http:
        board = await default_registry().resolve_url("https://vacaturebijdeoverheid.nl/", http)
    assert board == ResolvedBoard(PLATFORM, "overheid/1")


def _routes(api: str, config: str, jobs: Any) -> dict[str, Any]:
    return {
        f"{api}/configuration": httpx.Response(200, json=load_json_fixture(PLATFORM, config)),
        f"{api}/job-boards/1/jobs": jobs,
    }


def _single_page(fixture: str) -> httpx.Response:
    body = load_json_fixture(PLATFORM, fixture)
    body.update(total=len(body["data"]), lastPage=1)
    return httpx.Response(200, json=body)


async def test_map_from_fixture() -> None:
    routes = _routes(
        OVERHEID, "overheid_configuration.json", _single_page("overheid_jobs_page1.json")
    )
    http, seen = mock_http(routes)
    async with http:
        jobs = await JobBoardSoftwareConnector(http).fetch_jobs("overheid/1")
    assert [j.external_id for j in jobs] == ["7644", "7643", "7642"]
    job = jobs[0].normalized()
    assert (job.title, job.company) == (
        "Senior projectleider gebiedsontwikkeling",
        "Gemeente Schagen",
    )
    assert (job.city, job.country) == ("Schagen", "NL")
    assert job.employment_type == "full_time"
    assert (job.salary_min, job.salary_currency, job.salary_period) == (
        Decimal(5369),
        "EUR",
        "month",
    )
    # from/till are Europe/Amsterdam local midnight (CET = UTC+1 in November)
    assert job.posted_at == datetime(2026, 11, 9, 23, 0, tzinfo=UTC)
    assert job.expires_at == datetime(2026, 11, 28, 23, 0, tzinfo=UTC)
    assert job.source_url == (
        "https://vacaturebijdeoverheid.nl/vacature/senior-projectleider-gebiedsontwikkeling-se"
    )
    assert job.apply_url
    assert job.apply_url.startswith("https://")
    assert job.description_text
    assert "Schagen" in job.description_text
    # city holding a whole address ("Voorstraat, 2201 Noordwijk, 2201")
    assert jobs[2].city == "Noordwijk"
    lists = [r for r in seen if r.url.path.endswith("/jobs")]
    assert [(r.url.params["page"], r.url.params["limit"]) for r in lists] == [("1", "100")]


async def test_remote_hourly_and_missing_salary() -> None:
    # greenjobs' configuration is not saved; reuse overheid's with greenjobs' board slug
    config = load_json_fixture(PLATFORM, "overheid_configuration.json")
    config["job_boards"][0].update(slug="greenjobs", host="greenjobs.nl")
    http, _ = mock_http(
        {
            f"{GREENJOBS}/configuration": httpx.Response(200, json=config),
            f"{GREENJOBS}/job-boards/1/jobs": _single_page("greenjobs_jobs_page1.json"),
        }
    )
    async with http:
        remote, hourly, unpaid = await JobBoardSoftwareConnector(http).fetch_jobs("greenjobs/1")
    assert remote.remote_type is RemoteType.REMOTE
    assert remote.city is None
    assert remote.country == "NL"
    assert (remote.salary_min, remote.salary_period) == (Decimal(85000), "year")
    assert (hourly.salary_min, hourly.salary_period) == (Decimal(16), "hour")
    assert hourly.remote_type is RemoteType.UNKNOWN  # remote: null = not stated
    assert (unpaid.salary_min, unpaid.salary_max, unpaid.salary_currency) == (None, None, None)
    assert unpaid.employment_type == "internship"  # "Stage" listed first
    assert remote.source_url == "https://greenjobs.nl/vacature/adviseur-netcongestie-q6"


def _paged(n: int, total: int, page_size: int) -> Any:
    template = load_json_fixture(PLATFORM, "overheid_jobs_page1.json")["data"]
    rows = []
    for i in range(n):
        row = copy.deepcopy(template[i % len(template)])
        row["id"] = 1000 + i
        rows.append(row)
    last_page = max(1, -(-n // page_size))

    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        chunk = rows[(page - 1) * page_size : page * page_size]
        body = {"total": total, "lastPage": last_page, "currentPage": page, "data": chunk}
        return httpx.Response(200, json=body)

    return handle


async def test_follows_pages_until_last_page() -> None:
    http, seen = mock_http(_routes(OVERHEID, "overheid_configuration.json", _paged(250, 250, 100)))
    async with http:
        jobs = await JobBoardSoftwareConnector(http).fetch_jobs("overheid/1")
    assert len(jobs) == 250
    pages = [r.url.params["page"] for r in seen if r.url.path.endswith("/jobs")]
    assert pages == ["1", "2", "3"]


async def test_count_mismatch_is_refused_after_one_retry() -> None:
    http, seen = mock_http(_routes(OVERHEID, "overheid_configuration.json", _paged(4, 6, 2)))
    async with http:
        with pytest.raises(ConnectorError, match="got 4 of 6"):
            await JobBoardSoftwareConnector(http).fetch_jobs("overheid/1")
    assert len([r for r in seen if r.url.path.endswith("/jobs")]) == 4  # listed twice


async def test_failed_page_raises() -> None:
    handler = _paged(250, 250, 100)

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "2":
            return httpx.Response(404)
        return handler(request)  # type: ignore[no-any-return]

    http, _ = mock_http(_routes(OVERHEID, "overheid_configuration.json", flaky))
    async with http:
        with pytest.raises(Exception, match="404"):
            await JobBoardSoftwareConnector(http).fetch_jobs("overheid/1")


async def test_unknown_board_id_is_refused() -> None:
    http, _ = mock_http(
        {
            "bb.api.exenzo.com/api/public/v1/configuration": httpx.Response(
                200, json=load_json_fixture(PLATFORM, "bb_configuration.json")
            )
        }
    )
    async with http:
        with pytest.raises(ConnectorError, match="job board 3"):
            await JobBoardSoftwareConnector(http).fetch_jobs("bb/3")


@pytest.mark.live
@pytest.mark.parametrize("board", ["overheid/1", "bb/1"])
async def test_live(board: str) -> None:
    async with HttpClient.from_settings() as http:
        jobs = await JobBoardSoftwareConnector(http).fetch_jobs(board)
    assert len(jobs) > 0
    assert all(j.title and j.company and j.source_url and j.description_html for j in jobs)
