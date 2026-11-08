import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.cli import main
from jobindex.connectors.registry import Registry
from jobindex.models import Job, JobStatus, RemoteType, Source
from jobindex.quality import fill_rates, render_markdown


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Source(id=1, platform="lever", board_identifier="a", base_url="https://a"),
            Source(id=2, platform="niceboard", board_identifier="b", base_url="https://b"),
        ]
    )
    await session.flush()
    full = {
        "company": "Acme",
        "description_text": "Build things",
        "location_raw": "Berlin",
        "source_url": "https://a/1",
        "apply_url": "https://a/1/apply",
        "content_hash": "x",
    }
    session.add_all(
        [
            Job(source_id=1, external_id="1", title="Engineer", salary_max=1, **full),
            Job(source_id=1, external_id="2", title="Designer", **{**full, "company": None}),
            # remote without a place still has a location
            Job(
                source_id=2,
                external_id="3",
                title="Writer",
                remote_type=RemoteType.REMOTE,
                **{**full, "location_raw": None},
            ),
            # closed postings are not counted
            Job(
                source_id=2, external_id="4", title="Old", status=JobStatus.CLOSED, content_hash="y"
            ),
        ]
    )
    await session.commit()


async def test_fill_rates_per_platform(session: AsyncSession) -> None:
    await _seed(session)
    rows = {r.platform: r for r in await fill_rates(session)}
    assert rows["lever"].open_jobs == 2
    assert rows["lever"].rate("company") == 0.5
    assert rows["lever"].rate("salary") == 0.5
    assert rows["lever"].gaps() == ["company"]
    assert rows["niceboard"].open_jobs == 1
    assert rows["niceboard"].rate("location") == 1.0
    assert rows["niceboard"].rate("remote") == 1.0
    assert rows["niceboard"].gaps() == []
    table = render_markdown(list(rows.values()))
    assert "| lever | 2 | 100% | **50%** |" in table
    assert "| **all** | 3 |" in table


@pytest.mark.usefixtures("settings_env")
async def test_fill_rates_cli(session: AsyncSession, capsys: pytest.CaptureFixture[str]) -> None:
    await _seed(session)
    assert await asyncio.to_thread(main, ["fill-rates"], Registry()) == 0
    assert "required-field gaps: lever: company" in capsys.readouterr().out
    assert await asyncio.to_thread(main, ["fill-rates", "--strict"], Registry()) == 1
