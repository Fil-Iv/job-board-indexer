"""Size budget for the hosted demo: per-source cap, HTML trimming, compact job_changes,
``prune``, ``db-size`` and ``seed-demo --file`` (docs/OPERATIONS.md#size-budget)."""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.cli import load_demo_sources, main
from jobindex.config import get_settings
from jobindex.connectors.base import RawJob
from jobindex.connectors.registry import Registry
from jobindex.coverage import ROOT
from jobindex.crawler import CrawlResult, crawl_source, truncate_text
from jobindex.http import HttpClient
from jobindex.maintenance import CompactResult, compact_descriptions, prune
from jobindex.models import FetchRun, FetchStatus, Job, JobChange, JobStatus, Source
from jobindex.models.base import utcnow
from tests.fakes import FakeConnector, FakeDetailConnector

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
def budget(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Set size-budget env vars inside a test; settings are re-read afterwards."""
    get_settings.cache_clear()
    yield monkeypatch
    monkeypatch.undo()
    get_settings.cache_clear()


def _set(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


async def _source(session: AsyncSession, platform: str = "fake") -> int:
    source = Source(
        platform=platform, board_identifier="acme", company_name="Acme", base_url="https://x"
    )
    session.add(source)
    await session.commit()
    return source.id


async def _crawl(factory: Factory, source_id: int, registry: Registry) -> CrawlResult:
    async with HttpClient(user_agent="test") as http:
        return await crawl_source(source_id, session_factory=factory, http=http, registry=registry)


async def _open_ids(session: AsyncSession) -> set[str]:
    session.expire_all()
    rows = await session.scalars(select(Job.external_id).where(Job.status == JobStatus.OPEN))
    return set(rows.all())


async def test_cap_keeps_stored_postings_and_never_closes_listed_ones(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
) -> None:
    _set(budget, MAX_JOBS_PER_SOURCE="2")
    source_id = await _source(session)
    FakeConnector.result = [RawJob(external_id=i, title="Engineer") for i in "abcd"]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert result.status is FetchStatus.SUCCESS
    assert result.jobs_found == 2
    first = await _open_ids(session)
    assert len(first) == 2

    # New postings appear: the stored ones stay (no churn), nothing is closed.
    FakeConnector.result = [RawJob(external_id=i, title="Engineer") for i in "abcdefg"]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert (result.jobs_found, result.jobs_new, result.jobs_closed) == (2, 0, 0)
    assert await _open_ids(session) == first

    # A stored posting really disappears from the full list: closed, a new one fills in.
    gone = sorted(first)[0]
    FakeConnector.result = [RawJob(external_id=i, title="Engineer") for i in "abcdefg" if i != gone]
    result = await _crawl(session_factory, source_id, fake_registry)
    assert (result.jobs_found, result.jobs_new, result.jobs_closed) == (2, 1, 1)
    assert gone not in await _open_ids(session)


async def test_cap_limits_detail_requests(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
) -> None:
    _set(budget, MAX_JOBS_PER_SOURCE="3")
    source_id = await _source(session, platform="fake-detail")
    FakeDetailConnector.result = [RawJob(external_id=str(i), title="Engineer") for i in range(10)]
    FakeDetailConnector.details = {str(i): "<p>Text</p>" for i in range(10)}
    await _crawl(session_factory, source_id, fake_registry)
    assert len(FakeDetailConnector.detail_calls) == 3


async def test_long_html_trimmed_text_kept_and_detail_still_reused(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
) -> None:
    _set(budget, DESCRIPTION_HTML_MAX_CHARS="20")
    source_id = await _source(session, platform="fake-detail")
    FakeDetailConnector.result = [
        RawJob(external_id="1", title="A"),
        RawJob(external_id="2", title="B"),
    ]
    FakeDetailConnector.details = {"1": "<p>Short</p>", "2": "<p>" + "Long text. " * 10 + "</p>"}
    await _crawl(session_factory, source_id, fake_registry)
    session.expire_all()
    jobs = {j.external_id: j for j in (await session.scalars(select(Job))).all()}
    assert jobs["1"].description_html == "<p>Short</p>"
    assert jobs["2"].description_html == ""
    assert jobs["2"].description_text is not None
    assert jobs["2"].description_text.startswith("Long text.")

    FakeDetailConnector.detail_calls = []
    await _crawl(session_factory, source_id, fake_registry)
    assert FakeDetailConnector.detail_calls == []  # trimmed HTML still counts as fetched


async def test_job_change_values_are_cut(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
) -> None:
    _set(budget, JOB_CHANGE_VALUE_MAX_CHARS="10")
    source_id = await _source(session)
    FakeConnector.result = [RawJob(external_id="1", title="x" * 50, description_text="old")]
    await _crawl(session_factory, source_id, fake_registry)
    FakeConnector.result = [RawJob(external_id="1", title="y" * 50, description_text="new")]
    await _crawl(session_factory, source_id, fake_registry)
    change = await session.scalar(select(JobChange).where(JobChange.field == "title"))
    assert change is not None
    assert change.old_value == "x" * 10 + "…"
    assert change.new_value == "y" * 10 + "…"
    # description changes are recorded without their (large) values
    text = await session.scalar(select(JobChange).where(JobChange.field == "description_text"))
    assert text is not None
    assert (text.old_value, text.new_value) == (None, None)


LONG_TEXT = ("Build secure systems. " * 120).strip()  # ~2,600 characters


def test_truncate_text() -> None:
    cut = truncate_text(LONG_TEXT, 1500)
    assert cut is not None
    assert cut.endswith("…")
    assert len(cut) <= 1501
    assert not cut[:-1].endswith(" ")  # cut at a word boundary
    assert LONG_TEXT.startswith(cut[:-1])
    assert truncate_text("short", 1500) == "short"
    assert truncate_text(None, 1500) is None
    assert truncate_text(LONG_TEXT, None) == LONG_TEXT


async def test_text_budget_on_crawl(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
) -> None:
    _set(budget, DESCRIPTION_TEXT_MAX_CHARS="1500", DESCRIPTION_HTML_MAX_CHARS="0")
    source_id = await _source(session)
    html = "<p>" + LONG_TEXT + "</p>"
    FakeConnector.result = [RawJob(external_id="1", title="Engineer", description_html=html)]
    await _crawl(session_factory, source_id, fake_registry)
    job = await session.scalar(select(Job))
    assert job is not None
    assert job.description_html == ""
    assert job.description_text is not None
    assert job.description_text.endswith("…")
    assert len(job.description_text) <= 1501
    # the same posting again: unchanged (the hash covers the stored, trimmed text)
    await _crawl(session_factory, source_id, fake_registry)
    assert await session.scalar(select(func.count()).select_from(JobChange)) == 0


async def test_compact_existing_rows(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
) -> None:
    """Rows stored before the limits are trimmed, with a hash the next crawl agrees with."""
    source_id = await _source(session)
    html = "<p>" + LONG_TEXT + "</p>"
    FakeConnector.result = [RawJob(external_id="1", title="Engineer", description_html=html)]
    await _crawl(session_factory, source_id, fake_registry)  # no limits yet: full text
    session.add(JobChange(job_id=1, field="description_text", old_value="a" * 400, new_value="b"))
    await session.commit()

    async with session_factory() as s:
        result = await compact_descriptions(s, html_limit=0, text_limit=1500)
    assert (result.jobs, result.job_changes) == (1, 1)
    session.expire_all()
    job = await session.scalar(select(Job))
    assert job is not None
    assert job.description_html == ""
    assert job.description_text is not None
    assert job.description_text.endswith("…")
    async with session_factory() as s:  # idempotent
        assert await compact_descriptions(s, html_limit=0, text_limit=1500) == CompactResult(0, 0)

    _set(budget, DESCRIPTION_TEXT_MAX_CHARS="1500", DESCRIPTION_HTML_MAX_CHARS="0")
    await _crawl(session_factory, source_id, fake_registry)
    changes = await session.scalar(select(func.count()).select_from(JobChange))
    assert changes == 1  # only the row added above: the crawl saw no change


async def test_prune(session: AsyncSession) -> None:
    source_id = await _source(session)
    now = utcnow()
    old, recent = now - timedelta(days=40), now - timedelta(days=2)

    def job(external_id: str, status: JobStatus, closed_at: object = None) -> Job:
        return Job(
            source_id=source_id,
            external_id=external_id,
            title="T",
            content_hash="h",
            status=status,
            closed_at=closed_at,
        )

    jobs = [
        job("open", JobStatus.OPEN),
        job("closed-old", JobStatus.CLOSED, old),
        job("closed-recent", JobStatus.CLOSED, recent),
    ]
    session.add_all(jobs)
    await session.flush()
    session.add_all(
        [
            JobChange(job_id=jobs[0].id, field="title", changed_at=old),
            JobChange(job_id=jobs[0].id, field="title", changed_at=recent),
            JobChange(job_id=jobs[1].id, field="status", changed_at=recent),  # cascades
        ]
    )
    session.add_all(
        [
            FetchRun(source_id=source_id, started_at=old, status=FetchStatus.SUCCESS),  # latest ok
            FetchRun(source_id=source_id, started_at=old, status=FetchStatus.FAILED),
            FetchRun(source_id=source_id, started_at=recent, status=FetchStatus.FAILED),
        ]
    )
    await session.commit()

    result = await prune(session, closed_job_days=30, job_change_days=30, fetch_run_days=30)
    assert (result.jobs, result.job_changes, result.fetch_runs) == (1, 1, 1)
    session.expire_all()
    assert set((await session.scalars(select(Job.external_id))).all()) == {"open", "closed-recent"}
    assert await session.scalar(select(func.count()).select_from(JobChange)) == 1
    statuses = (await session.scalars(select(FetchRun.status).order_by(FetchRun.id))).all()
    assert statuses == [FetchStatus.SUCCESS, FetchStatus.FAILED]

    # None skips a step.
    assert (
        await prune(session, closed_job_days=None, job_change_days=None, fetch_run_days=None)
        == type(result)()
    )


@pytest.mark.usefixtures("settings_env")
async def test_db_size_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert await asyncio.to_thread(main, ["db-size"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("database: ")
    assert "jobs" in out
    assert await asyncio.to_thread(main, ["db-size", "--markdown", "--fail-above-mb", "0.001"]) == 1
    captured = capsys.readouterr()
    assert "| Table | Size (MB) | Rows (est.) |" in captured.out
    assert "above the 0.001 MB limit" in captured.err


@pytest.mark.usefixtures("settings_env")
async def test_seed_demo_from_file(
    tmp_path: Path,
    fake_registry: Registry,
    session: AsyncSession,
    capsys: pytest.CaptureFixture[str],
) -> None:
    demo = tmp_path / "demo.yaml"
    demo.write_text(
        "sources:\n"
        "  - platform: fake\n    url: https://jobs.fake.test/acme\n    company: Acme Corp\n"
        "  - url: https://jobs.fake.test/globex\n"
        "  - url: https://unsupported.example/jobs\n"
    )
    code = await asyncio.to_thread(main, ["seed-demo", "--file", str(demo)], fake_registry)
    assert code == 1  # 1 of 3 failed (> 10%); the others are still registered
    rows = (await session.execute(select(Source.board_identifier, Source.company_name))).all()
    assert sorted(rows) == [("acme", "Acme Corp"), ("globex", "Globex")]
    assert "1 of 3 source(s) failed (more than 10%)" in capsys.readouterr().err

    # 1 of 10 failed (exactly 10%): reported, but the seed step passes
    boards = "".join(f"  - url: https://jobs.fake.test/board{i}\n" for i in range(9))
    demo.write_text("sources:\n" + boards + "  - url: https://unsupported.example/jobs\n")
    code = await asyncio.to_thread(main, ["seed-demo", "--file", str(demo)], fake_registry)
    assert code == 0
    err = capsys.readouterr().err
    assert "1 of 10 source(s) failed" in err
    assert "more than" not in err

    demo.write_text("nope: 1\n")
    assert await asyncio.to_thread(main, ["seed-demo", "--file", str(demo)], fake_registry) == 2


def test_committed_demo_sources_file_is_valid() -> None:
    entries = load_demo_sources(ROOT / "coverage" / "demo_sources.yaml")
    assert len(entries) >= 50
    assert len({url for url, _ in entries}) == len(entries)


async def test_prune_command_compacts_and_vacuums(
    session: AsyncSession,
    session_factory: Factory,
    fake_registry: Registry,
    budget: pytest.MonkeyPatch,
    migrated_db: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_id = await _source(session)
    FakeConnector.result = [RawJob(external_id="1", title="T", description_text=LONG_TEXT)]
    await _crawl(session_factory, source_id, fake_registry)
    _set(budget, DATABASE_URL=migrated_db, DESCRIPTION_TEXT_MAX_CHARS="1500")
    assert await asyncio.to_thread(main, ["prune"], fake_registry) == 0
    assert "trimmed 1 descriptions" in capsys.readouterr().out
    assert await asyncio.to_thread(main, ["prune"], fake_registry) == 0  # nothing left to do
    assert "trimmed 0 descriptions" in capsys.readouterr().out
