"""Test doubles and helpers."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

import httpx

from jobindex.connectors.base import BaseConnector, RawJob
from jobindex.http import HttpClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_URL_PREFIX = "https://jobs.fake.test/"


def load_fixture(platform: str, name: str) -> str:
    """Read a saved real response from ``tests/fixtures/<platform>/<name>``."""
    return (FIXTURES / platform / name).read_text(encoding="utf-8")


def load_json_fixture(platform: str, name: str) -> Any:
    return json.loads(load_fixture(platform, name))


Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


def mock_http(
    routes: Mapping[str, Route], *, robots: str | None = None
) -> tuple[HttpClient, list[httpx.Request]]:
    """An ``HttpClient`` (no rate limit, no backoff) answering from ``routes``.

    Keys are ``"host/path"`` (no scheme, no query). ``robots.txt`` is 404 (= allow all)
    unless ``robots`` is given. Unknown paths return 404. Returns the client and the
    list of requests it received.
    """
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.url.host}{request.url.path}"
        if request.url.path == "/robots.txt" and key not in routes:
            return httpx.Response(200, text=robots) if robots else httpx.Response(404)
        route = routes.get(key)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        return route(request) if callable(route) else route

    client = HttpClient(
        user_agent="JobIndexBot/test",
        transport=httpx.MockTransport(handle),
        backoff_base=0.0,
        min_interval_per_host=0.0,
        max_retries=1,
    )
    return client, seen


class FakeConnector(BaseConnector):
    """Connector whose output is scripted by the test via ``FakeConnector.result``."""

    platform = "fake"
    result: ClassVar[list[RawJob] | Exception] = []

    @classmethod
    def detect(cls, url: str) -> str | None:
        if not url.startswith(FAKE_URL_PREFIX):
            return None
        return url.removeprefix(FAKE_URL_PREFIX).strip("/") or None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return FAKE_URL_PREFIX + board_identifier

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        return board_identifier.title()

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        result = type(self).result
        if isinstance(result, Exception):
            raise result
        return list(result)


class FakeDetailConnector(FakeConnector):
    """Like FakeConnector but descriptions come from ``fetch_job_detail``."""

    platform = "fake-detail"
    needs_detail = True
    details: ClassVar[dict[str, str | None]] = {}  # external_id -> html (None = vanished)
    detail_calls: ClassVar[list[str]] = []

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        type(self).detail_calls.append(job.external_id)
        html = type(self).details.get(job.external_id)
        if html is None:
            return None
        return RawJob(**{**job.as_dict(), "description_html": html})
