"""Polite HTTP client shared by all connectors (and by URL resolution).

Enforces the crawling rules from CLAUDE.md:

* a clear, configurable User-Agent;
* ``robots.txt`` is fetched per origin, cached for 24 h (10 min when it could not be
  fetched, which means "disallow all") and respected (RFC 9309, see ``robots.py``);
* requests to one host start at least ``min_interval_per_host`` seconds apart (default 1s),
  or the host's ``Crawl-delay`` if that is longer;
* timeouts, and retries with exponential backoff (+ jitter, honouring ``Retry-After``)
  for connection errors, timeouts, 429 and 5xx;
* redirects are followed manually (at most ``MAX_REDIRECTS`` hops), and every target is
  checked against the robots.txt of *its own* host and throttled per host before it is
  fetched; a disallowed target raises :class:`RobotsDisallowedError`.

Anything that still fails raises :class:`FetchError`; the crawler records it as a fetch
failure. Connectors must use this client and never create their own ``httpx`` clients.
"""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from jobindex.config import Settings, get_settings
from jobindex.robots import RobotsTxt

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 120.0
#: Redirect hops followed per request; one more raises FetchError.
MAX_REDIRECTS = 5


class FetchError(Exception):
    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class RobotsDisallowedError(FetchError):
    pass


class _HostThrottle:
    """Ensures requests to one host start at least ``min_interval`` seconds apart."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_start = float("-inf")

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last_start + self.min_interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_start = time.monotonic()


#: How long a fetched robots.txt is trusted (RFC 9309 §2.4: at most 24 hours). The worker
#: keeps one client for days, so entries must expire.
ROBOTS_TTL_SECONDS = 24 * 3600
#: Unreachable / 5xx robots.txt means "disallow all"; retry it much sooner.
ROBOTS_ERROR_TTL_SECONDS = 600


def _robots_clock() -> float:
    """Clock for robots.txt expiry (separate so tests can move it)."""
    return time.monotonic()


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        min_interval_per_host: float = 1.0,
        respect_robots: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.min_interval_per_host = min_interval_per_host
        self.respect_robots = respect_robots
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            # Redirects are followed in request() so every hop is checked against robots.txt.
            follow_redirects=False,
            transport=transport,
        )
        self._throttles: dict[str, _HostThrottle] = {}
        self._robots: dict[str, RobotsTxt] = {}
        self._robots_expires: dict[str, float] = {}  # origin -> _robots_clock() deadline
        #: HTTP status of each origin's robots.txt fetch (None = unreachable), for evidence.
        self.robots_status: dict[str, int | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        s = settings or get_settings()
        return cls(
            user_agent=s.http_user_agent,
            timeout=s.http_timeout_seconds,
            max_retries=s.http_max_retries,
            backoff_base=s.http_backoff_base_seconds,
            min_interval_per_host=s.rate_limit_per_host_seconds,
            respect_robots=s.respect_robots_txt,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- public API ---------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        return await self.request("GET", url, params=params, headers=headers)

    async def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        response = await self.get(url, params=params, headers={"Accept": "application/json"})
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"invalid JSON from {url}", url=url) from exc

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        current = self._client.build_request(method, url, params=params, headers=headers, json=json)
        history: list[httpx.Response] = []
        while True:
            response = await self._send(current)
            if not response.is_redirect:
                response.history = history
                return response
            next_request = response.next_request
            if next_request is None:  # 3xx without a usable Location: nothing to follow
                response.history = history
                return response
            await response.aclose()
            history.append(response)
            if len(history) > MAX_REDIRECTS:
                raise FetchError(
                    f"more than {MAX_REDIRECTS} redirects from {url}",
                    url=str(current.url),
                    status_code=response.status_code,
                )
            log.debug(
                "%s redirected (%d) to %s", current.url, response.status_code, next_request.url
            )
            current = next_request

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """One hop: robots.txt check, throttle, retries. Redirects are returned as is."""
        url = str(request.url)
        # robots.txt itself is always fetchable (RFC 9309); "Disallow: /" must not hide it.
        is_robots_file = request.url.path == "/robots.txt"
        if self.respect_robots and not is_robots_file and not await self._allowed_by_robots(url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}", url=url)

        attempt = 0
        while True:
            await self._throttle(url)
            try:
                response = await self._client.send(request)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise FetchError(f"{type(exc).__name__}: {exc}", url=url) from exc
                await self._backoff(attempt, url, reason=type(exc).__name__)
            else:
                if response.status_code < 400:
                    return response
                if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise FetchError(
                        f"HTTP {response.status_code} from {url}",
                        url=url,
                        status_code=response.status_code,
                    )
                await self._backoff(
                    attempt,
                    url,
                    reason=f"HTTP {response.status_code}",
                    retry_after=response.headers.get("Retry-After"),
                )
            attempt += 1

    # -- internals ----------------------------------------------------------

    def _throttle_for(self, host: str) -> _HostThrottle:
        throttle = self._throttles.get(host)
        if throttle is None:
            throttle = self._throttles[host] = _HostThrottle(self.min_interval_per_host)
        return throttle

    async def _throttle(self, url: str) -> None:
        await self._throttle_for(urlsplit(url).netloc.lower()).wait()

    async def _backoff(
        self, attempt: int, url: str, *, reason: str, retry_after: str | None = None
    ) -> None:
        delay = self.backoff_base * (2**attempt) * (1 + random.random() * 0.25)
        if retry_after is not None:
            # Ignore the HTTP-date form of Retry-After; exponential backoff still applies.
            with contextlib.suppress(ValueError):
                delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
        log.info(
            "retrying %s after %s (attempt %d, sleeping %.2fs)", url, reason, attempt + 1, delay
        )
        await asyncio.sleep(delay)

    async def robots_sitemaps(self, url: str) -> list[str]:
        """``Sitemap:`` URLs declared in the robots.txt of ``url``'s origin."""
        await self._allowed_by_robots(url)
        parts = urlsplit(url)
        return list(self._robots[f"{parts.scheme}://{parts.netloc}".lower()].sitemaps)

    async def robots_verdict(self, url: str) -> tuple[bool, str | None]:
        """``(allowed, deciding rule)`` for ``url`` under its host's robots.txt."""
        await self._allowed_by_robots(url)
        parts = urlsplit(url)
        robots = self._robots[f"{parts.scheme}://{parts.netloc}".lower()]
        return robots.explain(self.user_agent, url)

    async def _allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            robots = self._robots.get(origin)
            if robots is None or _robots_clock() >= self._robots_expires[origin]:
                robots = self._robots[origin] = await self._load_robots(origin)
                status = self.robots_status.get(origin)
                ttl = (
                    ROBOTS_ERROR_TTL_SECONDS
                    if status is None or status >= 500
                    else ROBOTS_TTL_SECONDS
                )
                self._robots_expires[origin] = _robots_clock() + ttl
                delay = robots.crawl_delay(self.user_agent)
                throttle = self._throttle_for(parts.netloc.lower())
                if delay is not None and delay > throttle.min_interval:
                    log.info("%s asks for Crawl-delay %.1fs; honouring it", origin, delay)
                    throttle.min_interval = delay
        return robots.can_fetch(self.user_agent, url)

    async def _load_robots(self, origin: str) -> RobotsTxt:
        """Fetch robots.txt per RFC 9309: 4xx => allow all; 5xx/unreachable => disallow all."""
        robots_url = f"{origin}/robots.txt"
        await self._throttle(robots_url)
        try:
            # RFC 9309 §2.3.1.2: follow (at least five) redirects for robots.txt itself.
            response = await self._client.get(robots_url, follow_redirects=True)
        except httpx.TransportError as exc:
            log.warning("could not fetch %s (%s); treating host as disallowed", robots_url, exc)
            self.robots_status[origin] = None
            return RobotsTxt.disallow_all()
        self.robots_status[origin] = response.status_code
        if response.status_code >= 500:
            log.warning(
                "%s returned %d; treating host as disallowed", robots_url, response.status_code
            )
            return RobotsTxt.disallow_all()
        if response.status_code >= 400:
            return RobotsTxt.allow_all()
        return RobotsTxt.parse(response.text)
