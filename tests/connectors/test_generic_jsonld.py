"""Generic sitemap + JSON-LD connector (base class) and its crawler integration.

Synthetic boards keep the edge cases readable; per-platform tests (test_niceboard.py, ...)
run the same code on saved real responses.
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.checkpoint import MemoryCheckpoint
from jobindex.connectors import generic_jsonld
from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.generic_jsonld import (
    HostedBoardConnector,
    _dump_job,
    _load_job,
    refresh_token,
)
from jobindex.connectors.registry import Registry
from jobindex.crawler import crawl_source
from jobindex.http import FetchError
from jobindex.models import FetchStatus, Job, JobStatus, RemoteType, Source
from tests.fakes import mock_http

HOST = "board.test"


class ExampleConnector(HostedBoardConnector):
    platform = "example-jsonld"
    native_suffix = ".example-boards.test"
    page_fingerprint = re.compile(r"cdn\.example-boards\.test")
    child_sitemap = re.compile(r"sitemap-jobs")
    job_path = re.compile(r"/job/(?P<id>\d+)-[^/]*")


def _page(job_id: str, *, valid_through: str = "2099-01-01", body: str = "") -> str:
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"@context":"https://schema.org","@type":"JobPosting","title":"Engineer {job_id}",'
        '"description":"<p>Build things</p>","datePosted":"2026-11-01",'
        f'"validThrough":"{valid_through}","employmentType":"FULL_TIME",'
        '"hiringOrganization":{"@type":"Organization","name":"Acme"},'
        '"jobLocation":{"@type":"Place","address":{"addressLocality":"Berlin",'
        '"addressCountry":"DE"}},'
        '"baseSalary":{"@type":"MonetaryAmount","currency":"EUR",'
        '"value":{"@type":"QuantitativeValue","minValue":50000,"maxValue":60000,'
        '"unitText":"YEAR"}}}'
        f"</script></head><body><h1>Engineer {job_id}</h1>{body}</body></html>"
    )


def _sitemap(*entries: tuple[str, str | None]) -> str:
    urls = "".join(
        f"<url><loc>https://{HOST}{path}</loc>"
        + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
        + "</url>"
        for path, lastmod in entries
    )
    return f'<?xml version="1.0"?><urlset>{urls}</urlset>'


INDEX = (
    '<?xml version="1.0"?><sitemapindex>'
    f"<sitemap><loc>https://{HOST}/sitemap-pages.xml</loc></sitemap>"
    f"<sitemap><loc>https://{HOST}/sitemap-jobs.xml</loc></sitemap>"
    "</sitemapindex>"
)


def _routes(jobs_sitemap: str, pages: dict[str, httpx.Response]) -> dict[str, Any]:
    return {
        f"{HOST}/robots.txt": httpx.Response(200, text=f"Sitemap: https://{HOST}/sitemap.xml\n"),
        f"{HOST}/sitemap.xml": httpx.Response(200, text=INDEX),
        f"{HOST}/sitemap-pages.xml": httpx.Response(500),  # not followed (child_sitemap)
        f"{HOST}/sitemap-jobs.xml": httpx.Response(200, text=jobs_sitemap),
        **{f"{HOST}{path}": response for path, response in pages.items()},
    }


def _stub(job_id: str, version: str = "2026-11-01") -> RawJob:
    url = f"https://{HOST}/job/{job_id}-engineer"
    return RawJob(external_id=job_id, title="", source_url=url, list_version=version)


# -- listing -------------------------------------------------------------------


async def test_lists_job_urls_from_sitemap_index_with_versions() -> None:
    sitemap = _sitemap(
        ("/job/1-engineer", "2026-11-01"),
        ("/job/1-engineer-old-slug", "2026-10-01"),  # same id: newest lastmod wins
        ("/job/2-designer", None),
        ("/jobs/full-time", "2026-11-01"),  # listing page
        ("/company/7-acme", "2026-11-01"),
    )
    http, seen = mock_http(_routes(sitemap, {}))
    async with http:
        jobs = await ExampleConnector(http).fetch_jobs(HOST)
    assert [(j.external_id, j.source_url) for j in jobs] == [
        ("1", f"https://{HOST}/job/1-engineer"),
        ("2", f"https://{HOST}/job/2-designer"),
    ]
    assert jobs[0].list_version == "2026-11-01"
    assert (jobs[1].list_version or "").startswith("refresh:7:")  # no lastmod
    assert all(r.url.path != "/sitemap-pages.xml" for r in seen)


async def test_detail_version_forces_one_refetch_of_stored_pages() -> None:
    class Reparsed(ExampleConnector):
        detail_version = 2

    sitemap = _sitemap(("/job/1-engineer", "2026-11-01"), ("/job/2-designer", None))
    http, _ = mock_http(_routes(sitemap, {}))
    async with http:
        jobs = await Reparsed(http).fetch_jobs(HOST)
    assert jobs[0].list_version == "2026-11-01#v2"  # differs from the stored "2026-11-01"
    assert (jobs[1].list_version or "").endswith("#v2")


async def test_native_host_redirecting_to_custom_domain() -> None:
    # e.g. alanet.mysmartjobboard.com -> jobs.alanet.org: the sitemap lists the latter.
    native = "acme.example-boards.test"
    moved = httpx.Response(301, headers={"Location": f"https://{HOST}/sitemap.xml"})
    sitemap = _sitemap(("/job/1-engineer", "2026-11-01"))
    http, _ = mock_http({**_routes(sitemap, {}), f"{native}/sitemap.xml": moved})
    async with http:
        jobs = await ExampleConnector(http).fetch_jobs(native)
    assert [j.source_url for j in jobs] == [f"https://{HOST}/job/1-engineer"]


def test_refresh_token_changes_weekly_on_a_different_day_per_url() -> None:
    urls = [f"https://{HOST}/job/{i}-x" for i in range(200)]
    switch_days = set()
    for url in urls:
        tokens = [refresh_token(url, 7, day) for day in range(9800, 9807)]
        changes = [d for d in range(1, 7) if tokens[d] != tokens[d - 1]]
        assert len(changes) <= 1  # at most one change within any 7 days
        switch_days.add(changes[0] if changes else 0)
    assert switch_days == set(range(7))  # spread over the week


async def test_failed_or_invalid_sitemap_raises() -> None:
    http, _ = mock_http({**_routes("", {}), f"{HOST}/sitemap-jobs.xml": httpx.Response(503)})
    async with http:
        with pytest.raises(FetchError):
            await ExampleConnector(http).fetch_jobs(HOST)
    html = httpx.Response(200, text="<html>maintenance</html>")
    http, _ = mock_http({**_routes("", {}), f"{HOST}/sitemap-jobs.xml": html})
    async with http:
        with pytest.raises(ConnectorError, match="not a sitemap"):
            await ExampleConnector(http).fetch_jobs(HOST)


def test_detect() -> None:
    assert ExampleConnector.detect("https://acme.example-boards.test/job/1-x") == (
        "acme.example-boards.test"
    )
    assert ExampleConnector.detect("https://www.example-boards.test/") is None
    assert ExampleConnector.detect("https://jobs.acme.com/") is None
    page = (
        '<link rel="canonical" href="https://jobs.acme.com/job/1-x">'
        '<script src="https://cdn.example-boards.test/app.js"></script>'
    )
    assert ExampleConnector.detect_in_html(page) == {"jobs.acme.com": 1}
    assert ExampleConnector.detect_in_html(page.replace("cdn.example", "cdn.other")) == {}
    # no canonical URL (SuccessFactors RMK home pages): the page's own URL is the board
    bare = '<script src="https://cdn.example-boards.test/app.js"></script>'
    assert ExampleConnector.detect_in_html(bare, "https://jobs.acme.com/") == {"jobs.acme.com": 1}
    assert ExampleConnector.detect_in_html(bare) == {}


# -- job pages -------------------------------------------------------------------


async def test_job_page_maps_jsonld() -> None:
    page = httpx.Response(200, text=_page("1"))
    http, _ = mock_http(_routes("", {"/job/1-engineer": page}))
    async with http:
        job = await ExampleConnector(http).fetch_job_detail(HOST, _stub("1"))
    assert job is not None
    assert (job.external_id, job.title, job.company) == ("1", "Engineer 1", "Acme")
    assert (job.city, job.country, job.employment_type) == ("Berlin", "DE", "full_time")
    assert (job.salary_min, job.salary_currency, job.salary_period) == (
        Decimal(50000),
        "EUR",
        "year",
    )
    assert job.list_version == "2026-11-01"
    # a date-only validThrough is valid through the end of that day
    assert job.expires_at == datetime(2099, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("response", "why"),
    [
        (httpx.Response(200, text=_page("1", valid_through="2020-01-01")), "validThrough"),
        (
            httpx.Response(200, text=_page("1", body="<p>This job is no longer available.</p>")),
            "closed notice next to a still-present JobPosting",
        ),
        (
            httpx.Response(200, text="<html><title>X</title><p>This position has been filled</p>"),
            "closed notice, no JobPosting",
        ),
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(410), "HTTP 410"),
    ],
)
async def test_closed_postings_come_back_expired(response: httpx.Response, why: str) -> None:
    http, _ = mock_http(_routes("", {"/job/1-engineer": response}))
    async with http:
        job = await ExampleConnector(http).fetch_job_detail(HOST, _stub("1"))
    assert job is not None, why
    assert job.expires_at is not None, why
    assert job.expires_at <= datetime.now(UTC), why
    assert job.list_version == "2026-11-01"


@pytest.mark.parametrize(
    "text",
    [
        "applications are accepted until the position is filled.",  # JobMount boilerplate
        "the posting will remain open until the job is filled",
        "apply now. once the role is filled we will contact all applicants",
    ],
)
async def test_conditional_closed_wording_is_not_a_closed_notice(text: str) -> None:
    page = httpx.Response(200, text=_page("1", body=f"<p>{text}</p>"))
    http, _ = mock_http(_routes("", {"/job/1-engineer": page}))
    async with http:
        job = await ExampleConnector(http).fetch_job_detail(HOST, _stub("1"))
    assert job is not None
    assert job.expires_at == datetime(2099, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("unit", "amounts", "expected"),
    [
        ("HOUR", (79229.54, 113390.04), ("79229.54", "year")),  # ApplicantPro
        ("YEAR", (10226400, 12463200), (None, None)),  # cents (JobMount): dropped
        ("HOUR", (25, 30), ("25", "hour")),
    ],
)
async def test_implausible_salaries(
    unit: str, amounts: tuple[float, float], expected: tuple[str | None, str | None]
) -> None:
    html = _page("1").replace(
        '"minValue":50000,"maxValue":60000,"unitText":"YEAR"',
        (f'"minValue":{amounts[0]},"maxValue":{amounts[1]},"unitText":"{unit}"'),
    )
    http, _ = mock_http(_routes("", {"/job/1-engineer": httpx.Response(200, text=html)}))
    async with http:
        job = await ExampleConnector(http).fetch_job_detail(HOST, _stub("1"))
    assert job is not None
    salary_min = str(job.salary_min) if job.salary_min is not None else None
    assert (salary_min, job.salary_period) == expected


async def test_redirect_away_from_the_job_counts_as_closed() -> None:
    routes = _routes(
        "",
        {
            "/job/1-engineer": httpx.Response(301, headers={"Location": f"https://{HOST}/jobs"}),
            "/jobs": httpx.Response(200, text="<html><title>All jobs</title></html>"),
        },
    )
    http, _ = mock_http(routes)
    async with http:
        connector = ExampleConnector(http)
        job = await connector.fetch_job_detail(HOST, _stub("1"))
    assert job is not None
    assert job.expires_at is not None
    assert (job.title, connector._unexplained) == ("All jobs", 0)


async def test_many_pages_without_jobposting_fail_the_crawl() -> None:
    blank = httpx.Response(200, text="<html><body>Hello</body></html>")
    pages = {f"/job/{i}-engineer": blank for i in range(10)}
    http, _ = mock_http(_routes("", pages))
    async with http:
        connector = ExampleConnector(http)
        for i in range(generic_jsonld.MAX_UNEXPLAINED):  # a few are tolerated
            await connector.fetch_job_detail(HOST, _stub(str(i)))
        with pytest.raises(ConnectorError, match="no JobPosting"):
            await connector.fetch_job_detail(HOST, _stub("9"))


async def test_other_http_errors_propagate() -> None:
    http, _ = mock_http(_routes("", {"/job/1-engineer": httpx.Response(503)}))
    async with http:
        with pytest.raises(FetchError):
            await ExampleConnector(http).fetch_job_detail(HOST, _stub("1"))


# -- checkpoint ------------------------------------------------------------------


async def test_fetched_pages_are_checkpointed_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generic_jsonld, "CHECKPOINT_EVERY", 2)
    sitemap = _sitemap(*((f"/job/{i}-engineer", "2026-11-01") for i in range(3)))
    pages = {f"/job/{i}-engineer": httpx.Response(200, text=_page(str(i))) for i in range(3)}
    checkpoint = MemoryCheckpoint()
    http, seen = mock_http(_routes(sitemap, pages))
    async with http:
        first = ExampleConnector(http, checkpoint)
        stubs = await first.fetch_jobs(HOST)
        for stub in stubs[:2]:  # the crawl "fails" after two pages
            await first.fetch_job_detail(HOST, stub)
        assert checkpoint.saves == 1
        assert set((checkpoint.data or {})["pages"]) == {"0", "1"}

        seen.clear()
        second = ExampleConnector(http, checkpoint)
        stubs = await second.fetch_jobs(HOST)
        details = [await second.fetch_job_detail(HOST, stub) for stub in stubs]
    page_requests = [r.url.path for r in seen if r.url.path.startswith("/job/")]
    assert page_requests == ["/job/2-engineer"]  # 0 and 1 came from the checkpoint
    assert [d.title for d in details if d] == ["Engineer 0", "Engineer 1", "Engineer 2"]


async def test_checkpoint_is_ignored_when_version_changed_or_stale() -> None:
    page = httpx.Response(200, text=_page("0"))
    saved = {
        "version": generic_jsonld.CHECKPOINT_VERSION,
        "board": HOST,
        "saved_at": datetime.now(UTC).isoformat(),
        "pages": {"0": _dump_job(RawJob("0", "Old title", list_version="2026-10-01"))},
    }
    http, _seen = mock_http(
        _routes(_sitemap(("/job/0-engineer", "2026-11-01")), {"/job/0-engineer": page})
    )
    async with http:
        connector = ExampleConnector(http, MemoryCheckpoint(saved))
        (stub,) = await connector.fetch_jobs(HOST)
        job = await connector.fetch_job_detail(HOST, stub)  # lastmod changed: re-fetch
        stale = {**saved, "saved_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}
        connector = ExampleConnector(http, MemoryCheckpoint(stale))
        await connector.fetch_jobs(HOST)
    assert job is not None
    assert job.title == "Engineer 0"
    assert connector._resumed == {}


def test_dump_load_roundtrip() -> None:
    job = RawJob(
        "1",
        "T",
        salary_min=Decimal("1.5"),
        remote_type=RemoteType.HYBRID,
        posted_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 2, 1, tzinfo=UTC),
        list_version="v",
    )
    assert _load_job(_dump_job(job)) == job


# -- crawler integration ---------------------------------------------------------

Factory = async_sessionmaker[AsyncSession]


class _Board:
    """A mutable fake board: sitemap entries + pages, served by mock_http."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}  # job id -> lastmod
        self.pages: dict[str, httpx.Response] = {}

    def add(self, job_id: str, lastmod: str, response: httpx.Response | None = None) -> None:
        self.entries[job_id] = lastmod
        self.pages[job_id] = response or httpx.Response(200, text=_page(job_id))

    def handler(self) -> dict[str, Any]:
        sitemap = _sitemap(*((f"/job/{i}-engineer", m) for i, m in self.entries.items()))
        pages = {f"/job/{i}-engineer": r for i, r in self.pages.items()}
        return _routes(sitemap, pages)


async def _crawl_board(factory: Factory, source_id: int, board: _Board) -> tuple[Any, list[str]]:
    registry = Registry()
    registry.add(ExampleConnector)
    http, seen = mock_http(board.handler())
    async with http:
        result = await crawl_source(
            source_id, session_factory=factory, http=http, registry=registry
        )
    return result, [
        r.url.path.split("-")[0].removeprefix("/job/") for r in seen if "/job/" in r.url.path
    ]


async def _jobs(session: AsyncSession) -> dict[str, Job]:
    session.expire_all()
    return {j.external_id: j for j in (await session.scalars(select(Job))).all()}


async def _source(session: AsyncSession) -> int:
    source = Source(platform="example-jsonld", board_identifier=HOST, base_url=f"https://{HOST}/")
    session.add(source)
    await session.commit()
    return source.id


async def test_crawl_fetches_only_new_or_changed_pages_and_stores_expired_closed(
    session: AsyncSession, session_factory: Factory
) -> None:
    source_id = await _source(session)
    board = _Board()
    board.add("1", "2026-11-01")
    board.add("2", "2026-11-01")
    board.add("3", "2026-11-01", httpx.Response(200, text=_page("3", valid_through="2020-01-01")))

    result, fetched = await _crawl_board(session_factory, source_id, board)
    assert result.status is FetchStatus.SUCCESS
    assert (result.jobs_found, result.jobs_new, result.jobs_closed) == (2, 2, 0)
    assert sorted(fetched) == ["1", "2", "3"]
    jobs = await _jobs(session)
    assert jobs["3"].status is JobStatus.CLOSED  # expired: stored, never open
    assert jobs["1"].company == "Acme"
    assert jobs["1"].list_version == "2026-11-01"

    # Second crawl: job 2 changed (new lastmod), 1 and 3 unchanged -> only 2 is fetched.
    board.add("2", "2026-11-05", httpx.Response(200, text=_page("2").replace("Build", "Make")))
    result, fetched = await _crawl_board(session_factory, source_id, board)
    assert fetched == ["2"]
    assert (result.jobs_found, result.jobs_updated, result.jobs_closed) == (2, 1, 0)
    jobs = await _jobs(session)
    assert jobs["2"].description_text == "Make things"
    assert jobs["3"].status is JobStatus.CLOSED

    # Third crawl: job 1 now says closed (new lastmod) -> closed, content kept.
    closed_page = _page("1", body="<p>This job has expired.</p>")
    board.add("1", "2026-11-06", httpx.Response(200, text=closed_page))
    result, fetched = await _crawl_board(session_factory, source_id, board)
    assert fetched == ["1"]
    assert (result.jobs_found, result.jobs_closed) == (1, 1)
    jobs = await _jobs(session)
    assert jobs["1"].status is JobStatus.CLOSED
    assert jobs["1"].closed_at is not None


async def test_crawl_refuses_a_large_drop_and_keeps_jobs(
    session: AsyncSession, session_factory: Factory
) -> None:
    source_id = await _source(session)
    board = _Board()
    for i in range(10):
        board.add(str(i), "2026-11-01")
    result, _ = await _crawl_board(session_factory, source_id, board)
    assert result.jobs_found == 10

    for i in range(6):  # the sitemap suddenly lists 4 of 10 jobs
        del board.entries[str(i)]
    result, fetched = await _crawl_board(session_factory, source_id, board)
    assert result.status is FetchStatus.FAILED
    assert result.error is not None
    assert "JobCountDropError" in result.error
    assert fetched == []  # refused before fetching any page
    jobs = await _jobs(session)
    assert all(job.status is JobStatus.OPEN for job in jobs.values())
    source = await session.get(Source, source_id)
    assert source is not None
    assert source.consecutive_failures == 1

    del board.entries["6"]
    board.entries.update({str(i): "2026-11-01" for i in range(4)})  # 7 of 10: a real 30% drop
    result, _ = await _crawl_board(session_factory, source_id, board)
    assert (result.status, result.jobs_found, result.jobs_closed) == (FetchStatus.SUCCESS, 7, 3)


async def test_crawl_refuses_when_pages_turn_out_closed_en_masse(
    session: AsyncSession, session_factory: Factory
) -> None:
    source_id = await _source(session)
    board = _Board()
    for i in range(4):
        board.add(str(i), "2026-11-01")
    await _crawl_board(session_factory, source_id, board)
    expired = httpx.Response(200, text=_page("x", valid_through="2020-01-01"))
    for i in range(3):
        board.add(str(i), "2026-11-02", expired)
    result, _ = await _crawl_board(session_factory, source_id, board)
    assert result.status is FetchStatus.FAILED  # 1 of 4 open: more than 50% fewer
    assert all(j.status is JobStatus.OPEN for j in (await _jobs(session)).values())
