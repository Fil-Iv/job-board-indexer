import time

import httpx
import pytest

from jobindex.http import FetchError, HttpClient, RobotsDisallowedError

ROBOTS = "User-agent: *\nDisallow: /private\n"


def _client(handler: httpx.MockTransport, **kw: object) -> HttpClient:
    opts: dict[str, object] = {"backoff_base": 0.0, "min_interval_per_host": 0.0, "max_retries": 2}
    opts.update(kw)
    return HttpClient(user_agent="JobIndexBot/test", transport=handler, **opts)  # type: ignore[arg-type]


def _router(
    responses: dict[str, list[httpx.Response]], seen: list[httpx.Request]
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        queue = responses[request.url.path]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.MockTransport(handle)


async def test_sends_user_agent_and_returns_json() -> None:
    seen: list[httpx.Request] = []
    async with _client(_router({"/jobs": [httpx.Response(200, json={"ok": True})]}, seen)) as http:
        assert await http.get_json("https://example.test/jobs") == {"ok": True}
    assert all(r.headers["User-Agent"] == "JobIndexBot/test" for r in seen)


async def test_retries_transient_errors_then_succeeds() -> None:
    seen: list[httpx.Request] = []
    responses = {
        "/jobs": [httpx.Response(503), httpx.Response(429), httpx.Response(200, text="ok")]
    }
    async with _client(_router(responses, seen)) as http:
        response = await http.get("https://example.test/jobs")
    assert response.text == "ok"
    assert [r.url.path for r in seen] == ["/robots.txt", "/jobs", "/jobs", "/jobs"]


async def test_gives_up_after_max_retries() -> None:
    seen: list[httpx.Request] = []
    async with _client(_router({"/jobs": [httpx.Response(500)]}, seen)) as http:
        with pytest.raises(FetchError) as info:
            await http.get("https://example.test/jobs")
    assert info.value.status_code == 500
    assert len([r for r in seen if r.url.path == "/jobs"]) == 3  # 1 try + 2 retries


async def test_does_not_retry_client_errors() -> None:
    seen: list[httpx.Request] = []
    async with _client(_router({"/jobs": [httpx.Response(404)]}, seen)) as http:
        with pytest.raises(FetchError):
            await http.get("https://example.test/jobs")
    assert len([r for r in seen if r.url.path == "/jobs"]) == 1


async def test_retries_transport_errors() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    async with _client(httpx.MockTransport(handle)) as http:
        assert (await http.get("https://example.test/jobs")).status_code == 200
    assert calls == 2


async def test_respects_robots_txt() -> None:
    seen: list[httpx.Request] = []
    async with _client(_router({"/private/x": [httpx.Response(200)]}, seen)) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://example.test/private/x")
    assert [r.url.path for r in seen] == ["/robots.txt"]


async def test_robots_txt_itself_is_always_fetchable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    async with _client(httpx.MockTransport(handle)) as http:
        assert "Disallow: /" in (await http.get("https://example.test/robots.txt")).text
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://example.test/jobs")


async def test_robots_5xx_means_disallow() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/robots.txt" else 200)

    async with _client(httpx.MockTransport(handle)) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://example.test/jobs")


async def test_rate_limits_per_host() -> None:
    seen: list[httpx.Request] = []
    responses = {"/a": [httpx.Response(200)]}
    async with _client(_router(responses, seen), min_interval_per_host=0.2) as http:
        start = time.monotonic()
        for _ in range(3):
            await http.get("https://example.test/a")
        elapsed = time.monotonic() - start
    # robots.txt + 3 requests => at least 3 intervals between 4 request starts.
    assert elapsed >= 0.6


async def test_honours_crawl_delay() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 0.3\n")
        return httpx.Response(200)

    async with _client(httpx.MockTransport(handle), min_interval_per_host=0.0) as http:
        start = time.monotonic()
        await http.get("https://example.test/a")
        await http.get("https://example.test/b")
        elapsed = time.monotonic() - start
    assert elapsed >= 0.3


async def test_robots_wildcards_are_respected() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /*/apply\n")
        return httpx.Response(200)

    async with _client(httpx.MockTransport(handle)) as http:
        await http.get("https://example.test/jobs/1")
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://example.test/jobs/1/apply")
