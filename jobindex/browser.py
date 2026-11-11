"""Polite headless browser for the few connectors that must render a page.

Approved by the project owner (2026-11-11) for one case only: rendering a public job page
like a normal visitor when robots.txt allows the page **and every request the page makes
for the job list**. Used by jobBoardHQ, Dayforce, CSOD and Paycom; everything else uses
``jobindex.http.HttpClient`` directly. Install with ``pip install -e ".[browser]"`` and
``playwright install chromium`` (see docs/OPERATIONS.md).

The browser follows the crawling rules of ``HttpClient`` and borrows its robots.txt cache
and per-host throttle (:meth:`HttpClient.admit`):

* The page loads like it does for a normal visitor: all its scripts, from any host,
  including bot-check scripts (owner decision 2026-11-11: they are not blocked). Only
  images, media and fonts are skipped (not needed to read a job list). Every request is
  checked against its own host's robots.txt and waits for its host's turn (default
  1 request/second, or the host's ``Crawl-delay``) via :meth:`HttpClient.admit`.
* The connector's ``hosts`` are the board's own (first-party) hosts. A first-party
  request robots.txt disallows is not sent and fails the crawl
  (:class:`BrowserBlockedError`): the list could be incomplete without it. A disallowed
  third-party request (analytics, maps, feature flags...) is not sent either, and only
  logged (``skipped_third_party``).
* If the site then shows a challenge or refuses (first-party HTTP 401/403/429, a
  ``cf-mitigated: challenge`` response, or a challenge/CAPTCHA page:
  :data:`CHALLENGE_PATTERNS`), the crawl fails with :class:`BrowserBlockedError` and is
  recorded as blocked. Nothing is ever solved, clicked through or retried around.
* The User-Agent names the crawler (``HTTP_USER_AGENT``) in the form big crawlers use,
  ``Mozilla/5.0 (compatible; JobIndexBot/0.1; +url)`` (:func:`browser_user_agent`):
  ASP.NET WebForms sites (jobBoardHQ) send their pager script only to agents of that
  form. It still names the bot; robots.txt is matched on the same ``JobIndexBot``
  token as everywhere else. A real browser's User-Agent is never sent.
* Page tokens and session cookies stay inside the page: nothing is read out of the page
  to be replayed elsewhere. Connectors read only what the page renders, or the bodies of
  the job-list responses the page itself received (:attr:`BrowserPage.responses`).
"""

import asyncio
import contextlib
import logging
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import urlsplit

from jobindex.http import FetchError, HttpClient

if TYPE_CHECKING:  # playwright is an optional dependency (extra "browser")
    from playwright.async_api import Browser, BrowserContext, Page, Playwright, Request, Route
    from playwright.async_api import Response as PwResponse

log = logging.getLogger(__name__)

#: Resource types never fetched: not needed to read a job list.
SKIPPED_RESOURCES = frozenset({"image", "media", "font", "manifest", "texttrack", "eventsource"})
#: Statuses that mean "the site refuses us" (never retried, never worked around).
REFUSED_STATUS = frozenset({401, 403, 429})
#: Markers of a challenge / CAPTCHA the site *shows* (interstitial or widget), not of the
#: background bot-check scripts many pages load for every visitor (Cloudflare's
#: ``/cdn-cgi/challenge-platform/scripts/jsd/main.js`` is on ordinary Dayforce pages).
CHALLENGE_PATTERNS = re.compile(
    r"<title>\s*(?:Just a moment\.\.\.|Attention Required! \| Cloudflare)"
    r"|cf_chl_opt|cf-chl-widget|challenges\.cloudflare\.com/turnstile"
    r"|class=[\"'][^\"']*\b(?:g-recaptcha|h-captcha)\b|captcha-delivery\.com|px-captcha"
    r"|Incapsula incident|Request unsuccessful\. Incapsula|verify you are (?:a )?human",
    re.I,
)
#: Default navigation / settle timeout. Hosts with a long Crawl-delay need more: every
#: request of the page waits for its turn (CSOD: 10 s each).
NAVIGATION_TIMEOUT_MS = 60_000

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class BrowserUnavailableError(RuntimeError):
    """Playwright or its Chromium is not installed (extra "browser")."""


class BrowserBlockedError(FetchError):
    """The page needed something robots.txt disallows, or the site refused / challenged
    us. Recorded as a fetch failure; never worked around."""


@dataclass(slots=True)
class LoggedRequest:
    method: str
    url: str
    resource_type: str
    status: int | None = None  # None: not sent (skipped or disallowed) or no response


@dataclass(slots=True)
class BrowserPage:
    """One rendered page and everything it requested."""

    page: "Page"
    hosts: frozenset[str]
    #: Every request the page made, in order, with what happened to it.
    log: list[LoggedRequest] = field(default_factory=list)
    #: Requests not sent because robots.txt disallows them: (url, deciding rule).
    disallowed: list[tuple[str, str | None]] = field(default_factory=list)
    #: Refusals / challenges seen: (url, status or "challenge").
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: Bodies of document/xhr/fetch responses from allowed hosts whose URL matched
    #: ``capture`` (url, status, body), in arrival order.
    responses: list[tuple[str, int, bytes]] = field(default_factory=list)
    #: Third-party requests robots.txt disallows (not sent; not fatal): (url, rule).
    skipped_third_party: list[tuple[str, str | None]] = field(default_factory=list)
    #: Response bodies still being read (awaited by :func:`settle`).
    pending: list["asyncio.Future[None]"] = field(default_factory=list)

    def check(self) -> None:
        """Raise if the page needed a disallowed URL or was refused/challenged."""
        if self.disallowed:
            url, rule = self.disallowed[0]
            raise BrowserBlockedError(
                f"robots.txt disallows {url} ({rule}), which the page requested; "
                f"{len(self.disallowed)} request(s) not sent",
                url=url,
            )
        if self.refused:
            url, what = self.refused[0]
            status = int(what) if what.isdigit() else None
            raise BrowserBlockedError(f"{url}: refused ({what})", url=url, status_code=status)

    async def html(self) -> str:
        content: str = await self.page.content()
        if CHALLENGE_PATTERNS.search(content[:20000]):
            self.refused.append((self.page.url, "challenge"))
        self.check()
        return content


def browser_user_agent(user_agent: str) -> str:
    """``JobIndexBot/0.1 (+url)`` -> ``Mozilla/5.0 (compatible; JobIndexBot/0.1; +url)``."""
    if user_agent.startswith("Mozilla/"):
        return user_agent
    product, _, comment = user_agent.partition(" ")
    comment = comment.strip().removeprefix("(").removesuffix(")").strip()
    return f"Mozilla/5.0 (compatible; {product}{'; ' + comment if comment else ''})"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def host_allowed(host: str, hosts: Iterable[str]) -> bool:
    """``host`` is one of ``hosts`` or a subdomain of a ``.suffix`` entry."""
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in hosts)


class PoliteBrowser:
    """``async with PoliteBrowser(http) as browser: page = await browser.open(url, ...)``"""

    def __init__(self, http: HttpClient, *, max_retries: int | None = None) -> None:
        self.http = http
        self.max_retries = http.max_retries if max_retries is None else max_retries
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> Self:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise BrowserUnavailableError(
                'Playwright is not installed: pip install -e ".[browser]" && '
                "playwright install chromium"
            ) from exc
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
        except Exception as exc:
            await self._pw.stop()
            raise BrowserUnavailableError(f"cannot start Chromium: {exc}") from exc
        self._context = await self._browser.new_context(
            user_agent=browser_user_agent(self.http.user_agent),
            locale="en-US",
            service_workers="block",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def open(
        self,
        url: str,
        *,
        hosts: Iterable[str],
        capture: re.Pattern[str] | None = None,
        wait_until: WaitUntil = "networkidle",
        timeout_ms: int = NAVIGATION_TIMEOUT_MS,
    ) -> BrowserPage:
        """Open ``url`` in a new page; only requests to ``hosts`` (exact, or ``.suffix``)
        are sent. Retries navigation errors and 5xx with backoff; refusals raise."""
        assert self._context is not None, "use 'async with PoliteBrowser(...)'"
        allowed_hosts = frozenset(h.lower() for h in hosts) | {_host(url)}
        attempt = 0
        while True:
            page = await self._context.new_page()
            bp = BrowserPage(page=page, hosts=allowed_hosts)
            await self._instrument(bp, capture)
            try:
                response = await page.goto(
                    url,
                    wait_until=wait_until,
                    timeout=timeout_ms,
                )
            except Exception as exc:
                bp.check()  # a disallowed/refused request explains the failure best
                await page.close()
                if attempt >= self.max_retries:
                    raise FetchError(f"browser could not load {url}: {exc}", url=url) from exc
                await self._backoff(attempt, url, type(exc).__name__)
                attempt += 1
                continue
            status = response.status if response is not None else None
            if status is not None and status >= 500 and attempt < self.max_retries:
                await page.close()
                await self._backoff(attempt, url, f"HTTP {status}")
                attempt += 1
                continue
            bp.check()
            if status is not None and status >= 400:
                await page.close()
                raise FetchError(f"HTTP {status} from {url}", url=url, status_code=status)
            return bp

    async def _backoff(self, attempt: int, url: str, reason: str) -> None:
        delay = self.http.backoff_base * (2**attempt) * (1 + random.random() * 0.25)
        log.info("browser: retrying %s after %s (sleeping %.1fs)", url, reason, delay)
        await asyncio.sleep(delay)

    async def _instrument(self, bp: BrowserPage, capture: re.Pattern[str] | None) -> None:
        async def on_route(route: "Route", request: "Request") -> None:
            entry = LoggedRequest(request.method, request.url, request.resource_type)
            bp.log.append(entry)
            if not request.url.startswith(("http://", "https://")):
                await route.continue_()  # data:, blob: — nothing goes over the network
                return
            if request.resource_type in SKIPPED_RESOURCES:
                await route.abort("blockedbyclient")
                return
            allowed, rule = await self.http.admit(request.url)
            if not allowed:
                first_party = host_allowed(_host(request.url), bp.hosts)
                log.log(
                    logging.WARNING if first_party else logging.INFO,
                    "browser: robots.txt disallows %s (%s); not sent",
                    request.url,
                    rule,
                )
                target = bp.disallowed if first_party else bp.skipped_third_party
                target.append((request.url, rule))
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        def on_response(response: "PwResponse") -> None:
            request = response.request
            for entry in reversed(bp.log):
                if entry.url == request.url and entry.status is None:
                    entry.status = response.status
                    break
            if response.headers.get("cf-mitigated", "").lower() == "challenge":
                bp.refused.append((request.url, "challenge"))  # Cloudflare challenge shown
                return
            if request.resource_type not in ("document", "xhr", "fetch"):
                return
            if response.status in REFUSED_STATUS:
                if host_allowed(_host(request.url), bp.hosts):
                    bp.refused.append((request.url, str(response.status)))
                else:
                    log.info("browser: third-party %s answered %d", request.url, response.status)
            elif capture is not None and capture.search(request.url):
                bp.pending.append(asyncio.ensure_future(_keep(response)))

        async def _keep(response: "PwResponse") -> None:
            with contextlib.suppress(Exception):  # body gone (redirect / page closed)
                bp.responses.append((response.url, response.status, await response.body()))

        bp.page.on("response", on_response)
        await bp.page.route("**/*", on_route)


async def settle(
    bp: BrowserPage, *, quiet_ms: int = 1500, timeout_ms: int = NAVIGATION_TIMEOUT_MS
) -> None:
    """Wait until the page's network is idle and captured response bodies are read."""
    with contextlib.suppress(Exception):
        await bp.page.wait_for_load_state("networkidle", timeout=timeout_ms)
    await bp.page.wait_for_timeout(quiet_ms)
    if bp.pending:
        await asyncio.gather(*bp.pending, return_exceptions=True)


#: A factory so tests can swap the browser for a fake.
BrowserFactory = Callable[[HttpClient], PoliteBrowser]
