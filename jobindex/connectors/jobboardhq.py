"""jobBoardHQ job boards (ASP.NET WebForms; list rendered in a browser, JSON-LD job pages).

List: ``/search`` (robots.txt allows it on every board seen) shows 20 jobs and pages only
      through ASP.NET postbacks (``__doPostBack('Page', 'N')``, a form POST carrying the
      page's ``__VIEWSTATE``); GET parameters are ignored and there is no sitemap or feed.
      The list is therefore read with the polite headless browser (``jobindex.browser``),
      the way a visitor does it: choose "100 Rows", then click the pager's page links
      one by one until the last page the pager shows. Nothing is read out of the page to
      be sent elsewhere; the browser makes the same requests a visitor's would, each
      checked against robots.txt and throttled per host.
      The site's own extent is the pager: every page but the last must hold exactly the
      chosen number of rows, the last at least one, no job may appear twice (an order
      that shifts between postbacks fails the crawl) and the page shown must be the page
      clicked. When the result is longer than the site will show, it says "Only the top
      1000 jobs found matching your selection are shown" (``#lblLimitExceeded``): such a
      board cannot be listed completely, so the crawl fails (roaddogjobs.com,
      njschooljobs.com, www.estatejobs.com and www.greaterroccareers.com, 2026-11-11).
Job:  ``/job/{code}/{slug}/{city}/{state}`` with a JSON-LD JobPosting, fetched with the
      normal HTTP client (no browser).

Boards run on their owners' domains and load the vendor's assets from
``siteresource.blob.core.windows.net/assets/prod/{site}/board.css|js``; recognised by
that + the page URL. Board identifier: the site host. Verified on jobs.macslist.org and
www.jobsindoorcounty.com (2026-11-11).
"""

import logging
import re
from typing import ClassVar, Protocol

from selectolax.lexbor import LexborHTMLParser

from jobindex.browser import BrowserPage, PoliteBrowser, settle
from jobindex.connectors.base import ConnectorError
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register
from jobindex.http import HttpClient

log = logging.getLogger(__name__)

#: Rows per page chosen in the page's own "Rows" menu (largest offered: fewest requests).
ROWS = 100
#: Safety stop: more pager pages than this means something is wrong with the pager.
MAX_PAGES = 100
#: How long one postback may take (each request waits for its turn: 1 per second).
PAGE_TIMEOUT_MS = 90_000
#: Hosts the page may load from: the board itself + the vendor's asset store.
ASSET_HOSTS = ("siteresource.blob.core.windows.net",)


class SearchPage(Protocol):
    """The ``/search`` page as a visitor drives it (a real browser page, or a fake)."""

    async def html(self) -> str: ...
    async def choose_rows(self, rows: int) -> None: ...
    async def click_page(self, number: int) -> None: ...


class _BrowserSearchPage:
    def __init__(self, browser: PoliteBrowser, url: str) -> None:
        self._browser = browser
        self._url = url
        self._bp: BrowserPage | None = None

    async def open(self) -> None:
        self._bp = await self._browser.open(self._url, hosts=ASSET_HOSTS)

    async def html(self) -> str:
        assert self._bp is not None
        await settle(self._bp, quiet_ms=300)
        return await self._bp.html()

    async def choose_rows(self, rows: int) -> None:
        assert self._bp is not None
        await self._bp.page.select_option("#ddlPageRows", str(rows))
        await self._wait_for(
            f"() => document.querySelector('#ddlPageRows option[selected]')?.value === '{rows}'"
        )

    async def click_page(self, number: int) -> None:
        assert self._bp is not None
        page = self._bp.page
        link = page.locator("a.pagerPageLink", has_text=re.compile(rf"^\s*{number}\s*$"))
        if await link.count() != 1:
            raise ConnectorError(f"{self._url}: no pager link for page {number}")
        await link.click()
        await self._wait_for(
            "() => document.querySelector('.pagerLinks .pagerCurrentPage')"
            f"?.textContent.trim() === '{number}'"
        )

    async def _wait_for(self, condition: str) -> None:
        """The pager and the rows menu post back in place (an ASP.NET UpdatePanel: an XHR
        POST to /search, no navigation); wait until the page shows the result."""
        assert self._bp is not None
        try:
            await self._bp.page.wait_for_function(condition, timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            self._bp.check()  # a disallowed / refused request explains it best
            raise ConnectorError(f"{self._url}: the page did not update ({exc})") from exc
        self._bp.check()


class SearchState:
    """What one rendered ``/search`` page shows."""

    def __init__(self, html: str) -> None:
        tree = LexborHTMLParser(html)
        # Each result links its job twice (title + row overlay); keep the page order.
        hrefs = (node.attributes.get("href") or "" for node in tree.css("#jobList a[href]"))
        self.job_paths: list[str] = list(dict.fromkeys(h for h in hrefs if h.startswith("/job/")))
        current = tree.css_first(".pagerLinks .pagerCurrentPage")
        self.current = int(current.text(strip=True)) if current else 1
        numbers = [
            int(text)
            for node in tree.css(".pagerLinks .pagerPageLink, .pagerLinks .pagerCurrentPage")
            if (text := node.text(strip=True)).isdigit()
        ]
        self.last = max(numbers, default=1)
        selected = tree.css_first("#ddlPageRows option[selected]")
        self.rows = int(selected.attributes.get("value") or 0) if selected else None
        self.limit_exceeded = tree.css_first("#lblLimitExceeded") is not None
        #: the search form is there (an error page or a redesign would lack it)
        self.is_search = (
            tree.css_first("#ddlPageRows") is not None and tree.css_first("#jobList") is not None
        )


async def read_search(page: SearchPage, url: str, rows: int = ROWS) -> list[str]:
    """Every job path the search lists, page by page, with the checks described above."""
    state = SearchState(await page.html())
    if not state.is_search:  # read while the page was still updating: look once more
        state = SearchState(await page.html())
    if not state.is_search:
        raise ConnectorError(f"{url} is not a jobBoardHQ search page")
    if state.rows != rows and state.job_paths:
        await page.choose_rows(rows)
        state = SearchState(await page.html())
        if state.rows != rows:
            raise ConnectorError(f"{url}: could not choose {rows} rows (shows {state.rows})")
    if state.limit_exceeded:
        raise ConnectorError(
            f"{url}: the site shows only part of its jobs ('Only the top ... jobs'); "
            "the list cannot be read completely"
        )
    pages = [state]
    while state.current < state.last:
        if len(pages) >= MAX_PAGES:
            raise ConnectorError(f"{url}: more than {MAX_PAGES} pages")
        wanted = state.current + 1
        await page.click_page(wanted)
        state = SearchState(await page.html())
        if state.current != wanted or state.limit_exceeded:
            raise ConnectorError(f"{url}: asked for page {wanted}, got {state.current}")
        pages.append(state)
    paths: list[str] = []
    for i, shown in enumerate(pages, start=1):
        last = i == len(pages)
        if (last and not 0 < len(shown.job_paths) <= rows) or (
            not last and len(shown.job_paths) != rows
        ):
            if len(pages) == 1 and not shown.job_paths:
                return []  # an empty board: one page, no jobs
            raise ConnectorError(
                f"{url}: page {i} of {len(pages)} lists {len(shown.job_paths)} jobs"
            )
        paths += shown.job_paths
    if len(set(paths)) != len(paths):
        raise ConnectorError(
            f"{url}: {len(paths) - len(set(paths))} job(s) listed twice across pages "
            "(the order changed between pages)"
        )
    log.info("jobboardhq %s: %d jobs on %d page(s)", url, len(paths), len(pages))
    return paths


@register
class JobBoardHQConnector(HostedBoardConnector):
    platform = "jobboardhq"
    uses_browser = True
    page_fingerprint = re.compile(
        r"siteresource\.blob\.core\.windows\.net/assets/prod/[a-z0-9]+/board\.(?:css|js)", re.I
    )
    job_path = re.compile(r"/job/(?P<id>[a-z0-9]+)(?:/[^/]+){1,3}")
    #: Replaced in tests by a fake that serves saved pages.
    browser_class: ClassVar[type[PoliteBrowser]] = PoliteBrowser

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        url = f"https://{board_identifier}/search"
        paths = await self.read_list(self.http, url)
        return [(f"https://{board_identifier}{path}", None) for path in paths]

    async def read_list(self, http: HttpClient, url: str) -> list[str]:
        async with self.browser_class(http) as browser:
            page = _BrowserSearchPage(browser, url)
            await page.open()
            return await read_search(page, url)
