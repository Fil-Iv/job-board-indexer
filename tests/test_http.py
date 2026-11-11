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


async def test_corrupt_gzip_body_is_retried_then_a_fetch_error() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls += 1
        body = b"not gzip" if calls < 3 else b""
        return httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})

    async with _client(httpx.MockTransport(handle), max_retries=1) as http:
        with pytest.raises(FetchError, match="DecodingError"):
            await http.get("https://example.test/jobs")
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


async def test_robots_txt_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-running worker re-reads robots.txt: after 24 h, or after 10 min when the
    last attempt failed (5xx = disallow all, which must not stick)."""
    robots_status = [503]
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status[0], text=ROBOTS)
        return httpx.Response(200)

    clock = [1000.0]
    monkeypatch.setattr("jobindex.http._robots_clock", lambda: clock[0])
    async with _client(httpx.MockTransport(handle)) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://example.test/jobs")
        robots_status[0] = 200
        clock[0] += 599
        with pytest.raises(RobotsDisallowedError):  # still cached
            await http.get("https://example.test/jobs")
        clock[0] += 2
        await http.get("https://example.test/jobs")  # re-fetched: allowed now
        clock[0] += 23 * 3600
        await http.get("https://example.test/jobs")
        clock[0] += 3600
        await http.get("https://example.test/jobs")
    assert [r.url.path for r in seen] == [
        "/robots.txt",
        "/robots.txt",
        "/jobs",
        "/jobs",
        "/robots.txt",
        "/jobs",
    ]


def _redirecting(
    robots: dict[str, str], pages: dict[str, httpx.Response], seen: list[httpx.Request]
) -> httpx.MockTransport:
    """Hosts' robots.txt from ``robots`` (404 = allow all); pages keyed ``host/path``."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host = request.url.host
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots[host]) if host in robots else httpx.Response(404)
        return pages.get(f"{host}{request.url.path}", httpx.Response(404))

    return httpx.MockTransport(handle)


def _to(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


async def test_follows_redirects_checking_each_hosts_robots() -> None:
    seen: list[httpx.Request] = []
    pages = {
        "a.test/job": _to("https://b.test/job"),
        "b.test/job": _to("/final", 301),
        "b.test/final": httpx.Response(200, text="ok"),
    }
    async with _client(_redirecting({"b.test": ROBOTS}, pages, seen)) as http:
        response = await http.get("https://a.test/job")
    assert response.text == "ok"
    assert str(response.url) == "https://b.test/final"
    assert [r.status_code for r in response.history] == [302, 301]
    assert [f"{r.url.host}{r.url.path}" for r in seen] == [
        "a.test/robots.txt",
        "a.test/job",
        "b.test/robots.txt",
        "b.test/job",
        "b.test/final",
    ]


async def test_redirect_to_disallowed_target_is_never_fetched() -> None:
    seen: list[httpx.Request] = []
    pages = {
        "a.test/job": _to("https://b.test/private/job"),
        "b.test/private/job": httpx.Response(200),
    }
    async with _client(_redirecting({"b.test": ROBOTS}, pages, seen)) as http:
        with pytest.raises(RobotsDisallowedError) as info:
            await http.get("https://a.test/job")
    assert info.value.url == "https://b.test/private/job"
    assert [str(r.url) for r in info.value.history] == ["https://a.test/job"]
    assert "b.test/private/job" not in [f"{r.url.host}{r.url.path}" for r in seen]


async def test_redirect_within_host_to_disallowed_path_is_refused() -> None:
    seen: list[httpx.Request] = []
    pages = {"a.test/job": _to("/private/login"), "a.test/private/login": httpx.Response(200)}
    async with _client(_redirecting({"a.test": ROBOTS}, pages, seen)) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://a.test/job")
    assert [r.url.path for r in seen] == ["/robots.txt", "/job"]


async def test_at_most_five_redirects() -> None:
    seen: list[httpx.Request] = []
    five = {f"a.test/{i}": _to(f"/{i + 1}") for i in range(5)} | {"a.test/5": httpx.Response(200)}
    async with _client(_redirecting({}, five, seen)) as http:
        assert len((await http.get("https://a.test/0")).history) == 5

    six = {f"a.test/{i}": _to(f"/{i + 1}") for i in range(6)} | {"a.test/6": httpx.Response(200)}
    async with _client(_redirecting({}, six, seen)) as http:
        with pytest.raises(FetchError, match="more than 5 redirects"):
            await http.get("https://a.test/0")


async def test_303_after_post_becomes_get() -> None:
    seen: list[httpx.Request] = []
    pages = {"a.test/search": _to("/results", 303), "a.test/results": httpx.Response(200)}
    async with _client(_redirecting({}, pages, seen)) as http:
        await http.request("POST", "https://a.test/search", json={"q": "x"})
    assert [(r.method, r.url.path) for r in seen if r.url.path != "/robots.txt"] == [
        ("POST", "/search"),
        ("GET", "/results"),
    ]


async def test_robots_txt_redirect_is_followed() -> None:
    pages = {
        "a.test/robots.txt": _to("https://www.a.test/robots.txt", 301),
        "a.test/private/x": httpx.Response(200),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.a.test":
            return httpx.Response(200, text=ROBOTS)
        return pages.get(f"{request.url.host}{request.url.path}", httpx.Response(404))

    async with _client(httpx.MockTransport(handle)) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://a.test/private/x")
