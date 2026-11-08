"""GitHub Actions crawl support: shard planning, crawl deadline, run summary
(.github/workflows/crawl.yml)."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.cli import main
from jobindex.connectors.base import RawJob
from jobindex.connectors.registry import Registry
from jobindex.crawler import DEADLINE_ERROR, crawl_sources
from jobindex.crawlplan import (
    DEFAULT_ESTIMATE_SECONDS,
    crawl_summary,
    plan_shards,
    render_summary,
    source_estimates,
)
from jobindex.http import HttpClient
from jobindex.models import FetchRun, FetchStatus, Source
from jobindex.models.base import utcnow
from tests.fakes import FakeConnector

Factory = async_sessionmaker[AsyncSession]


def test_platforms_stay_together_and_load_is_balanced() -> None:
    estimates = [
        (1, "workday", 3000.0),
        (2, "workday", 1000.0),
        (3, "jobiqo", 3500.0),
        (4, "greenhouse", 10.0),
        (5, "greenhouse", 10.0),
        (6, "lever", 500.0),
    ]
    shards = plan_shards(estimates, 2)
    # workday (4000 s) -> 1, jobiqo (3500) -> 2, lever (500) -> 2, greenhouse (20) -> 1
    assert [s.platforms for s in shards] == [["greenhouse", "workday"], ["jobiqo", "lever"]]
    assert shards[0].source_ids == [1, 2, 4, 5]  # longest first
    assert shards[1].source_ids == [3, 6]
    assert [s.estimated_seconds for s in shards] == [4020.0, 4000.0]

    assert [s.source_ids for s in plan_shards(estimates[:1], 3)] == [[1], [], []]
    with pytest.raises(ValueError, match="shards"):
        plan_shards(estimates, 0)


async def _sources(session: AsyncSession, *platforms: str) -> list[int]:
    sources = [
        Source(platform=p, board_identifier=f"b{i}", base_url="https://x", company_name="X")
        for i, p in enumerate(platforms)
    ]
    session.add_all(sources)
    await session.commit()
    return [s.id for s in sources]


async def test_estimates_use_the_latest_finished_run(session: AsyncSession) -> None:
    a, b, c = await _sources(session, "fake", "fake", "other")
    now = utcnow()

    def run(sid: int, start_min: int, minutes: float | None) -> FetchRun:
        started = now - timedelta(minutes=start_min)
        finished = started + timedelta(minutes=minutes) if minutes is not None else None
        status = FetchStatus.RUNNING if minutes is None else FetchStatus.SUCCESS
        return FetchRun(source_id=sid, started_at=started, finished_at=finished, status=status)

    session.add_all([run(a, 600, 30), run(a, 300, 10), run(b, 5, None)])
    session.add(Source(platform="off", board_identifier="x", base_url="x", active=False))
    await session.commit()
    estimates = {sid: seconds for sid, _, seconds in await source_estimates(session)}
    assert estimates == {a: 600.0, b: DEFAULT_ESTIMATE_SECONDS, c: DEFAULT_ESTIMATE_SECONDS}


async def test_deadline_cancels_running_and_skips_unstarted(
    session: AsyncSession, session_factory: Factory
) -> None:
    ids = await _sources(session, "slow", "slow", "slow")

    class SlowConnector(FakeConnector):
        platform = "slow"

        async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
            await asyncio.sleep(30)
            return []

    registry = Registry()
    registry.add(SlowConnector)
    async with HttpClient(user_agent="test") as http:
        results = await crawl_sources(
            ids,
            session_factory=session_factory,
            http=http,
            concurrency=2,
            registry=registry,
            deadline_seconds=0.3,
        )
    assert [r.source_id for r in results] == ids
    assert all(r.status is FetchStatus.FAILED and r.error == DEADLINE_ERROR for r in results)
    session.expire_all()
    runs = (await session.scalars(select(FetchRun).order_by(FetchRun.id))).all()
    # The two started crawls are recorded as failed (cancelled); the third never started.
    assert [(r.source_id, r.status) for r in runs] == [
        (ids[0], FetchStatus.FAILED),
        (ids[1], FetchStatus.FAILED),
    ]
    assert all("cancelled" in (r.error or "") for r in runs)


@pytest.mark.usefixtures("settings_env")
async def test_plan_crawl_and_summary_cli(
    session: AsyncSession,
    fake_registry: Registry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ids = await _sources(session, "fake", "fake")
    stale = FetchRun(
        source_id=ids[0], started_at=utcnow() - timedelta(hours=7), status=FetchStatus.RUNNING
    )
    session.add(stale)
    await session.commit()
    stale_id = stale.id
    since = utcnow().isoformat()

    code = await asyncio.to_thread(
        main, ["crawl-plan", "--shards", "3", "--recover-after-hours", "5.5"], fake_registry
    )
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    include = plan["matrix"]["include"]
    assert len(include) == 1  # one platform -> one non-empty shard
    assert include[0]["ids"] == f"{ids[0]},{ids[1]}"
    session.expire_all()
    recovered = await session.get(FetchRun, stale_id)
    assert recovered is not None
    assert recovered.status is FetchStatus.FAILED

    out_file = tmp_path / "github_output"
    assert (
        await asyncio.to_thread(
            main, ["crawl-plan", "--shards", "2", "--github-output", str(out_file)], fake_registry
        )
        == 0
    )
    assert out_file.read_text().splitlines()[0].startswith('matrix={"include":[{"shard":1,')
    assert "shard 1: 2 sources" in capsys.readouterr().out

    FakeConnector.result = [RawJob(external_id="1", title="Engineer")]
    args = ["crawl", "--ids", include[0]["ids"], "--no-dedup", "--deadline-minutes", "5"]
    assert await asyncio.to_thread(main, args, fake_registry) == 0
    out = capsys.readouterr().out
    assert "crawl: 2 succeeded, 0 failed" in out
    assert "dedup:" not in out

    FakeConnector.result = RuntimeError("boom")
    assert await asyncio.to_thread(main, args, fake_registry) == 1
    assert await asyncio.to_thread(main, [*args, "--allow-failures"], fake_registry) == 0
    capsys.readouterr()

    assert await asyncio.to_thread(main, ["crawl", "--ids", "999"], fake_registry) == 2
    assert await asyncio.to_thread(main, ["crawl", "--ids", ""], fake_registry) == 0

    assert await asyncio.to_thread(main, ["crawl-summary", "--since", since]) == 0
    summary = capsys.readouterr().out
    assert "**2 succeeded, 4 failed**" in summary
    assert "| fake | 2 | 4 | 2 | 2 | 0 | 0 |" in summary
    assert "`fake` `b0`: RuntimeError: boom" in summary


async def test_empty_summary(session: AsyncSession) -> None:
    summary = await crawl_summary(session, utcnow())
    text = render_summary(summary)
    assert "**0 succeeded, 0 failed** · 0 active sources · **0 open postings**" in text
    assert "Failures" not in text
