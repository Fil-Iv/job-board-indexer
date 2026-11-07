"""Helpers shared by the tests of generic sitemap + JSON-LD connectors."""

from datetime import UTC, datetime

import httpx

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.http import HttpClient
from tests.fakes import load_fixture, mock_http


def fixture_routes(platform: str, files: dict[str, str]) -> dict[str, httpx.Response]:
    """``{"host/path": fixture file}`` -> mock_http routes serving the saved responses."""
    return {
        key: httpx.Response(200, text=load_fixture(platform, name)) for key, name in files.items()
    }


def sitemap_route(
    sitemap_url: str, *page_urls: str, lastmod: str = "2026-11-07"
) -> dict[str, httpx.Response]:
    """A route serving a minimal sitemap that lists ``page_urls``."""
    urls = "".join(f"<url><loc>{u}</loc><lastmod>{lastmod}</lastmod></url>" for u in page_urls)
    body = f'<?xml version="1.0"?><urlset>{urls}</urlset>'
    return {sitemap_url.split("://", 1)[1]: httpx.Response(200, text=body)}


async def crawl_fixture(
    connector_cls: type[GenericJsonLdConnector], board: str, routes: dict[str, httpx.Response]
) -> tuple[list[RawJob], dict[str, RawJob]]:
    """List the board, then fetch the detail of every listed job that has a route
    (keyed by external id)."""
    http, _ = mock_http(routes)
    async with http:
        connector = connector_cls(http)
        stubs = await connector.fetch_jobs(board)
        details = {}
        for stub in stubs:
            path = (stub.source_url or "").split("://", 1)[1].split("?")[0]
            if path in routes:
                detail = await connector.fetch_job_detail(board, stub)
                assert detail is not None
                details[stub.external_id] = detail
    return stubs, details


async def live_check(
    connector_cls: type[GenericJsonLdConnector], board: str, sample: int = 3
) -> list[RawJob]:
    """Live: the board lists jobs and a sample of job pages parses (mostly open)."""
    async with HttpClient.from_settings() as http:
        connector = connector_cls(http)
        stubs = await connector.fetch_jobs(board)
        assert stubs, f"no job URLs for {board}"
        step = max(1, len(stubs) // sample)
        details = [await connector.fetch_job_detail(board, s) for s in stubs[::step][:sample]]
    now = datetime.now(UTC)
    parsed = [d for d in details if d is not None and d.description_html]
    assert parsed
    assert all(d.title for d in parsed)
    assert any(d.expires_at is None or d.expires_at > now for d in parsed)
    return stubs
