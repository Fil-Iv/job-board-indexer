"""Adding career pages (jobindex/intake.py): URL checks, the review queue, saving."""

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.config import get_settings
from jobindex.connectors.base import RawJob
from jobindex.connectors.registry import Registry, default_registry
from jobindex.intake import (
    Check,
    IntakeError,
    QueueFull,
    check_url,
    clean_url,
    process_pending,
    queue,
    save_source,
    token_ok,
)
from jobindex.models import Job, PendingSource, Source
from tests.fakes import FakeConnector, mock_http

BOARD = "https://jobs.fake.test/acme"


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://jobs.lever.co/acme", "https://jobs.lever.co/acme"),
        ("  acme.com/careers ", "https://acme.com/careers"),
        ("http://acme.com/jobs?x=1", "http://acme.com/jobs?x=1"),
    ],
)
def test_clean_url(raw: str, expected: str) -> None:
    assert clean_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "javascript:alert(1)",
        "ftp://acme.com/x",
        "https://localhost/x",
        "https://user:pw@acme.com/",
        "https://acme.com/" + "x" * 3000,
    ],
)
def test_clean_url_rejects(raw: str) -> None:
    with pytest.raises(IntakeError):
        clean_url(raw)


def test_token(settings_env: pytest.MonkeyPatch) -> None:
    assert not token_ok("anything")  # no ADMIN_TOKEN set: nobody can save
    settings_env.setenv("ADMIN_TOKEN", "s3cret-token")
    get_settings.cache_clear()
    assert token_ok("s3cret-token")
    assert not token_ok("s3cret-tokeN")
    assert not token_ok("")
    assert not token_ok(None)


async def test_url_pattern_only(session: AsyncSession) -> None:
    check = await check_url(session, default_registry(), "https://jobs.lever.co/acme", None)
    assert (check.platform, check.board_identifier, check.detected_by) == ("lever", "acme", "url")
    assert check.platform_name == "Lever.co"
    assert not check.live
    assert check.robots_allowed is None
    assert check.preview_jobs is None
    nothing = await check_url(session, default_registry(), "https://acme.com/careers", None)
    assert not nothing.found


async def test_live_check_dry_run(session: AsyncSession, fake_registry: Registry) -> None:
    FakeConnector.result = [RawJob(external_id=str(i), title="Engineer") for i in (1, 2, 2)]
    http, _ = mock_http({"jobs.fake.test/acme": httpx.Response(200, text="<html></html>")})
    async with http:
        check = await check_url(session, fake_registry, BOARD, http)
    assert check.live
    assert check.robots_allowed is True
    assert check.preview_jobs == 2  # distinct postings
    assert check.error is None


async def test_live_check_respects_robots(session: AsyncSession, fake_registry: Registry) -> None:
    FakeConnector.result = [RawJob(external_id="1", title="Engineer")]
    http, seen = mock_http({}, robots="User-agent: *\nDisallow: /")
    async with http:
        check = await check_url(session, fake_registry, BOARD, http)
    assert check.robots_allowed is False
    assert check.robots_rule == "Disallow: /"
    assert check.preview_jobs is None  # no dry run against a disallowed site
    assert [r.url.path for r in seen] == ["/robots.txt"]


async def test_dry_run_failure_is_reported(session: AsyncSession, fake_registry: Registry) -> None:
    FakeConnector.result = RuntimeError("page 3 of 5 failed")
    http, _ = mock_http({"jobs.fake.test/acme": httpx.Response(200, text="ok")})
    async with http:
        check = await check_url(session, fake_registry, BOARD, http)
    assert check.preview_jobs is None
    assert check.error == "Dry run failed: RuntimeError: page 3 of 5 failed"


async def test_existing_source(session: AsyncSession) -> None:
    session.add(Source(id=7, platform="lever", board_identifier="acme", base_url="https://x"))
    await session.flush()
    session.add(Job(source_id=7, external_id="1", title="T", content_hash="h"))
    await session.commit()
    check = await check_url(session, default_registry(), "https://jobs.lever.co/acme", None)
    assert (check.existing_source_id, check.existing_open_jobs) == (7, 1)


async def test_queue_dedupes_and_caps(
    session: AsyncSession, settings_env: pytest.MonkeyPatch
) -> None:
    row, created = await queue(session, Check(url="https://a.example/jobs", platform="lever"))
    assert created
    assert (row.status, row.platform) == ("pending", "lever")
    again, created = await queue(session, Check(url="https://a.example/jobs"))
    assert (again.id, created) == (row.id, False)
    settings_env.setenv("INTAKE_QUEUE_PER_HOUR", "2")
    get_settings.cache_clear()
    await queue(session, Check(url="https://b.example/jobs"))
    with pytest.raises(QueueFull):
        await queue(session, Check(url="https://c.example/jobs"))
    assert await session.scalar(select(func.count()).select_from(PendingSource)) == 2


async def test_save_source(session: AsyncSession) -> None:
    url = "https://jobs.lever.co/acme"
    await queue(session, Check(url=url, platform="lever", board_identifier="acme"))
    check = await check_url(session, default_registry(), url, None)
    source, created = await save_source(session, default_registry(), check)
    assert created
    assert (source.platform, source.board_identifier) == ("lever", "acme")
    assert source.base_url == "https://jobs.lever.co/acme"
    pending = await session.scalar(select(PendingSource))
    assert pending is not None
    assert (pending.status, pending.source_id) == ("added", source.id)
    same, created = await save_source(session, default_registry(), check)
    assert (same.id, created) == (source.id, False)
    with pytest.raises(IntakeError, match=r"robots\.txt"):
        await save_source(
            session,
            default_registry(),
            Check(url=url, platform="lever", board_identifier="b", robots_allowed=False),
        )
    with pytest.raises(IntakeError, match="No supported platform"):
        await save_source(session, default_registry(), Check(url="https://acme.com/careers"))


async def test_process_pending(
    session_factory: async_sessionmaker[AsyncSession], fake_registry: Registry
) -> None:
    FakeConnector.result = [RawJob(external_id="1", title="Engineer")]
    async with session_factory() as session:
        await queue(session, Check(url=BOARD))
        await queue(session, Check(url="https://unknown.example/careers"))
    http, _ = mock_http(
        {
            "jobs.fake.test/acme": httpx.Response(200, text="<html></html>"),
            "unknown.example/careers": httpx.Response(200, text="<html>no platform</html>"),
        }
    )
    async with http:
        assert await process_pending(session_factory, fake_registry, http) == 2
    async with session_factory() as session:
        rows = {r.url: r for r in await session.scalars(select(PendingSource))}
    fake = rows[BOARD]
    assert (fake.status, fake.platform, fake.board_identifier) == ("checked", "fake", "acme")
    assert (fake.robots_allowed, fake.preview_jobs, fake.error) == (True, 1, None)
    assert fake.checked_at is not None
    unknown = rows["https://unknown.example/careers"]
    assert (unknown.status, unknown.platform, unknown.error) == (
        "checked",
        None,
        "no supported platform found",
    )
