"""The MCP endpoint (jobindex/api/mcp.py): protocol handshake, tools, errors, rate limit."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.api import mcp, ratelimit
from jobindex.api.app import app
from jobindex.api.ratelimit import RateLimiter
from jobindex.config import get_settings
from tests.test_api import _seed

HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app.state.session_factory = session_factory
    ratelimit.limiter._hits.clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def rpc(client: httpx.AsyncClient, method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        message["params"] = params
    response = await client.post("/mcp", json=message, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body: dict[str, Any] = response.json()
    assert body["id"] == 7
    return body


async def call(client: httpx.AsyncClient, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = (
        await rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    )["result"]
    return result


async def test_handshake(client: httpx.AsyncClient) -> None:
    body = await rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert body["result"]["serverInfo"]["name"] == "jobforge"
    unknown = await rpc(client, "initialize", {"protocolVersion": "1999-01-01"})
    assert unknown["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]

    note = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=HEADERS
    )
    assert note.status_code == 202
    assert (await rpc(client, "ping"))["result"] == {}


async def test_tools_list(client: httpx.AsyncClient) -> None:
    tools = {t["name"]: t for t in (await rpc(client, "tools/list"))["result"]["tools"]}
    assert set(tools) == {"search_jobs", "get_job"}
    props = tools["search_jobs"]["inputSchema"]["properties"]
    assert set(props) == {"q", "company", "location", "remote_type", "seniority", "page"}
    assert tools["get_job"]["inputSchema"]["required"] == ["id"]
    assert all(t["annotations"]["readOnlyHint"] for t in tools.values())


async def test_search_and_get(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await _seed(session)
    result = await call(client, "search_jobs", {"q": "python", "location": "Germany"})
    data = result["structuredContent"]
    assert data["total"] == 1
    job = data["jobs"][0]
    assert job["id"] == 1
    assert job["title"] == "Senior Python Engineer"
    assert job["remote_type"] == "remote"
    assert job["salary"] == {"min": 70000.0, "max": 90000.0, "currency": "EUR", "period": "year"}
    assert job["url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert '"Senior Python Engineer"' in result["content"][0]["text"]

    remote = await call(client, "search_jobs", {"remote_type": "hybrid"})
    assert remote["structuredContent"]["total"] == 0

    detail = (await call(client, "get_job", {"id": 2}))["structuredContent"]
    assert detail["id"] == 1  # any listing resolves to its opening
    assert detail["description"] == "FastAPI and PostgreSQL"
    assert [x["platform"] for x in detail["listings"]] == ["greenhouse", "niceboard"]

    missing = await call(client, "get_job", {"id": 999})
    assert missing["isError"] is True


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_jobs", {"page": 0}),
        ("search_jobs", {"remote_type": "moon"}),
        ("search_jobs", {"salary": 1}),
        ("get_job", {}),
    ],
)
async def test_invalid_arguments_are_tool_errors(
    client: httpx.AsyncClient, tool: str, arguments: dict[str, Any]
) -> None:
    result = await call(client, tool, arguments)
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("Invalid arguments")


async def test_protocol_errors(client: httpx.AsyncClient) -> None:
    assert (await rpc(client, "resources/list"))["error"]["code"] == mcp.METHOD_NOT_FOUND
    unknown = await rpc(client, "tools/call", {"name": "delete_everything"})
    assert unknown["error"]["code"] == mcp.INVALID_PARAMS

    bad = await client.post("/mcp", content=b"{nope", headers=HEADERS)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == mcp.PARSE_ERROR
    batch = await client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert batch.status_code == 400

    stream = await client.get("/mcp")
    assert stream.status_code == 405
    assert stream.headers["allow"] == "POST"


async def test_rate_limit(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    try:
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        other = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
        for _ in range(3):
            assert (await client.post("/mcp", json=ping)).status_code == 200
        limited = await client.post("/mcp", json=ping)
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) >= 1
        assert (await client.post("/mcp", json=ping, headers=other)).status_code == 200
    finally:
        get_settings.cache_clear()


def test_rate_limiter_window() -> None:
    limiter = RateLimiter(window=60)
    assert [limiter.retry_after("a", 2, now=t) for t in (0, 1, 2)] == [0, 0, 59]
    assert limiter.retry_after("a", 2, now=60.5) == 0  # first hit left the window


async def test_agents_section_on_search_page(client: httpx.AsyncClient) -> None:
    page = (await client.get("/", headers={"X-Forwarded-Proto": "https"})).text
    assert 'id="agents"' in page
    assert "claude mcp add --transport http jobforge https://test/mcp" in page
    assert '"mcp-remote", "https://test/mcp"' in page
