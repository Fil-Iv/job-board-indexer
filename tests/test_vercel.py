"""The Vercel deployment (api/index.py, vercel.json, requirements.txt) stays read-only,
light and in sync with pyproject.toml. See docs/DEPLOY.md."""

import json
import subprocess
import sys
import tomllib
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api.app import app
from jobindex.api.deps import session_factory
from jobindex.config import Settings, get_settings
from jobindex.coverage import ROOT

NOT_ON_VERCEL = {"uvicorn", "apscheduler", "alembic"}  # server, scheduler, migrations


def _name(requirement: str) -> str:
    for sep in "[<>=!~ ;":
        requirement = requirement.split(sep)[0]
    return requirement.strip().lower()


def test_requirements_match_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    expected = {
        req.split("#")[0].strip()
        for req in pyproject["project"]["dependencies"]
        if _name(req) not in NOT_ON_VERCEL
    }
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    actual = {line.strip() for line in lines if line.strip() and not line.startswith("#")}
    assert actual == expected


def test_fastapi_preset_serves_the_app_unrewritten() -> None:
    # The FastAPI preset routes every path to the entrypoint itself; a rewrite to
    # /api/index would make the app see that path and answer 404 everywhere.
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["tool"]["vercel"]["entrypoint"] == "api.index:app"
    config = json.loads((ROOT / "vercel.json").read_text())
    assert "rewrites" not in config
    assert "routes" not in config
    assert "api/index.py" in config["functions"]


def test_entry_point_imports_no_crawler_runtime() -> None:
    code = (
        "import sys, api.index; "
        "print(sorted(m for m in ('apscheduler', 'uvicorn') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_api_has_no_write_endpoints() -> None:
    methods = {m for r in app.routes if isinstance(r, APIRoute) for m in r.methods or ()}
    assert methods <= {"GET", "HEAD"}


def test_vercel_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_NULL_POOL", "DATABASE_READ_ONLY", "API_CACHE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    settings = Settings()
    assert settings.database_null_pool
    assert settings.database_read_only
    assert settings.api_cache_seconds == 60

    monkeypatch.setenv("API_CACHE_SECONDS", "0")
    assert Settings().api_cache_seconds == 0  # explicit settings win

    monkeypatch.delenv("VERCEL")
    plain = Settings(_env_file=None)
    assert not plain.database_null_pool
    assert not plain.database_read_only


@pytest.fixture
async def cached_client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("API_CACHE_SECONDS", "60")
    get_settings.cache_clear()
    app.state.session_factory = session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    get_settings.cache_clear()


async def test_cache_control(cached_client: httpx.AsyncClient) -> None:
    jobs = await cached_client.get("/jobs")
    assert jobs.headers["cache-control"].startswith("public, max-age=0, s-maxage=60")
    health = await cached_client.get("/health")
    assert health.status_code == 200
    assert "cache-control" not in health.headers
    missing = await cached_client.get("/jobs/999999")
    assert missing.status_code == 404
    assert "cache-control" not in missing.headers


@pytest.mark.usefixtures("settings_env")
async def test_session_factory_created_without_lifespan() -> None:
    bare = FastAPI()
    factory = session_factory(bare)
    assert session_factory(bare) is factory
    await factory.kw["bind"].dispose()
