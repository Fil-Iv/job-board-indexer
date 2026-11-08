import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.cli import add_source, main, seed_demo
from jobindex.connectors.base import RawJob
from jobindex.connectors.registry import Registry
from jobindex.models import FetchRun, FetchStatus, Job, Source
from tests.fakes import FakeConnector, mock_http


@pytest.mark.usefixtures("settings_env")
async def test_add_source_and_crawl(
    fake_registry: Registry, session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    # main() calls asyncio.run, so run it in a thread, outside the test's event loop.
    assert (
        await asyncio.to_thread(main, ["add-source", "https://jobs.fake.test/acme"], fake_registry)
        == 0
    )
    assert "added source id=1 (fake/acme) company='Acme'" in capsys.readouterr().out

    # Adding the same board twice is idempotent.
    assert (
        await asyncio.to_thread(main, ["add-source", "https://jobs.fake.test/acme"], fake_registry)
        == 0
    )
    assert "already exists: id=1" in capsys.readouterr().out

    source = (await session.scalars(select(Source))).one()
    assert (source.platform, source.board_identifier, source.company_name) == (
        "fake",
        "acme",
        "Acme",
    )

    FakeConnector.result = [RawJob(external_id="x1", title="Data Engineer")]
    assert await asyncio.to_thread(main, ["crawl", "1"], fake_registry) == 0
    assert "found=1 new=1" in capsys.readouterr().out
    assert (await session.scalars(select(Job.title))).all() == ["Data Engineer"]

    FakeConnector.result = RuntimeError("upstream down")
    assert await asyncio.to_thread(main, ["crawl", "--all"], fake_registry) == 1
    assert "FAILED" in capsys.readouterr().out
    statuses = (await session.scalars(select(FetchRun.status).order_by(FetchRun.id))).all()
    assert statuses == [FetchStatus.SUCCESS, FetchStatus.FAILED]


async def test_add_source_rejects_page_without_platform(
    session_factory: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    page = httpx.Response(200, text="<html><h1>Careers</h1><p>Email us!</p></html>")
    http, _ = mock_http({"acme.test/careers": page})
    async with http:
        code = await add_source(
            session_factory, Registry(), http, "https://acme.test/careers", None
        )
    assert code == 2
    assert "no supported platform found" in capsys.readouterr().err


@pytest.mark.usefixtures("settings_env")
def test_crawl_missing_source(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["crawl", "999"], Registry()) == 2
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.usefixtures("settings_env")
async def test_set_company(
    fake_registry: Registry, session: AsyncSession, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await asyncio.to_thread(main, ["add-source", "https://jobs.fake.test/acme"], fake_registry)
        == 0
    )
    assert await asyncio.to_thread(main, ["set-company", "1", "Acme GmbH"], fake_registry) == 0
    assert "'Acme' -> 'Acme GmbH'" in capsys.readouterr().out
    source = (await session.scalars(select(Source))).one()
    assert source.company_name == "Acme GmbH"
    assert await asyncio.to_thread(main, ["set-company", "99", "X"], fake_registry) == 2
    assert await asyncio.to_thread(main, ["set-company", "1", "  "], fake_registry) == 2


async def test_seed_demo_registers_examples(
    fake_registry: Registry,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = tmp_path / "platforms.yaml"
    plan.write_text(
        """
- name: Fake
  key: fake
  type: ats
  status: done
  connector: tests.fakes
  public_api: "yes"
  api: "GET /"
  examples: ["https://jobs.fake.test/acme", "https://jobs.fake.test/globex"]
  example_companies: {"https://jobs.fake.test/globex": "Globex Corporation"}
  robots: allowed
  verified: 2026-11-08
  notes: ""
- name: Todo
  key: todo
  type: ats
  status: todo
  tier: hard
  public_api: "no"
  api: "?"
  examples: ["https://todo.test/x"]
  robots: unknown
  verified: null
  notes: ""
""",
        encoding="utf-8",
    )
    http, _ = mock_http({})
    async with http:
        assert await seed_demo(session_factory, fake_registry, http, platforms_file=plan) == 0
        # idempotent; todo platforms are skipped
        assert await seed_demo(session_factory, fake_registry, http, platforms_file=plan) == 0
    rows = (await session.execute(select(Source.board_identifier, Source.company_name))).all()
    names = {board: company for board, company in rows}
    assert names == {"acme": "Acme", "globex": "Globex Corporation"}
